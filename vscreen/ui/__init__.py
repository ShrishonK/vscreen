"""User interface layer. Renders events; contains no science (rule 2)."""

from __future__ import annotations

import sys
from typing import IO

from .live import LiveReporter, stdout_is_tty
from .plain import PlainReporter
from .theme import STATUS_STYLES, THEME

__all__ = [
    "LiveReporter",
    "PlainReporter",
    "THEME",
    "STATUS_STYLES",
    "make_reporter",
]


def make_reporter(
    project_name: str,
    *,
    stream: IO[str] | None = None,
    plain: bool = False,
) -> LiveReporter | PlainReporter:
    """The live dashboard on a terminal, the plain reporter everywhere else.

    Both satisfy ``Reporter`` and both are context managers, so the caller does
    not need to know which one it got.
    """
    stream = stream if stream is not None else sys.stdout
    if plain or not stdout_is_tty(stream):
        return PlainReporter(stream)
    return LiveReporter(project_name)
