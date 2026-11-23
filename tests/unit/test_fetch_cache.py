"""DiskCache behaviour: content addressing, offline refusal, TTL, binary bodies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vscreen.fetch.cache import (
    CacheEntry,
    DiskCache,
    FetchBlocked,
    FetchError,
    offline_fetcher,
)


def test_put_text_round_trip(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache", fetcher=offline_fetcher)
    cache.put_text("https://example.test/a", '{"ok": true}', params={"q": "egfr"})
    assert cache.get_text("https://example.test/a", params={"q": "egfr"}) == '{"ok": true}'
    assert cache.get_json("https://example.test/a", params={"q": "egfr"}) == {"ok": True}


def test_params_order_does_not_change_the_key(tmp_path: Path) -> None:
    """Canonical parameter ordering: dicts with the same entries hit the same file."""
    cache = DiskCache(tmp_path / "cache", fetcher=offline_fetcher)
    cache.put_text("u", "hello", params={"a": "1", "b": "2"})
    assert cache.get_text("u", params={"b": "2", "a": "1"}) == "hello"


def test_different_method_and_url_hit_different_keys(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache", fetcher=offline_fetcher)
    key_get = cache.key_for("u", method="GET")
    key_post = cache.key_for("u", method="POST")
    key_other = cache.key_for("v", method="GET")
    assert len({key_get, key_post, key_other}) == 3


def test_post_json_round_trip(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache", fetcher=offline_fetcher)
    body = {"query": "{ me }"}
    import json as _json

    serialized = _json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    cache.put_text("gql", '{"data": 1}', method="POST", body=serialized)
    assert cache.post_json("gql", body) == {"data": 1}


def test_offline_fetcher_refuses_misses(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache", fetcher=offline_fetcher)
    with pytest.raises(FetchBlocked, match="Refusing to fetch"):
        cache.get_text("https://never.test/")


def test_ttl_expiry_triggers_refetch(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache", fetcher=offline_fetcher)
    key = cache.key_for("https://example.test/")
    stale_time = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0).isoformat()
    stale = CacheEntry(
        url="https://example.test/",
        method="GET",
        fetched_at=stale_time,
        status=200,
        text="old",
    )
    cache._write_entry(key, stale)  # noqa: SLF001 — test exercises the on-disk format
    # With a generous TTL the stale entry is still fresh enough to serve.
    assert cache.get_text("https://example.test/", ttl_s=365 * 86400) == "old"
    # With a short TTL it expires and the offline fetcher is asked, so we see a refusal.
    with pytest.raises(FetchBlocked):
        cache.get_text("https://example.test/", ttl_s=60)


def test_force_refresh_bypasses_the_cache(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache", fetcher=offline_fetcher)
    cache.put_text("u", "cached")
    with pytest.raises(FetchBlocked):
        cache.get_text("u", force_refresh=True)


def test_binary_round_trip(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache", fetcher=offline_fetcher)
    payload = bytes(range(256))
    cache.put_bytes("bin", payload)
    assert cache.get_bytes("bin") == payload


def test_corrupt_entry_is_reported_with_path(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache", fetcher=offline_fetcher)
    cache.put_text("u", "x")
    # Scribble on the cached file; next read must raise, not silently retry.
    (tmp_path / "cache").iterdir()
    for file in (tmp_path / "cache").iterdir():
        file.write_text("not-json", encoding="utf-8")
    with pytest.raises(FetchError, match="Corrupt cache entry"):
        cache.get_text("u")
