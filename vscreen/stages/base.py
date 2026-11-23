"""The Stage interface and the runner that owns everything around it.

Fixed in Phase 0; every later phase implements against it
(``docs/ARCHITECTURE.md``).

A stage author writes ``iter_items`` and ``run_item``. ``StageRunner`` owns the
run key, creating or resuming the ``stage_run`` row, skipping completed items,
parallelism sized to ``requires.cpu_cores``, writing results, emitting events
and marking completion.

**``run_item`` runs in a worker thread.** It must not touch ``ctx.conn`` — a
SQLite connection belongs to the thread that made it. Return ``Result`` objects
and the runner writes them on the main thread.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .. import store
from ..config import ProjectConfig, canonical_json, stage_hash, utc_now_iso
from ..errors import GpuUnavailable, StageError
from ..events import (
    ItemCompleted,
    LogLine,
    NullReporter,
    Reporter,
    StageFinished,
    StageStarted,
    Warning,
)
from ..tools import gpu_available, tool_versions

__all__ = [
    "Requirements",
    "Item",
    "Result",
    "Context",
    "Stage",
    "StageRunner",
    "StageOutcome",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Requirements:
    """What a stage needs. GPU is an optional accelerator, never assumed (rule 6)."""

    cpu_cores: int = 1
    needs_gpu: bool = False
    external_tools: tuple[str, ...] = ()
    needs_network: bool = False


@dataclass(frozen=True)
class Item:
    """One unit of resumable work. ``id`` is what lands in ``stage_items``."""

    id: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    """One narrow result row (rule 4): one compound, one metric, one value."""

    compound_id: str
    metric: str
    value_num: float | None = None
    value_text: str | None = None

    def __post_init__(self) -> None:
        if not self.metric:
            raise StageError("A Result needs a metric name")
        if not self.compound_id:
            raise StageError(f"A Result for metric {self.metric!r} needs a compound_id")


@dataclass
class Context:
    """Everything a stage is given. Mutable: the runner fills in run-scoped fields."""

    project_dir: Path
    config: ProjectConfig
    conn: sqlite3.Connection
    reporter: Reporter = field(default_factory=NullReporter)
    target_id: str | None = None
    set_id: str | None = None
    tool_versions: Mapping[str, str] = field(default_factory=dict)
    stage_run_id: str | None = None
    input_ids: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def seed(self) -> int:
        return self.config.seed

    def stage_params(self, stage_name: str) -> dict[str, Any]:
        stage = self.config.stages.get(stage_name)
        return dict(stage.params) if stage else {}

    def path(self, *parts: str) -> Path:
        """A path inside the project directory. Never hardcode an absolute path."""
        return self.project_dir.joinpath(*parts)

    def emit(self, event: Any) -> None:
        self.reporter.emit(event)


class Stage(ABC):
    """One pipeline stage."""

    name: ClassVar[str]
    requires: ClassVar[Requirements] = Requirements()
    #: Whether this stage writes narrow ``Result`` rows per item.
    #: Default True (compound-work stages); False for setup stages like
    #: ``target_add`` that write to other tables and whose items wrote no
    #: result rows by design (the orphan check would otherwise fire).
    writes_results: ClassVar[bool] = True

    def config_subtree(self, ctx: Context) -> dict[str, Any]:
        """The config this stage's run key depends on."""
        return ctx.config.stage_subtree(self.name)

    def run_key(self, ctx: Context) -> str:
        """Stable key over config, inputs and tool versions (rule 3).

        The runner materialises the item ids into ``ctx.input_ids`` before
        calling this, so the default implementation does not iterate twice.
        """
        input_ids: Sequence[str] = ctx.input_ids or tuple(item.id for item in self.iter_items(ctx))
        return stage_hash(self.name, self.config_subtree(ctx), input_ids, ctx.tool_versions)

    @abstractmethod
    def iter_items(self, ctx: Context) -> Iterable[Item]:
        """Every item this run covers, completed or not. Called on the main thread."""

    def is_item_done(self, ctx: Context, item: Item) -> bool:
        """Default: the ledger says so. Override only to add a stronger check."""
        if ctx.stage_run_id is None:
            return False
        row = store.get_item(ctx.conn, ctx.stage_run_id, item.id)
        return row is not None and row.status == "done"

    @abstractmethod
    def run_item(self, ctx: Context, item: Item) -> list[Result]:
        """Do the work for one item. Runs in a worker thread; do not touch ctx.conn."""

    def finalize(self, ctx: Context) -> None:
        """Hook after the last item. Runs on the main thread."""
        return None


