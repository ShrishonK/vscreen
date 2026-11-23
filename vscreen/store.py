"""SQLite store: connection, schema, and typed CRUD.

The schema is ``docs/SCHEMA.sql`` verbatim, shipped inside the package as
``vscreen/schema.sql`` so an installed wheel can create a database without the
repository present. ``tests/unit/test_schema.py`` asserts the two files are
byte-identical.

Side effects live here and in ``runners/`` only (``docs/CONVENTIONS.md``).
Storage is narrow: one ``results`` row per compound per metric per stage run
(rule 4). Never add a score column to ``compounds``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from .config import DB_NAME, utc_now_iso
from .errors import StoreError

__all__ = [
    "Target",
    "Compound",
    "CompoundSet",
    "Study",
    "StageRun",
    "ResultRow",
    "StageItem",
    "ItemStatus",
    "RunStatus",
    "schema_sql",
    "connect",
    "init_schema",
    "open_store",
    "transaction",
    "utc_now_iso",
]

log = logging.getLogger(__name__)

ItemStatus = Literal["pending", "running", "done", "failed", "skipped"]
RunStatus = Literal["pending", "running", "complete", "failed"]


# --------------------------------------------------------------------------- #
# Row types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Target:
    id: str
    source_kind: str
    box_json: str
    receptor_path: str
    has_cognate_ligand: int
    provenance_json: str
    uniprot: str | None = None
    pdb_id: str | None = None
    sequence_sha: str | None = None
    conformation: str | None = None
    cognate_ligand_path: str | None = None
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class Compound:
    id: str
    inchikey: str
    smiles_input: str
    parse_ok: int
    smiles_canonical: str | None = None
    parse_note: str | None = None
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class CompoundSet:
    id: str
    kind: str
    provenance_json: str
    target_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class Study:
    id: str
    recovery_tier: str
    validation_reported: str
    doi: str | None = None
    year: int | None = None
    venue: str | None = None
    venue_kind: str | None = None
    model_class: str | None = None
    target_id: str | None = None
    authors_pdb: str | None = None
    n_proposed: int | None = None
    recovery_source: str | None = None
    synthesis_reported: int | None = None
    code_available: str | None = None
    notes: str | None = None
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class StageRun:
    id: str
    stage: str
    run_key: str
    config_json: str
    tool_versions_json: str
    status: str
    target_id: str | None = None
    set_id: str | None = None
    error_text: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class ResultRow:
    stage_run_id: str
    compound_id: str
    metric: str
    value_num: float | None = None
    value_text: str | None = None


@dataclass(frozen=True)
class StageItem:
    stage_run_id: str
    item_id: str
    status: str
    attempts: int
    error_text: str | None
    updated_at: str


# --------------------------------------------------------------------------- #
# Connection and schema
# --------------------------------------------------------------------------- #


def schema_sql() -> str:
    """The packaged copy of ``docs/SCHEMA.sql``, used verbatim."""
    try:
        return resources.files("vscreen").joinpath("schema.sql").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:  # pragma: no cover
        raise StoreError(
            "Packaged schema.sql is missing. Reinstall vscreen (pip install -e .) "
            "or copy docs/SCHEMA.sql to vscreen/schema.sql."
        ) from exc


def connect(db_path: Path, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a connection with WAL and foreign keys on, as the schema requires."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(db_path, timeout=timeout, isolation_level=None)
    except sqlite3.Error as exc:
        raise StoreError(f"Could not open database {db_path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        # Some network and DrvFS mounts refuse WAL. Not a correctness problem,
        # but concurrent readers during a run will block, so say so once.
        log.warning("WAL journal mode unavailable for %s (mode=%s)", db_path, mode)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply ``docs/SCHEMA.sql``. Idempotent — every statement is IF NOT EXISTS."""
    try:
        conn.executescript(schema_sql())
    except sqlite3.Error as exc:
        raise StoreError(f"Could not initialise schema: {exc}") from exc


