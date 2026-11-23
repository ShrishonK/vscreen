"""Disk-backed response cache.

Every fetch module in ``fetch/`` goes through this cache (``docs/PHASES.md``).
Nothing in vscreen contacts a remote server except ``DiskCache.fetch``: the API
wrappers hand the cache a request, and the cache either serves it from
``<project>/cache/`` or asks its injected ``fetcher`` to go get it. Tests inject
a fetcher that raises, which is how the convention "tests never hit the
network" (``docs/CONVENTIONS.md``) is enforced mechanically.

Cache entries are JSON files named by the sha256 of ``(method, url, params,
body)``. A ``.meta.json`` sidecar is deliberately avoided: one self-contained
file per request keeps a dropped entry discoverable by a plain ``grep``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import VscreenError

__all__ = [
    "CacheMiss",
    "FetchBlocked",
    "FetchError",
    "CacheEntry",
    "DiskCache",
    "offline_fetcher",
    "build_cache_for",
    "OFFLINE_ENV",
]

OFFLINE_ENV = "VSCREEN_OFFLINE"

log = logging.getLogger(__name__)

DEFAULT_TTL_S: int = 7 * 24 * 60 * 60  # One week.


class CacheMiss(VscreenError):
    """A lookup missed and no fetcher was configured to satisfy it."""


class FetchBlocked(VscreenError):
    """The offline fetcher was invoked: a test or run tried to hit the network."""


class FetchError(VscreenError):
    """A fetcher ran but failed. The URL is in the message so the user can retry."""


@dataclass(frozen=True)
class CacheEntry:
    """One cached response. ``text`` and ``binary`` are mutually exclusive."""

    url: str
    method: str
    fetched_at: str
    status: int
    text: str | None = None
    binary_b64: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_binary(self) -> bool:
        return self.binary_b64 is not None

    def body_text(self) -> str:
        if self.text is None:
            raise FetchError(
                f"Cache entry for {self.url} is binary; use body_bytes() to read it"
            )
        return self.text

    def body_bytes(self) -> bytes:
        if self.binary_b64 is not None:
            return base64.b64decode(self.binary_b64)
        if self.text is None:
            raise FetchError(f"Cache entry for {self.url} is empty")
        return self.text.encode("utf-8")

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": self.url,
            "method": self.method,
            "fetched_at": self.fetched_at,
            "status": self.status,
            "headers": dict(self.headers),
        }
        if self.binary_b64 is not None:
            payload["binary_b64"] = self.binary_b64
        else:
            payload["text"] = self.text
        return payload

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CacheEntry:
        return cls(
            url=str(data["url"]),
            method=str(data.get("method", "GET")),
            fetched_at=str(data.get("fetched_at", "")),
            status=int(data.get("status", 200)),
            text=data.get("text"),
            binary_b64=data.get("binary_b64"),
            headers=dict(data.get("headers", {})),
        )


FetcherFn = Callable[[str, str, Mapping[str, str] | None, bytes | None], "CacheEntry"]


def offline_fetcher(
    url: str,
    method: str,
    params: Mapping[str, str] | None,
    body: bytes | None,
) -> CacheEntry:
    """A fetcher that refuses to go online; used by tests (rule: offline after first fetch)."""
    raise FetchBlocked(
        f"Refusing to fetch {method} {url} — tests and offline runs must be served "
        f"by the cache alone. Prime it from tests/fixtures/ or run with network access."
    )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _canon_params(params: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    if not params:
        return []
    items: list[tuple[str, str]] = [(str(k), str(v)) for k, v in params.items()]
    items.sort(key=lambda kv: (kv[0], kv[1]))
    return items


def _hash_key(method: str, url: str, params: Mapping[str, Any] | None, body: bytes | None) -> str:
    """Key over (method, url, canonicalised params, exact body bytes). Stable across runs."""
    parts: dict[str, Any] = {
        "method": method.upper(),
        "url": url,
        "params": _canon_params(params),
        "body_sha256": hashlib.sha256(body or b"").hexdigest(),
    }
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DiskCache:
    """A content-addressable store of HTTP-shaped responses.

    Construct once per run under ``<project>/cache/`` (``docs/PHASES.md``).
    Inject a fetcher at construction; tests inject ``offline_fetcher`` so the
    cache cannot silently reach the network.
    """

    def __init__(
        self,
        root: Path,
        *,
        fetcher: FetcherFn | None = None,
        default_ttl_s: int = DEFAULT_TTL_S,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.default_ttl_s = int(default_ttl_s)
        self.fetcher: FetcherFn = fetcher or offline_fetcher

    # -- addressing -------------------------------------------------------- #

    def key_for(
        self,
        url: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        body: bytes | None = None,
    ) -> str:
        return _hash_key(method, url, params, body)

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.json"

    # -- reads ------------------------------------------------------------- #

    def load(self, key: str) -> CacheEntry | None:
        path = self.path_for(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FetchError(f"Corrupt cache entry {path}: {exc}") from exc
        return CacheEntry.from_json(data)

    def _expired(self, entry: CacheEntry, ttl_s: int | None) -> bool:
        if ttl_s is None:
            return False
        if not entry.fetched_at:
            return True
        try:
            fetched = datetime.fromisoformat(entry.fetched_at)
        except ValueError:
            return True
        age = (datetime.now(UTC) - fetched).total_seconds()
        return age > ttl_s

    # -- writes ------------------------------------------------------------ #

    def _write_entry(self, key: str, entry: CacheEntry) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(entry.to_json(), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def put_text(
        self,
        url: str,
        text: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> CacheEntry:
        """Store a text response explicitly; used for test priming and for manual seeding."""
        entry = CacheEntry(
            url=url,
            method=method.upper(),
            fetched_at=_utc_now(),
            status=status,
            text=text,
            headers=dict(headers or {}),
        )
        self._write_entry(self.key_for(url, method=method, params=params, body=body), entry)
        return entry

    def put_bytes(
        self,
        url: str,
        data: bytes,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> CacheEntry:
        entry = CacheEntry(
            url=url,
            method=method.upper(),
            fetched_at=_utc_now(),
            status=status,
            binary_b64=base64.b64encode(data).decode("ascii"),
            headers=dict(headers or {}),
        )
        self._write_entry(self.key_for(url, method=method, params=params, body=body), entry)
        return entry

    # -- resolve ----------------------------------------------------------- #

    def resolve(
        self,
        url: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        ttl_s: int | None = None,
        force_refresh: bool = False,
    ) -> CacheEntry:
        """Serve from cache, or ask the fetcher. Writes every fresh response back."""
        key = self.key_for(url, method=method, params=params, body=body)
        ttl = self.default_ttl_s if ttl_s is None else ttl_s
        if not force_refresh:
            entry = self.load(key)
            if entry is not None and not self._expired(entry, ttl):
                log.debug("cache hit %s (%s %s)", key[:12], method, url)
                return entry
        entry = self.fetcher(url, method.upper(), params, body)
        if entry.fetched_at == "":
            entry = CacheEntry(
                url=entry.url,
                method=entry.method,
                fetched_at=_utc_now(),
                status=entry.status,
                text=entry.text,
                binary_b64=entry.binary_b64,
                headers=entry.headers,
            )
        self._write_entry(key, entry)
        log.info("cache store %s (%s %s)", key[:12], method, url)
        return entry

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        ttl_s: int | None = None,
        force_refresh: bool = False,
    ) -> Any:
        entry = self.resolve(
            url, method="GET", params=params, ttl_s=ttl_s, force_refresh=force_refresh
        )
        try:
            return json.loads(entry.body_text())
        except json.JSONDecodeError as exc:
            raise FetchError(f"Cached body for {url} is not JSON: {exc}") from exc

    def post_json(
        self,
        url: str,
        json_body: Any,
        *,
        ttl_s: int | None = None,
        force_refresh: bool = False,
    ) -> Any:
        body = json.dumps(json_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        entry = self.resolve(
            url, method="POST", body=body, ttl_s=ttl_s, force_refresh=force_refresh
        )
        try:
            return json.loads(entry.body_text())
        except json.JSONDecodeError as exc:
            raise FetchError(f"Cached body for POST {url} is not JSON: {exc}") from exc

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        ttl_s: int | None = None,
        force_refresh: bool = False,
    ) -> str:
        entry = self.resolve(
            url, method="GET", params=params, ttl_s=ttl_s, force_refresh=force_refresh
        )
        return entry.body_text()

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        ttl_s: int | None = None,
        force_refresh: bool = False,
    ) -> bytes:
        entry = self.resolve(
            url, method="GET", params=params, ttl_s=ttl_s, force_refresh=force_refresh
        )
        return entry.body_bytes()


# --------------------------------------------------------------------------- #
# Default live fetcher — used only when a run is explicitly allowed to go
# online. Tests never pass this.
# --------------------------------------------------------------------------- #


def build_cache_for(project_dir: Path, *, force_offline: bool = False) -> DiskCache:
    """Build the cache a CLI command uses. ``VSCREEN_OFFLINE=1`` forces offline.

    Tests set the env var so a missing fixture surfaces as a ``FetchBlocked``
    rather than as a silent live request (``docs/CONVENTIONS.md`` — tests
    never hit the network).
    """
    import os

    offline = force_offline or os.environ.get(OFFLINE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    fetcher: FetcherFn = offline_fetcher if offline else http_fetcher()
    return DiskCache(Path(project_dir) / "cache", fetcher=fetcher)


def http_fetcher(timeout_s: float = 60.0) -> FetcherFn:
    """Build a fetcher backed by ``requests``. Opt-in; never used by tests."""
    import requests  # local import keeps tests independent of network stack

    def fetch(
        url: str,
        method: str,
        params: Mapping[str, str] | None,
        body: bytes | None,
    ) -> CacheEntry:
        start = time.monotonic()
        try:
            resp = requests.request(
                method=method,
                url=url,
                params=dict(params) if params else None,
                data=body,
                timeout=timeout_s,
            )
        except requests.RequestException as exc:
            raise FetchError(f"{method} {url} failed: {exc}") from exc
        elapsed = time.monotonic() - start
        log.info("fetched %s %s in %.2fs (status %d)", method, url, elapsed, resp.status_code)
        if resp.status_code >= 400:
            raise FetchError(
                f"{method} {url} returned HTTP {resp.status_code}; "
                f"first 200 bytes: {resp.text[:200]!r}"
            )
        content_type = resp.headers.get("content-type", "")
        is_binary = (
            "application/octet-stream" in content_type
            or url.lower().endswith((".pdb.gz", ".cif.gz", ".bcif", ".mmtf"))
        )
        if is_binary:
            data = resp.content
            return CacheEntry(
                url=url,
                method=method,
                fetched_at=_utc_now(),
                status=resp.status_code,
                binary_b64=base64.b64encode(data).decode("ascii"),
                headers={"content-type": content_type} if content_type else {},
            )
        return CacheEntry(
            url=url,
            method=method,
            fetched_at=_utc_now(),
            status=resp.status_code,
            text=resp.text,
            headers={"content-type": content_type} if content_type else {},
        )

    return fetch
