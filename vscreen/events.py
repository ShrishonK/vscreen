"""Events and the Reporter protocol (rule 2).

Core code emits events; ``ui/`` renders them. Nothing in this module, in
``core/`` or in ``stages/`` may import ``rich``: that is what lets the same
stage code drive a Rich dashboard, a plain log, a notebook or an OSPool job
without changing a line.

The event set is fixed in ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "StageStarted",
    "ItemCompleted",
    "StageFinished",
    "LogLine",
    "Warning",
    "Event",
    "Reporter",
    "NullReporter",
    "CollectingReporter",
]


@dataclass(frozen=True)
class StageStarted:
    stage: str
    total: int


@dataclass(frozen=True)
class ItemCompleted:
    stage: str
    item_id: str
    ok: bool
    resumed: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class StageFinished:
    stage: str
    counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LogLine:
    level: str
    text: str


@dataclass(frozen=True)
class Warning:  # noqa: A001 - the event name is fixed by docs/ARCHITECTURE.md
    """A warning for the user's attention.

    Deliberately shadows the builtin inside this module only; the event names
    are part of the documented interface, so importers should use
    ``events.Warning``.
    """

    text: str


Event = StageStarted | ItemCompleted | StageFinished | LogLine | Warning


@runtime_checkable
class Reporter(Protocol):
    def emit(self, event: Event) -> None: ...


class NullReporter:
    """Drops every event. The default where no interface is attached."""

    def emit(self, event: Event) -> None:  # noqa: D102
        return None


class CollectingReporter:
    """Keeps every event in order. For tests and for post-run summaries."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, *types: type) -> list[Event]:
        return [e for e in self.events if isinstance(e, types)]