@dataclass(frozen=True)
class StageOutcome:
    """What a stage run did, for the caller and the interface."""

    stage: str
    run_id: str
    run_key: str
    status: str
    total: int
    executed: int
    done: int
    failed: int
    resumed: bool
    counts: Mapping[str, int]


class StageRunner:
    """Runs a stage: run key, resume, thread pool, results, events, completion."""

    def __init__(self, *, workers: int | None = None) -> None:
        self.workers_override = workers

    # -- public API -------------------------------------------------------- #

    def run(self, stage: Stage, ctx: Context) -> StageOutcome:
        reporter = ctx.reporter
        requires = stage.requires

        self._check_gpu(stage, reporter)
        ctx.tool_versions = tool_versions(requires.external_tools)

        items = list(stage.iter_items(ctx))
        self._check_unique(stage, items)
        ctx.input_ids = tuple(item.id for item in items)

        run_key = stage.run_key(ctx)
        run, resumed = self._open_stage_run(stage, ctx, run_key)
        ctx.stage_run_id = run.id

        pending = [item for item in items if not stage.is_item_done(ctx, item)]
        already_done = len(items) - len(pending)

        log.info(
            "%s run %s key %s: %d item(s), %d already done, tools %s",
            stage.name,
            run.id,
            run_key,
            len(items),
            already_done,
            dict(ctx.tool_versions),
        )
        reporter.emit(StageStarted(stage=stage.name, total=len(items)))
        if resumed:
            reporter.emit(
                LogLine(
                    level="info",
                    text=(
                        f"resuming {stage.name}: {already_done} of {len(items)} items already "
                        f"done (run {run.id[:8]})"
                    ),
                )
            )
        if already_done:
            # Replay completion for items finished by an earlier run, so a
            # progress bar starts where the work actually is.
            pending_ids = {item.id for item in pending}
            for item in items:
                if item.id not in pending_ids:
                    reporter.emit(
                        ItemCompleted(stage=stage.name, item_id=item.id, ok=True, resumed=True)
                    )

        executed, failed = self._execute(stage, ctx, pending, requires)

        stage.finalize(ctx)

        done_total = len(store.done_item_ids(ctx.conn, run.id))
        status = "complete" if failed == 0 and done_total == len(items) else "failed"
        error_text = None if status == "complete" else f"{failed} of {len(items)} items failed"
        store.update_stage_run(
            ctx.conn,
            run.id,
            status=status,  # type: ignore[arg-type]
            error_text=error_text,
            finished_at=utc_now_iso(),
        )

        counts = {
            "total": len(items),
            "executed": executed,
            "done": done_total,
            "failed": failed,
            "resumed": already_done,
        }
        log.info("%s run %s %s: %s", stage.name, run.id, status, counts)
        reporter.emit(StageFinished(stage=stage.name, counts=counts))
        orphans = (
            store.items_done_without_results(ctx.conn, run.id)
            if stage.writes_results
            else []
        )
        if orphans:
            reporter.emit(
                Warning(
                    text=(
                        f"{len(orphans)} item(s) in {stage.name} are marked done but wrote no "
                        f"results (first: {orphans[0]}). Delete the stage run to redo them."
                    )
                )
            )
        return StageOutcome(
            stage=stage.name,
            run_id=run.id,
            run_key=run_key,
            status=status,
            total=len(items),
            executed=executed,
            done=done_total,
            failed=failed,
            resumed=resumed,
            counts=counts,
        )

    # -- internals --------------------------------------------------------- #

    def _check_gpu(self, stage: Stage, reporter: Reporter) -> None:
        if not stage.requires.needs_gpu or gpu_available():
            return
        reporter.emit(
            Warning(
                text=(
                    f"{stage.name} needs a GPU and none was detected. Nothing was written; "
                    f"run this stage on a GPU host."
                )
            )
        )
        raise GpuUnavailable(stage.name)

    def _check_unique(self, stage: Stage, items: Sequence[Item]) -> None:
        seen: set[str] = set()
        for item in items:
            if item.id in seen:
                raise StageError(
                    f"{stage.name} produced duplicate item id {item.id!r}. Item ids are the "
                    f"resume ledger's primary key and must be unique within a run."
                )
            seen.add(item.id)

    def _open_stage_run(
        self, stage: Stage, ctx: Context, run_key: str
    ) -> tuple[store.StageRun, bool]:
        existing = store.get_stage_run_by_key(ctx.conn, run_key)
        if existing is not None:
            store.update_stage_run(
                ctx.conn,
                existing.id,
                status="running",
                started_at=existing.started_at or utc_now_iso(),
            )
            return existing, True

        run = store.StageRun(
            id=uuid.uuid4().hex,
            stage=stage.name,
            run_key=run_key,
            target_id=ctx.target_id,
            set_id=ctx.set_id,
            config_json=canonical_json(stage.config_subtree(ctx)),
            tool_versions_json=canonical_json(ctx.tool_versions),
            status="running",
            started_at=utc_now_iso(),
        )
        with store.transaction(ctx.conn):
            store.insert_stage_run(ctx.conn, run)
        return run, False

    def _execute(
        self,
        stage: Stage,
        ctx: Context,
        pending: Sequence[Item],
        requires: Requirements,
    ) -> tuple[int, int]:
        if not pending:
            return 0, 0
        assert ctx.stage_run_id is not None
        run_id = ctx.stage_run_id
        reporter = ctx.reporter
        workers = max(1, self.workers_override or requires.cpu_cores)

        executed = 0
        failed = 0
        queue = iter(pending)
        in_flight: dict[Future[list[Result]], Item] = {}
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=stage.name)

        def dispatch_next() -> bool:
            """Charge one attempt and submit one item. Keeps in-flight work bounded.

            Only ``workers`` items are ever queued, so an interrupted run has
            not charged attempts to items it never started, and a 100k-compound
            stage does not materialise 100k futures.
            """
            item = next(queue, None)
            if item is None:
                return False
            store.mark_item(ctx.conn, run_id, item.id, "running", increment_attempts=True)
            in_flight[executor.submit(stage.run_item, ctx, item)] = item
            return True

        def fill() -> None:
            while len(in_flight) < workers and dispatch_next():
                pass

        try:
            fill()
            while in_flight:
                finished, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for future in finished:
                    item = in_flight.pop(future)
                    try:
                        results = future.result()
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:  # noqa: BLE001 - a failed item is a data point
                        failed += 1
                        executed += 1
                        store.mark_item(ctx.conn, run_id, item.id, "failed", error_text=str(exc))
                        log.exception("%s item %s failed", stage.name, item.id)
                        reporter.emit(
                            ItemCompleted(
                                stage=stage.name, item_id=item.id, ok=False, detail=str(exc)
                            )
                        )
                        reporter.emit(LogLine(level="error", text=f"{item.id}: {exc}"))
                        continue

                    # Rule 3: the ledger is written before the results, so a
                    # crash between the two leaves a detectable
                    # done-without-results row rather than silent data loss.
                    store.mark_item(ctx.conn, run_id, item.id, "done")
                    store.write_results(
                        ctx.conn,
                        [
                            store.ResultRow(
                                stage_run_id=run_id,
                                compound_id=r.compound_id,
                                metric=r.metric,
                                value_num=r.value_num,
                                value_text=r.value_text,
                            )
                            for r in results
                        ],
                    )
                    executed += 1
                    reporter.emit(ItemCompleted(stage=stage.name, item_id=item.id, ok=True))
                fill()
        except BaseException:
            # Interrupted: the stage_run stays 'running' and every unfinished
            # item stays un-done, so a re-run picks up exactly where this left
            # off. Nothing is rolled back.
            for future in in_flight:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            reporter.emit(
                Warning(text=f"{stage.name} interrupted; re-run the same command to resume")
            )
            raise
        else:
            executor.shutdown(wait=True)
        return executed, failed