@contextmanager
def open_store(project_dir: Path, *, db_name: str = DB_NAME) -> Iterator[sqlite3.Connection]:
    """Open ``<project_dir>/vscreen.db``, initialise the schema, close on exit."""
    conn = connect(Path(project_dir) / db_name)
    try:
        init_schema(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One explicit transaction. Rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _insert(conn: sqlite3.Connection, table: str, row: Any, *, replace: bool = False) -> None:
    data = {k: v for k, v in vars(row).items()}
    columns = ", ".join(data)
    placeholders = ", ".join(f":{k}" for k in data)
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    try:
        conn.execute(f"{verb} INTO {table} ({columns}) VALUES ({placeholders})", data)
    except sqlite3.IntegrityError as exc:
        raise StoreError(f"Could not write {table} row {getattr(row, 'id', '')!r}: {exc}") from exc


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #


def insert_target(conn: sqlite3.Connection, target: Target, *, replace: bool = False) -> Target:
    _insert(conn, "targets", target, replace=replace)
    return target


def get_target(conn: sqlite3.Connection, target_id: str) -> Target | None:
    row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
    return Target(**dict(row)) if row else None


def list_targets(conn: sqlite3.Connection) -> list[Target]:
    rows = conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
    return [Target(**dict(r)) for r in rows]


# --------------------------------------------------------------------------- #
# compounds
# --------------------------------------------------------------------------- #


def insert_compound(
    conn: sqlite3.Connection, compound: Compound, *, replace: bool = False
) -> Compound:
    _insert(conn, "compounds", compound, replace=replace)
    return compound


def insert_compounds(conn: sqlite3.Connection, compounds: Iterable[Compound]) -> int:
    """Bulk insert, ignoring compounds already present (same id)."""
    rows = list(compounds)
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO compounds "
        "(id, inchikey, smiles_input, smiles_canonical, parse_ok, parse_note, created_at) "
        "VALUES (:id, :inchikey, :smiles_input, :smiles_canonical, :parse_ok, :parse_note, "
        ":created_at)",
        [vars(c) for c in rows],
    )
    return len(rows)


def get_compound(conn: sqlite3.Connection, compound_id: str) -> Compound | None:
    row = conn.execute("SELECT * FROM compounds WHERE id = ?", (compound_id,)).fetchone()
    return Compound(**dict(row)) if row else None


def list_compounds(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Compound]:
    sql = "SELECT * FROM compounds ORDER BY id"
    rows = conn.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    return [Compound(**dict(r)) for r in rows]


def find_compounds_by_inchikey(conn: sqlite3.Connection, inchikey: str) -> list[Compound]:
    rows = conn.execute(
        "SELECT * FROM compounds WHERE inchikey = ? ORDER BY id", (inchikey,)
    ).fetchall()
    return [Compound(**dict(r)) for r in rows]


def count_compounds(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM compounds").fetchone()[0])


# --------------------------------------------------------------------------- #
# compound_sets and set_members
# --------------------------------------------------------------------------- #


def insert_compound_set(
    conn: sqlite3.Connection, compound_set: CompoundSet, *, replace: bool = False
) -> CompoundSet:
    _insert(conn, "compound_sets", compound_set, replace=replace)
    return compound_set


def get_compound_set(conn: sqlite3.Connection, set_id: str) -> CompoundSet | None:
    row = conn.execute("SELECT * FROM compound_sets WHERE id = ?", (set_id,)).fetchone()
    return CompoundSet(**dict(row)) if row else None


def list_compound_sets(conn: sqlite3.Connection) -> list[CompoundSet]:
    rows = conn.execute("SELECT * FROM compound_sets ORDER BY id").fetchall()
    return [CompoundSet(**dict(r)) for r in rows]


def add_set_members(conn: sqlite3.Connection, set_id: str, compound_ids: Iterable[str]) -> int:
    ids = [(set_id, c) for c in compound_ids]
    if not ids:
        return 0
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO set_members (set_id, compound_id) VALUES (?, ?)", ids
        )
    except sqlite3.IntegrityError as exc:
        raise StoreError(f"Could not add members to set {set_id!r}: {exc}") from exc
    return len(ids)


def list_set_members(conn: sqlite3.Connection, set_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT compound_id FROM set_members WHERE set_id = ? ORDER BY compound_id", (set_id,)
    ).fetchall()
    return [r[0] for r in rows]


def count_set_members(conn: sqlite3.Connection, set_id: str) -> int:
    return int(
        conn.execute("SELECT count(*) FROM set_members WHERE set_id = ?", (set_id,)).fetchone()[0]
    )


