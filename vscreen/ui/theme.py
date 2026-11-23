"""Colours and styles for the Rich interface.

Presentation only. Nothing in ``core/`` or ``stages/`` imports this (rule 2).
"""

from __future__ import annotations

from rich.theme import Theme

__all__ = ["THEME", "STATUS_STYLES", "status_style"]

THEME = Theme(
    {
        "vscreen.header": "bold white on grey23",
        "vscreen.project": "bold cyan",
        "vscreen.stage": "bold magenta",
        "vscreen.ok": "green",
        "vscreen.fail": "bold red",
        "vscreen.warn": "yellow",
        "vscreen.dim": "grey62",
        "vscreen.metric": "cyan",
    }
)

#: stage_run / stage_item status -> style name
STATUS_STYLES: dict[str, str] = {
    "pending": "vscreen.dim",
    "running": "vscreen.warn",
    "complete": "vscreen.ok",
    "done": "vscreen.ok",
    "failed": "vscreen.fail",
    "skipped": "vscreen.dim",
}


def status_style(status: str) -> str:
    """Style name for a stage_run or stage_item status."""
    return STATUS_STYLES.get(status, "vscreen.dim")
