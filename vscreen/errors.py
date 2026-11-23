"""Typed exceptions.

Every error raised by vscreen carries an actionable message: what failed, and
what the user should do about it. A bare ``FileNotFoundError`` is never
acceptable (``docs/CONVENTIONS.md``).
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "VscreenError",
    "ConfigError",
    "ProjectNotFound",
    "StoreError",
    "ToolNotFound",
    "ToolVersionError",
    "StageError",
    "UnknownStage",
    "GpuUnavailable",
]


class VscreenError(Exception):
    """Base class for every error vscreen raises deliberately."""


class ConfigError(VscreenError):
    """A config file is missing, malformed, or fails validation."""


class ProjectNotFound(VscreenError):
    """No vscreen project directory was found."""

    def __init__(self, start: Path, config_name: str = "vscreen.yaml") -> None:
        self.start = start
        self.config_name = config_name
        super().__init__(
            f"No {config_name} found in {start} or any parent directory. "
            f"Create a project with: vscreen init NAME"
        )


class StoreError(VscreenError):
    """The SQLite store could not be opened, initialised, or written."""


class ToolNotFound(VscreenError):
    """An external binary could not be located on PATH."""

    def __init__(self, name: str, candidates: tuple[str, ...], install_hint: str) -> None:
        self.name = name
        self.candidates = candidates
        self.install_hint = install_hint
        tried = ", ".join(candidates)
        super().__init__(f"{name} not found on PATH (tried: {tried}). {install_hint}")


class ToolVersionError(VscreenError):
    """An external binary was found but its version could not be captured.

    Rule 5: a result without a recorded tool version is not publishable, so a
    missing version is an error rather than a warning.
    """


class StageError(VscreenError):
    """A stage could not be run."""


class UnknownStage(VscreenError):
    """A stage name is not present in the registry."""

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        self.name = name
        self.known = known
        available = ", ".join(known) if known else "none registered"
        super().__init__(f"Unknown stage {name!r}. Available stages: {available}")


class GpuUnavailable(VscreenError):
    """A stage declaring ``needs_gpu`` was asked to run on a machine without one.

    Rule 6: this is a clean exit with instructions, never a crash and never a
    silent fall back to a CPU path two orders of magnitude slower.
    """

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(
            f"Stage {stage!r} requires a GPU and no GPU was detected on this machine. "
            f"Run it on a GPU host or via a remote runner; nothing was written to the store."
        )
