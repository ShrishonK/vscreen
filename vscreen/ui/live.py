"""The Rich dashboard.

Header panel with project name and current stage, one progress bar per running
stage, and a scrolling tail of the last eight log lines. Degrades to
``PlainReporter`` when stdout is not a TTY — see ``make_reporter``.

Rendering only: this module knows nothing about what a stage does (rule 2).
"""

from __future__ import annotations

import sys
from collections import deque
from types import TracebackType
from typing import IO

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from ..events import Event, ItemCompleted, LogLine, StageFinished, StageStarted, Warning
from .theme import THEME

__all__ = ["LiveReporter", "LOG_TAIL"]

LOG_TAIL = 8


class LiveReporter:
    """A live dashboard driven entirely by events."""

    def __init__(
        self,
        project_name: str,
        *,
        console: Console | None = None,
        refresh_per_second: float = 12.0,
        log_tail: int = LOG_TAIL,
    ) -> None:
        self.project_name = project_name
        self.console = console or Console(theme=THEME)
        # A caller-supplied console does not know our style names, so push the
        # theme onto whichever console we ended up with.
        self.console.push_theme(THEME)
        self.current_stage: str | None = None
        self.logs: deque[Text] = deque(maxlen=log_tail)
        self.failures = 0
        self.progress = Progress(
            SpinnerColumn(style="vscreen.stage"),
            TextColumn("[vscreen.stage]{task.fields[stage]}[/]"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("[vscreen.dim]{task.fields[note]}[/]"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self.console,
            expand=True,
        )
        self._tasks: dict[str, TaskID] = {}
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=refresh_per_second,
            transient=False,
        )

    # -- lifecycle --------------------------------------------------------- #

    def __enter__(self) -> LiveReporter:
        self._live.start(refresh=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._live.update(self._render(), refresh=True)
        self._live.stop()

    # -- rendering --------------------------------------------------------- #

    def _header(self) -> RenderableType:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        left = Text.assemble(("vscreen ", "vscreen.dim"), (self.project_name, "vscreen.project"))
        stage = self.current_stage or "idle"
        right = Text.assemble(
            ("stage ", "vscreen.dim"),
            (stage, "vscreen.stage"),
            ("  failures ", "vscreen.dim"),
            (str(self.failures), "vscreen.fail" if self.failures else "vscreen.ok"),
        )
        grid.add_row(left, right)
        return Panel(grid, style="vscreen.header", padding=(0, 1))

    def _log_panel(self) -> RenderableType:
        empty = Text("no log lines yet", style="vscreen.dim")
        body: RenderableType = Group(*self.logs) if self.logs else empty
        return Panel(body, title="log", title_align="left", padding=(0, 1))

    def _render(self) -> RenderableType:
        return Group(self._header(), self.progress, self._log_panel())

    def _log(self, text: str, style: str) -> None:
        self.logs.append(Text(text, style=style))

    # -- Reporter ---------------------------------------------------------- #

    def emit(self, event: Event) -> None:
        match event:
            case StageStarted(stage=stage, total=total):
                self.current_stage = stage
                self._tasks[stage] = self.progress.add_task(
                    description=stage, total=total, stage=stage, note=""
                )
                self._log(f"{stage}: {total} item(s)", "vscreen.dim")
            case ItemCompleted(stage=stage, item_id=item_id, ok=ok, resumed=resumed, detail=detail):
                task = self._tasks.get(stage)
                if task is not None:
                    self.progress.update(task, advance=1, note="resumed" if resumed else "")
                if not ok:
                    self.failures += 1
                    self._log(f"{item_id} failed: {detail or 'no detail'}", "vscreen.fail")
            case StageFinished(stage=stage, counts=counts):
                task = self._tasks.get(stage)
                if task is not None:
                    self.progress.update(task, note="done")
                summary = " ".join(f"{k}={v}" for k, v in counts.items())
                self._log(f"{stage} finished: {summary}", "vscreen.ok")
            case LogLine(level=level, text=text):
                style = "vscreen.fail" if level.lower() in {"error", "critical"} else "vscreen.dim"
                self._log(f"{level}: {text}", style)
            case Warning(text=text):
                self._log(text, "vscreen.warn")
            case _:  # pragma: no cover - a new event type without a renderer
                self._log(str(event), "vscreen.dim")
        self._live.update(self._render())


def stdout_is_tty(stream: IO[str] | None = None) -> bool:
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - detached stream
        return False