def remove_set_member(conn: sqlite3.Connection, set_id: str, compound_id: str) -> None:
    conn.execute(
        "DELETE FROM set_members WHERE set_id = ? AND compound_id = ?", (set_id, compound_id)
    )


# --------------------------------------------------------------------------- #
# studies (audit workflow, Paper 1)
# --------------------------------------------------------------------------- #


def insert_study(conn: sqlite3.Connection, study: Study, *, replace: bool = False) -> Study:
    _insert(conn, "studies", study, replace=replace)
    return study


def get_study(conn: sqlite3.Connection, study_id: str) -> Study | None:
    row = conn.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
    return Study(**dict(row)) if row else None


def list_studies(conn: sqlite3.Connection) -> list[Study]:
    rows = conn.execute("SELECT * FROM studies ORDER BY id").fetchall()
    return [Study(**dict(r)) for r in rows]


# --------------------------------------------------------------------------- #
# stage_runs
# --------------------------------------------------------------------------- #


def insert_stage_run(conn: sqlite3.Connection, run: StageRun) -> StageRun:
    if not run.tool_versions_json or run.tool_versions_json in ("{}", "null"):
        # Rule 5: a result without recorded tool versions is not publishable.
        raise StoreError(
            f"Refusing to create stage_run for {run.stage!r} with empty tool_versions_json. "
            f"Record the version of every external tool the stage calls (rule 5)."
        )
    _insert(conn, "stage_runs", run)
    return run


def get_stage_run(conn: sqlite3.Connection, run_id: str) -> StageRun | None:
    row = conn.execute("SELECT * FROM stage_runs WHERE id = ?", (run_id,)).fetchone()
    return StageRun(**dict(row)) if row else None


def get_stage_run_by_key(conn: sqlite3.Connection, run_key: str) -> StageRun | None:
    row = conn.execute("SELECT * FROM stage_runs WHERE run_key = ?", (run_key,)).fetchone()
    return StageRun(**dict(row)) if row else None


def list_stage_runs(conn: sqlite3.Connection, *, stage: str | None = None) -> list[StageRun]:
    if stage is None:
        rows = conn.execute(
            "SELECT * FROM stage_runs ORDER BY coalesce(started_at, ''), stage"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM stage_runs WHERE stage = ? ORDER BY coalesce(started_at, '')", (stage,)
        ).fetchall()
    return [StageRun(**dict(r)) for r in rows]


def update_stage_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: RunStatus | None = None,
    error_text: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    sets: list[str] = []
    params: dict[str, Any] = {"id": run_id}
    for name, value in (
        ("status", status),
        ("error_text", error_text),
        ("started_at", started_at),
        ("finished_at", finished_at),
    ):
        if value is not None:
            sets.append(f"{name} = :{name}")
            params[name] = value
    if not sets:
        return
    cursor = conn.execute(f"UPDATE stage_runs SET {', '.join(sets)} WHERE id = :id", params)
    if cursor.rowcount == 0:
        raise StoreError(f"No stage_run with id {run_id!r} to update")


# --------------------------------------------------------------------------- #
# results (narrow storage, rule 4)
# --------------------------------------------------------------------------- #


def write_results(conn: sqlite3.Connection, results: Iterable[ResultRow]) -> int:
    rows = list(results)
    if not rows:
        return 0
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO results "
            "(stage_run_id, compound_id, metric, value_num, value_text) "
            "VALUES (:stage_run_id, :compound_id, :metric, :value_num, :value_text)",
            [vars(r) for r in rows],
        )
    except sqlite3.IntegrityError as exc:
        raise StoreError(
            f"Could not write {len(rows)} result rows: {exc}. "
            f"Every result needs an existing compounds row and stage_runs row."
        ) from exc
    return len(rows)


def list_results(
    conn: sqlite3.Connection,
    *,
    stage_run_id: str | None = None,
    compound_id: str | None = None,
    metric: str | None = None,
) -> list[ResultRow]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for name, value in (
        ("stage_run_id", stage_run_id),
        ("compound_id", compound_id),
        ("metric", metric),
    ):
        if value is not None:
            clauses.append(f"{name} = :{name}")
            params[name] = value
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM results{where} ORDER BY compound_id, metric", params
    ).fetchall()
    return [ResultRow(**dict(r)) for r in rows]


