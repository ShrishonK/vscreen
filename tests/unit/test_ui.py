"""Reporters. The live dashboard must be swappable for the plain one (rule 2)."""

from __future__ import annotations

import io

from rich.console import Console

from vscreen.events import (
    CollectingReporter,
    ItemCompleted,
    LogLine,
    NullReporter,
    Reporter,
    StageFinished,
    StageStarted,
    Warning,
)
from vscreen.ui import LiveReporter, PlainReporter, make_reporter

EVENTS = [
    StageStarted(stage="demo", total=3),
    ItemCompleted(stage="demo", item_id="demo-C001", ok=True),
    ItemCompleted(stage="demo", item_id="demo-C002", ok=True, resumed=True),
    ItemCompleted(stage="demo", item_id="demo-C003", ok=False, detail="vina exited 1"),
    LogLine(level="info", text="resuming demo"),
    Warning(text="no cognate ligand: gate cannot run"),
    StageFinished(stage="demo", counts={"total": 3, "done": 2, "failed": 1}),
]


def test_every_reporter_satisfies_the_protocol() -> None:
    for reporter in (NullReporter(), CollectingReporter(), PlainReporter(io.StringIO())):
        assert isinstance(reporter, Reporter)


def test_plain_reporter_writes_one_line_per_event() -> None:
    stream = io.StringIO()
    with PlainReporter(stream) as reporter:
        for event in EVENTS:
            reporter.emit(event)
    lines = stream.getvalue().splitlines()
    assert len(lines) == len(EVENTS)
    assert lines[0] == "[demo] started: 3 item(s)"
    assert lines[1] == "[demo] demo-C001 ok"
    assert lines[2] == "[demo] demo-C002 already done"
    assert "FAILED: vina exited 1" in lines[3]
    assert lines[4] == "[info] resuming demo"
    assert lines[5].startswith("[warning] no cognate ligand")
    assert lines[6] == "[demo] finished: total=3 done=2 failed=1"


def test_plain_reporter_can_stay_quiet_about_items() -> None:
    stream = io.StringIO()
    reporter = PlainReporter(stream, verbose_items=False)
    for event in EVENTS:
        reporter.emit(event)
    lines = stream.getvalue().splitlines()
    # started, failure, log, warning, finished — the two completed items are quiet
    assert len(lines) == 5
    assert not any("demo-C001" in line for line in lines)


def test_live_reporter_renders_the_dashboard() -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=120, height=30, legacy_windows=False)
    with LiveReporter("egfr-audit", console=console, refresh_per_second=1000) as reporter:
        for event in EVENTS:
            reporter.emit(event)
    output = stream.getvalue()
    assert "egfr-audit" in output
    assert "demo" in output
    assert "vina exited 1" in output or "failed" in output
    assert reporter.failures == 1


def test_live_reporter_tracks_progress_per_stage() -> None:
    console = Console(file=io.StringIO(), force_terminal=True, width=120)
    reporter = LiveReporter("p", console=console)
    reporter.emit(StageStarted(stage="dock", total=2))
    reporter.emit(StageStarted(stage="rescore", total=5))
    assert set(reporter._tasks) == {"dock", "rescore"}
    reporter.emit(ItemCompleted(stage="dock", item_id="c1", ok=True))
    task = reporter.progress.tasks[reporter._tasks["dock"]]
    assert task.completed == 1 and task.total == 2


def test_live_reporter_keeps_only_the_last_eight_log_lines() -> None:
    console = Console(file=io.StringIO(), force_terminal=True, width=120)
    reporter = LiveReporter("p", console=console)
    for i in range(20):
        reporter.emit(LogLine(level="info", text=f"line {i}"))
    assert len(reporter.logs) == 8
    assert "line 19" in reporter.logs[-1].plain


def test_make_reporter_degrades_when_stdout_is_not_a_tty() -> None:
    assert isinstance(make_reporter("p", stream=io.StringIO()), PlainReporter)


def test_make_reporter_uses_the_dashboard_on_a_terminal() -> None:
    class FakeTTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert isinstance(make_reporter("p", stream=FakeTTY()), LiveReporter)
    assert isinstance(make_reporter("p", stream=FakeTTY(), plain=True), PlainReporter)


def test_collecting_reporter_filters_by_type() -> None:
    reporter = CollectingReporter()
    for event in EVENTS:
        reporter.emit(event)
    assert len(reporter.of_type(ItemCompleted)) == 3
    assert len(reporter.of_type(StageStarted, StageFinished)) == 2
    assert NullReporter().emit(EVENTS[0]) is None
