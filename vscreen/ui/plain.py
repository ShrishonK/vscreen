"""One line per event, no dependencies beyond the standard library.

This is the reporter for log files, notebooks, OSPool jobs and anything whose
stdout is not a terminal. ``LiveReporter`` must be swappable for it with no
change to any stage (rule 2).
"""

from __future__ import annotations

import sys
from types import TracebackType
from typing import IO

from ..events import Event, ItemCompleted, LogLine, StageFinished, StageStarted, Warning

__all__ = ["PlainReporter"]


class PlainReporter:
    """Prints events as plain text, flushing each line."""

    def __init__(self, stream: IO[str] | None = None, *, verbose_items: bool = True) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.verbose_items = verbose_items

    # Context-manager shape so callers can swap in LiveReporter unchanged.
    def __enter__(self) -> PlainReporter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def _write(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def emit(self, event: Event) -> None:
        match event:
            case StageStarted(stage=stage, total=total):
                self._write(f"[{stage}] started: {total} item(s)")
            case ItemCompleted(stage=stage, item_id=item_id, ok=ok, resumed=resumed, detail=detail):
                if resumed:
                    if self.verbose_items:
                        self._write(f"[{stage}] {item_id} already done")
                    return
                if ok:
                    if self.verbose_items:
                        self._write(f"[{stage}] {item_id} ok")
                    return
                self._write(f"[{stage}] {item_id} FAILED: {detail or 'no detail'}")
            case StageFinished(stage=stage, counts=counts):
                summary = " ".join(f"{k}={v}" for k, v in counts.items())
                self._write(f"[{stage}] finished: {summary}")
            case LogLine(level=level, text=text):
                self._write(f"[{level}] {text}")
            case Warning(text=text):
                self._write(f"[warning] {text}")
            case _:  # pragma: no cover - a new event type without a renderer
                self._write(str(event))