def count_results(conn: sqlite3.Connection, *, stage_run_id: str | None = None) -> int:
    if stage_run_id is None:
        return int(conn.execute("SELECT count(*) FROM results").fetchone()[0])
    return int(
        conn.execute(
            "SELECT count(*) FROM results WHERE stage_run_id = ?", (stage_run_id,)
        ).fetchone()[0]
    )


# --------------------------------------------------------------------------- #
# stage_items (the resume ledger, rule 3)
# --------------------------------------------------------------------------- #


def mark_item(
    conn: sqlite3.Connection,
    stage_run_id: str,
    item_id: str,
    status: ItemStatus,
    *,
    error_text: str | None = None,
    increment_attempts: bool = False,
) -> None:
    """Upsert one item's ledger row. Attempts accumulate across resumes."""
    conn.execute(
        """
        INSERT INTO stage_items (stage_run_id, item_id, status, attempts, error_text, updated_at)
        VALUES (:stage_run_id, :item_id, :status, :attempts, :error_text, :updated_at)
        ON CONFLICT(stage_run_id, item_id) DO UPDATE SET
            status     = :status,
            attempts   = attempts + :attempts,
            error_text = :error_text,
            updated_at = :updated_at
        """,
        {
            "stage_run_id": stage_run_id,
            "item_id": item_id,
            "status": status,
            "attempts": 1 if increment_attempts else 0,
            "error_text": error_text,
            "updated_at": utc_now_iso(),
        },
    )


def get_item(conn: sqlite3.Connection, stage_run_id: str, item_id: str) -> StageItem | None:
    row = conn.execute(
        "SELECT * FROM stage_items WHERE stage_run_id = ? AND item_id = ?",
        (stage_run_id, item_id),
    ).fetchone()
    return StageItem(**dict(row)) if row else None


def list_items(
    conn: sqlite3.Connection, stage_run_id: str, *, status: ItemStatus | None = None
) -> list[StageItem]:
    if status is None:
        rows = conn.execute(
            "SELECT * FROM stage_items WHERE stage_run_id = ? ORDER BY item_id", (stage_run_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM stage_items WHERE stage_run_id = ? AND status = ? ORDER BY item_id",
            (stage_run_id, status),
        ).fetchall()
    return [StageItem(**dict(r)) for r in rows]


def done_item_ids(conn: sqlite3.Connection, stage_run_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT item_id FROM stage_items WHERE stage_run_id = ? AND status = 'done'",
        (stage_run_id,),
    ).fetchall()
    return {r[0] for r in rows}


def item_status_counts(conn: sqlite3.Connection, stage_run_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, count(*) FROM stage_items WHERE stage_run_id = ? GROUP BY status",
        (stage_run_id,),
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def items_done_without_results(conn: sqlite3.Connection, stage_run_id: str) -> list[str]:
    """Items the ledger calls done that wrote no results.

    The ledger is written before results precisely so this is detectable
    (``docs/ARCHITECTURE.md``, rule 3); an empty list is the healthy case.
    """
    rows = conn.execute(
        """
        SELECT i.item_id FROM stage_items i
        LEFT JOIN results r
          ON r.stage_run_id = i.stage_run_id AND r.compound_id = i.item_id
        WHERE i.stage_run_id = ? AND i.status = 'done' AND r.compound_id IS NULL
        ORDER BY i.item_id
        """,
        (stage_run_id,),
    ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# Read-only summaries for the status command
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StageRunSummary:
    run: StageRun
    items_total: int
    items_done: int
    items_failed: int
    results: int


def stage_run_summaries(conn: sqlite3.Connection) -> list[StageRunSummary]:
    summaries: list[StageRunSummary] = []
    for run in list_stage_runs(conn):
        counts = item_status_counts(conn, run.id)
        summaries.append(
            StageRunSummary(
                run=run,
                items_total=sum(counts.values()),
                items_done=counts.get("done", 0),
                items_failed=counts.get("failed", 0),
                results=count_results(conn, stage_run_id=run.id),
            )
        )
    return summaries


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables: Sequence[str] = (
        "targets",
        "compounds",
        "compound_sets",
        "set_members",
        "studies",
        "stage_runs",
        "results",
        "stage_items",
    )
    return {t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]) for t in tables}
