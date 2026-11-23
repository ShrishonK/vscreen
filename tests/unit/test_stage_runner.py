"""StageRunner: run keys, resume at item granularity, results, events.

The resumability test is the one that matters: run 20 items, die after 10,
re-run, and exactly 10 more items execute.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

import pytest

from vscreen import store
from vscreen.config import StageConfig
from vscreen.errors import GpuUnavailable, StageError, ToolNotFound
from vscreen.events import ItemCompleted, StageFinished, StageStarted
from vscreen.stages import base as stage_base
from vscreen.stages.base import Context, Item, Requirements, Result, Stage, StageRunner
from vscreen.stages.demo import DemoStage
from vscreen.stages.registry import STAGES, build_stage, get_stage_class, register, stage_names


class CountingStage(DemoStage):
    """DemoStage that records every execution and can die partway through."""

    requires = Requirements(cpu_cores=1)

    def __init__(self, *, n_items: int = 20, die_after: int | None = None) -> None:
        super().__init__(n_items=n_items, sleep_s=0.0)
        self.die_after = die_after
        self.executed: list[str] = []
        self._lock = threading.Lock()

    def run_item(self, ctx: Context, item: Item) -> list[Result]:
        with self._lock:
            self.executed.append(item.id)
            count = len(self.executed)
        if self.die_after is not None and count > self.die_after:
            raise KeyboardInterrupt("simulated Ctrl-C")
        return super().run_item(ctx, item)


# --------------------------------------------------------------------------- #
# a plain successful run
# --------------------------------------------------------------------------- #


def test_successful_run_writes_items_results_and_events(ctx: Context) -> None:
    stage = DemoStage(n_items=5, sleep_s=0.0)
    outcome = StageRunner().run(stage, ctx)

    assert outcome.status == "complete"
    assert (outcome.total, outcome.executed, outcome.done, outcome.failed) == (5, 5, 5, 0)
    assert outcome.resumed is False

    run = store.get_stage_run(ctx.conn, outcome.run_id)
    assert run is not None
    assert run.stage == "demo"
    assert run.run_key == outcome.run_key and len(run.run_key) == 64
    assert run.status == "complete"
    assert run.started_at and run.finished_at
    assert "python" in run.tool_versions_json  # rule 5
    assert store.item_status_counts(ctx.conn, run.id) == {"done": 5}
    assert store.count_results(ctx.conn, stage_run_id=run.id) == 10  # two metrics per item
    assert store.items_done_without_results(ctx.conn, run.id) == []

    events = ctx.reporter.events  # type: ignore[attr-defined]
    assert isinstance(events[0], StageStarted) and events[0].total == 5
    assert isinstance(events[-1], StageFinished)
    assert events[-1].counts == {
        "total": 5,
        "executed": 5,
        "done": 5,
        "failed": 0,
        "resumed": 0,
    }
    assert sum(isinstance(e, ItemCompleted) and e.ok for e in events) == 5


def test_results_are_deterministic_for_a_seed(ctx: Context) -> None:
    StageRunner().run(DemoStage(n_items=3, sleep_s=0.0), ctx)
    first = {r.compound_id: r.value_num for r in store.list_results(ctx.conn, metric="demo_score")}

    ctx.conn.execute("DELETE FROM stage_runs")
    StageRunner().run(DemoStage(n_items=3, sleep_s=0.0), ctx)
    second = {r.compound_id: r.value_num for r in store.list_results(ctx.conn, metric="demo_score")}
    assert first == second


def test_demo_scores_are_negative_like_a_docking_score(ctx: Context) -> None:
    StageRunner().run(DemoStage(n_items=10, sleep_s=0.0), ctx)
    values = [r.value_num for r in store.list_results(ctx.conn, metric="demo_score")]
    assert values and all(v is not None and -12.0 <= v <= -4.0 for v in values)


# --------------------------------------------------------------------------- #
# resume: the acceptance test for rule 3
# --------------------------------------------------------------------------- #


def test_interrupted_run_resumes_and_executes_exactly_the_remainder(ctx: Context) -> None:
    first = CountingStage(n_items=20, die_after=10)
    with pytest.raises(KeyboardInterrupt):
        StageRunner().run(first, ctx)

    runs = store.list_stage_runs(ctx.conn)
    assert len(runs) == 1
    run_id = runs[0].id
    assert runs[0].status == "running"  # not complete, not failed: resumable
    assert len(store.done_item_ids(ctx.conn, run_id)) == 10
    assert store.count_results(ctx.conn, stage_run_id=run_id) == 20

    second = CountingStage(n_items=20)
    outcome = StageRunner().run(second, ctx)

    assert len(second.executed) == 10, "a resumed run must not redo completed items"
    assert set(second.executed) == {f"demo-C{i:03d}" for i in range(11, 21)}
    assert outcome.run_id == run_id, "the same run key must reuse the same stage_run row"
    assert outcome.resumed is True
    assert outcome.status == "complete"
    assert outcome.counts == {
        "total": 20,
        "executed": 10,
        "done": 20,
        "failed": 0,
        "resumed": 10,
    }
    assert len(store.list_stage_runs(ctx.conn)) == 1
    assert store.count_results(ctx.conn, stage_run_id=run_id) == 40
    assert store.items_done_without_results(ctx.conn, run_id) == []


def test_resuming_a_finished_run_executes_nothing(ctx: Context) -> None:
    StageRunner().run(CountingStage(n_items=6), ctx)
    again = CountingStage(n_items=6)
    outcome = StageRunner().run(again, ctx)
    assert again.executed == []
    assert outcome.executed == 0 and outcome.done == 6 and outcome.status == "complete"
    assert len(store.list_stage_runs(ctx.conn)) == 1


def test_attempts_are_charged_for_an_interrupted_item(ctx: Context) -> None:
    with pytest.raises(KeyboardInterrupt):
        StageRunner().run(CountingStage(n_items=20, die_after=10), ctx)
    run_id = store.list_stage_runs(ctx.conn)[0].id
    StageRunner().run(CountingStage(n_items=20), ctx)
    item = store.get_item(ctx.conn, run_id, "demo-C011")
    assert item is not None and item.status == "done" and item.attempts == 2


# --------------------------------------------------------------------------- #
# run keys
# --------------------------------------------------------------------------- #


def test_changing_stage_config_starts_a_new_run(ctx: Context) -> None:
    first = StageRunner().run(DemoStage(n_items=4, sleep_s=0.0), ctx)
    ctx.config.stages = {"demo": StageConfig(params={"exhaustiveness": 32})}
    second = StageRunner().run(DemoStage(n_items=4, sleep_s=0.0), ctx)

    assert second.run_key != first.run_key
    assert second.run_id != first.run_id
    assert len(store.list_stage_runs(ctx.conn)) == 2, "history is preserved, never overwritten"


def test_changing_the_item_set_starts_a_new_run(ctx: Context) -> None:
    first = StageRunner().run(DemoStage(n_items=4, sleep_s=0.0), ctx)
    second = StageRunner().run(DemoStage(n_items=5, sleep_s=0.0), ctx)
    assert second.run_key != first.run_key
    assert len(store.list_stage_runs(ctx.conn)) == 2


def test_run_key_covers_tool_versions(ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:
    first = StageRunner().run(DemoStage(n_items=3, sleep_s=0.0), ctx)
    monkeypatch.setattr(
        stage_base, "tool_versions", lambda names: {"python": "9.9.9", "vscreen": "0.1.0"}
    )
    second = StageRunner().run(DemoStage(n_items=3, sleep_s=0.0), ctx)
    assert second.run_key != first.run_key


# --------------------------------------------------------------------------- #
# failures
# --------------------------------------------------------------------------- #


class FailingStage(DemoStage):
    requires = Requirements(cpu_cores=1)

    def __init__(self, *, n_items: int = 4, fail_ids: tuple[str, ...] = ()) -> None:
        super().__init__(n_items=n_items, sleep_s=0.0)
        self.fail_ids = fail_ids

    def run_item(self, ctx: Context, item: Item) -> list[Result]:
        if item.id in self.fail_ids:
            raise RuntimeError(f"vina exited 1 for {item.id}")
        return super().run_item(ctx, item)


def test_a_failed_item_is_recorded_not_raised(ctx: Context) -> None:
    outcome = StageRunner().run(FailingStage(n_items=4, fail_ids=("demo-C002",)), ctx)

    assert outcome.status == "failed"
    assert (outcome.done, outcome.failed) == (3, 1)
    run = store.get_stage_run(ctx.conn, outcome.run_id)
    assert run is not None and run.status == "failed"
    assert run.error_text == "1 of 4 items failed"
    item = store.get_item(ctx.conn, outcome.run_id, "demo-C002")
    assert item is not None and item.status == "failed"
    assert "vina exited 1" in (item.error_text or "")
    assert store.count_results(ctx.conn, stage_run_id=outcome.run_id) == 6

    failures = [e for e in ctx.reporter.events if isinstance(e, ItemCompleted) and not e.ok]  # type: ignore[attr-defined]
    assert len(failures) == 1 and failures[0].item_id == "demo-C002"


def test_a_failed_item_is_retried_on_the_next_run(ctx: Context) -> None:
    StageRunner().run(FailingStage(n_items=4, fail_ids=("demo-C002",)), ctx)
    retry = CountingStage(n_items=4)
    outcome = StageRunner().run(retry, ctx)
    assert retry.executed == ["demo-C002"]
    assert outcome.status == "complete" and outcome.done == 4


def test_duplicate_item_ids_are_refused(ctx: Context) -> None:
    class DuplicateStage(DemoStage):
        def iter_items(self, ctx: Context) -> Iterable[Item]:
            items = list(super().iter_items(ctx))
            return [*items, items[0]]

    with pytest.raises(StageError, match="duplicate item id"):
        StageRunner().run(DuplicateStage(n_items=3, sleep_s=0.0), ctx)
    assert store.list_stage_runs(ctx.conn) == []


# --------------------------------------------------------------------------- #
# requirements: tools and GPU are checked before anything is written
# --------------------------------------------------------------------------- #


class VinaStage(DemoStage):
    requires = Requirements(cpu_cores=1, external_tools=("vina",))


def test_a_missing_tool_stops_the_run_before_any_row_is_written(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ToolNotFound, match="conda install"):
        StageRunner().run(VinaStage(n_items=2, sleep_s=0.0), ctx)
    assert store.list_stage_runs(ctx.conn) == []


class GpuStage(DemoStage):
    requires = Requirements(cpu_cores=1, needs_gpu=True)


def test_gpu_stage_exits_cleanly_without_a_gpu(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stage_base, "gpu_available", lambda: False)
    with pytest.raises(GpuUnavailable, match="requires a GPU"):
        StageRunner().run(GpuStage(n_items=2, sleep_s=0.0), ctx)
    assert store.list_stage_runs(ctx.conn) == []
    assert store.count_compounds(ctx.conn) == 0, "nothing at all was written"


def test_gpu_stage_runs_when_a_gpu_is_present(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stage_base, "gpu_available", lambda: True)
    outcome = StageRunner().run(GpuStage(n_items=2, sleep_s=0.0), ctx)
    assert outcome.status == "complete"


# --------------------------------------------------------------------------- #
# parallelism
# --------------------------------------------------------------------------- #


class ThreadNameStage(DemoStage):
    requires = Requirements(cpu_cores=4)

    def __init__(self, *, n_items: int = 8) -> None:
        super().__init__(n_items=n_items, sleep_s=0.03)
        self.threads: set[str] = set()

    def run_item(self, ctx: Context, item: Item) -> list[Result]:
        self.threads.add(threading.current_thread().name)
        return super().run_item(ctx, item)


def test_parallelism_follows_requirements(ctx: Context) -> None:
    stage = ThreadNameStage(n_items=8)
    StageRunner().run(stage, ctx)
    assert len(stage.threads) > 1, "cpu_cores=4 should spread items over worker threads"
    assert all(name.startswith("demo") for name in stage.threads)


def test_worker_override_wins(ctx: Context) -> None:
    stage = ThreadNameStage(n_items=4)
    StageRunner(workers=1).run(stage, ctx)
    assert len(stage.threads) == 1


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def test_registry_knows_the_demo_stage() -> None:
    assert "demo" in stage_names()
    assert get_stage_class("demo") is DemoStage
    assert isinstance(build_stage("demo", n_items=3, sleep_s=0.0), DemoStage)


def test_registry_rejects_unknown_and_duplicate_names() -> None:
    from vscreen.errors import UnknownStage

    with pytest.raises(UnknownStage, match="demo"):
        get_stage_class("dock")

    class Clash(Stage):
        name = "demo"

        def iter_items(self, ctx: Context) -> Iterable[Item]:  # pragma: no cover
            return []

        def run_item(self, ctx: Context, item: Item) -> list[Result]:  # pragma: no cover
            return []

    with pytest.raises(StageError, match="already registered"):
        register(Clash)

    class Nameless(Stage):
        def iter_items(self, ctx: Context) -> Iterable[Item]:  # pragma: no cover
            return []

        def run_item(self, ctx: Context, item: Item) -> list[Result]:  # pragma: no cover
            return []

    with pytest.raises(StageError, match="class-level name"):
        register(Nameless)
    assert "demo" in STAGES


# --------------------------------------------------------------------------- #
# interface guarantees
# --------------------------------------------------------------------------- #


def test_result_requires_a_metric_and_a_compound() -> None:
    with pytest.raises(StageError, match="metric"):
        Result(compound_id="c1", metric="")
    with pytest.raises(StageError, match="compound_id"):
        Result(compound_id="", metric="demo_score")


def test_context_helpers(ctx: Context) -> None:
    assert ctx.seed == 7
    assert ctx.stage_params("demo") == {}
    assert ctx.path("poses", "x.sdf") == ctx.project_dir / "poses" / "x.sdf"


def test_stages_never_import_rich() -> None:
    """Rule 2: core and stage code must stay renderable by any interface."""
    import vscreen.stages.base as base_module
    import vscreen.stages.demo as demo_module

    for module in (base_module, demo_module):
        source = module.__file__
        assert source is not None
        text = open(source, encoding="utf-8").read()  # noqa: SIM115
        assert "import rich" not in text and "from rich" not in text
