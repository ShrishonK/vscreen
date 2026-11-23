"""vscreen — a reusable virtual screening and published-candidate audit pipeline.

Read ``docs/CONVENTIONS.md`` and ``docs/ARCHITECTURE.md`` before changing
anything here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("vscreen")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
