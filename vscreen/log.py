"""File logging.

``logs/`` inside the project directory receives the log; stdout belongs to the
user interface (``docs/CONVENTIONS.md``). Nothing here writes to stdout or
stderr, so a Rich dashboard is never corrupted by a log record.
"""

from __future__ import annotations

import logging
from pathlib import Path

__all__ = ["configure_logging", "LOG_FORMAT"]

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_HANDLER_TAG = "vscreen-file-log"


def configure_logging(project_dir: Path, *, level: int = logging.INFO) -> Path:
    """Attach a file handler for ``<project_dir>/logs/vscreen.log``.

    Idempotent: calling it twice for the same project does not duplicate
    records. Returns the log file path.
    """
    log_dir = Path(project_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "vscreen.log"

    root = logging.getLogger("vscreen")
    root.setLevel(level)
    root.propagate = False
    for existing in list(root.handlers):
        if getattr(existing, "_vscreen_tag", None) == _HANDLER_TAG:
            if Path(getattr(existing, "baseFilename", "")) == log_path.resolve():
                return log_path
            root.removeHandler(existing)
            existing.close()

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.setLevel(level)
    handler._vscreen_tag = _HANDLER_TAG  # type: ignore[attr-defined]
    root.addHandler(handler)
    return log_path
