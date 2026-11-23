"""External binaries: locate them, capture their versions (rule 5).

Every external tool vscreen calls is declared here. A stage never shells out to
a binary it found itself — it asks for a resolved tool, and the version string
captured at resolution time is written into
``stage_runs.tool_versions_json``.

A result without a recorded tool version cannot go in a paper, and silent
version drift over a multi-week build is a real risk.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from .errors import ToolNotFound, ToolVersionError

__all__ = [
    "ExternalTool",
    "ResolvedTool",
    "REGISTRY",
    "get_tool",
    "resolve_tool",
    "tool_versions",
    "is_available",
    "runtime_versions",
    "gpu_available",
    "clear_cache",
]

log = logging.getLogger(__name__)

VERSION_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class ResolvedTool:
    """A located binary and the version string captured from it."""

    name: str
    path: Path
    version: str


@dataclass(frozen=True)
class ExternalTool:
    """Declaration of one external binary.

    ``candidates`` are tried in order; the first one on PATH wins.
    ``version_pattern`` is an optional regex with one group; without it, the
    first non-empty line of output is recorded verbatim.
    """

    name: str
    candidates: tuple[str, ...]
    version_args: tuple[str, ...] = ("--version",)
    install_hint: str = ""
    version_pattern: str | None = None

    def locate(self) -> Path:
        for candidate in self.candidates:
            found = shutil.which(candidate)
            if found:
                return Path(found)
        raise ToolNotFound(self.name, self.candidates, self.install_hint)

    def capture_version(self, path: Path) -> str:
        cmd = [str(path), *self.version_args]
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, declared binary, no shell
                cmd,
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_S,
                check=False,
            )
        except OSError as exc:
            raise ToolVersionError(
                f"Could not execute {' '.join(cmd)} to capture the {self.name} version: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolVersionError(
                f"{self.name} did not report a version within {VERSION_TIMEOUT_S:.0f}s "
                f"({' '.join(cmd)})"
            ) from exc

        text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            raise ToolVersionError(
                f"{self.name} at {path} printed no version output for "
                f"{' '.join(self.version_args)}. Rule 5 requires a recorded version, so this "
                f"stage cannot run until the tool reports one."
            )
        if self.version_pattern:
            match = re.search(self.version_pattern, text)
            if match is None:
                raise ToolVersionError(
                    f"Could not parse a {self.name} version from: {lines[0]!r} "
                    f"(pattern {self.version_pattern!r})"
                )
            return match.group(1).strip() if match.groups() else match.group(0).strip()
        return lines[0]

    def resolve(self) -> ResolvedTool:
        """Locate the binary and capture its version, or raise with a hint."""
        path = self.locate()
        return ResolvedTool(name=self.name, path=path, version=self.capture_version(path))


REGISTRY: dict[str, ExternalTool] = {
    "vina": ExternalTool(
        name="vina",
        candidates=("vina", "vina_1.2.5", "autodock_vina"),
        version_args=("--version",),
        install_hint="Install with: conda install -c conda-forge vina",
    ),
    "gnina": ExternalTool(
        name="gnina",
        candidates=("gnina",),
        version_args=("--version",),
        install_hint="Optional rescoring tool; see https://github.com/gnina/gnina",
    ),
    "aizynthcli": ExternalTool(
        name="aizynthcli",
        candidates=("aizynthcli",),
        version_args=("--version",),
        install_hint='Install with: pip install "aizynthfinder[all]" (a separate env is fine)',
    ),
}

_CACHE: dict[str, ResolvedTool] = {}


def clear_cache() -> None:
    """Forget resolved tools. Used by tests and after a PATH change."""
    _CACHE.clear()


def get_tool(name: str) -> ExternalTool:
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ToolNotFound(
            name, (name,), f"Not a registered tool. Registered tools: {known}."
        ) from None


def resolve_tool(name: str, *, refresh: bool = False) -> ResolvedTool:
    """Resolve one registered tool, caching the result for the process."""
    if not refresh and name in _CACHE:
        return _CACHE[name]
    resolved = get_tool(name).resolve()
    _CACHE[name] = resolved
    log.info("resolved %s %s at %s", resolved.name, resolved.version, resolved.path)
    return resolved


def is_available(name: str) -> bool:
    """True when the tool is on PATH and reports a version."""
    try:
        resolve_tool(name)
    except (ToolNotFound, ToolVersionError):
        return False
    return True


def runtime_versions() -> dict[str, str]:
    """Versions that are always recorded, so ``tool_versions_json`` is never empty."""
    try:
        vscreen_version = package_version("vscreen")
    except PackageNotFoundError:  # pragma: no cover - source checkout without install
        vscreen_version = "unknown"
    return {
        "python": sys.version.split()[0],
        "vscreen": vscreen_version,
    }


def tool_versions(names: Iterable[str]) -> dict[str, str]:
    """Resolve every named tool and return ``{name: version}``.

    Always includes the Python and vscreen versions (rule 5), so a stage with
    no external tools still records what produced its numbers.
    """
    versions: dict[str, str] = runtime_versions()
    for name in names:
        resolved = resolve_tool(name)
        versions[resolved.name] = resolved.version
    return versions


def gpu_available(env: Mapping[str, str] | None = None) -> bool:
    """Whether an NVIDIA GPU is visible. Never assumed (rule 6).

    ``VSCREEN_FORCE_GPU=1`` overrides detection for remote runners whose GPU is
    visible to the job but not to ``nvidia-smi``.
    """
    import os

    environ = env if env is not None else os.environ
    forced = environ.get("VSCREEN_FORCE_GPU", "").strip().lower()
    if forced in {"1", "true", "yes"}:
        return True
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [smi, "-L"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and "GPU" in (proc.stdout or "")
