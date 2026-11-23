"""Store CRUD, the invariants the schema enforces, and the resume ledger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.conftest import make_compound, make_stage_run, make_target
from vscreen import store
from vscreen.config import DB_NAME
from vscreen.errors import StoreError

# --------------------------------------------------------------------------- #
# connection and schema
# --------------------------------------------------------------------------- #


def test_connect_enables_foreign_keys_and_wal(project_dir: Path) -> None:
    conn = store.connect(project_dir / DB_NAME)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_init_schema_is_idempotent(conn: sqlite3.Connection) -> None:
    store.init_schema(conn)
    store.init_schema(conn)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {
        "targets",
        "compounds",
        "compound_sets",
        "set_members",
        "studies",
        "stage_runs",
        "results",
        "stage_items",
    } <= tables


def test_open_store_creates_the_database(project_dir: Path) -> None:
    with store.open_store(project_dir) as conn:
        assert store.table_counts(conn)["compounds"] == 0
    assert (project_dir / DB_NAME).is_file()


# --------------------------------------------------------------------------- #
# targets, compounds, sets, studies
# --------------------------------------------------------------------------- #


def test_target_crud(conn: sqlite3.Connection) -> None:
    target = make_target("egfr-4i22")
    store.insert_target(conn, target)
    assert store.get_target(conn, "egfr-4i22") == target
    assert store.list_targets(conn) == [target]
    assert store.get_target(conn, "absent") is None
    with pytest.raises(StoreError, match="egfr-4i22"):
        store.insert_target(conn, target)
    store.insert_target(conn, make_target("egfr-4i22", conformation="DFG-out"), replace=True)
    assert store.get_target(conn, "egfr-4i22").conformation == "DFG-out"  # type: ignore[union-attr]


def test_compound_crud_and_parse_failures_are_data_points(conn: sqlite3.Connection) -> None:
    ok = make_compound("c1")
    bad = make_compound(
        "c2", smiles_canonical=None, parse_ok=0, parse_note="RDKit: unkekulizable ring"
    )
    store.insert_compound(conn, ok)
    store.insert_compound(conn, bad)
    assert store.count_compounds(conn) == 2
    assert store.get_compound(conn, "c2").parse_note == "RDKit: unkekulizable ring"  # type: ignore[union-attr]
    assert [c.id for c in store.list_compounds(conn)] == ["c1", "c2"]
    assert [c.id for c in store.find_compounds_by_inchikey(conn, ok.inchikey)] == ["c1", "c2"]


def test_insert_compounds_ignores_duplicates(conn: sqlite3.Connection) -> None:
    store.insert_compounds(conn, [make_compound("c1"), make_compound("c2")])
    store.insert_compounds(conn, [make_compound("c2"), make_compound("c3")])
    assert store.count_compounds(conn) == 3
    assert store.insert_compounds(conn, []) == 0


def test_compound_set_members(conn: sqlite3.Connection) -> None:
    store.insert_target(conn, make_target("t1"))
    store.insert_compounds(conn, [make_compound(f"c{i}") for i in range(1, 4)])
    cset = store.CompoundSet(
        id="actives:P00533", kind="actives", target_id="t1", provenance_json='{"seed":1}'
    )
    store.insert_compound_set(conn, cset)
    assert store.get_compound_set(conn, "actives:P00533") == cset
    assert store.list_compound_sets(conn) == [cset]

    store.add_set_members(conn, "actives:P00533", ["c1", "c2", "c2"])
    assert store.list_set_members(conn, "actives:P00533") == ["c1", "c2"]
    assert store.count_set_members(conn, "actives:P00533") == 2
    store.remove_set_member(conn, "actives:P00533", "c1")
    assert store.list_set_members(conn, "actives:P00533") == ["c2"]
    assert store.add_set_members(conn, "actives:P00533", []) == 0


def test_set_membership_requires_existing_rows(conn: sqlite3.Connection) -> None:
    store.insert_compound_set(
        conn, store.CompoundSet(id="corpus", kind="corpus", provenance_json="{}")
    )
    with pytest.raises(StoreError):
        store.add_set_members(conn, "corpus", ["ghost"])
    with pytest.raises(StoreError):
        store.insert_compound_set(
            conn, store.CompoundSet(id="x", kind="actives", target_id="ghost", provenance_json="{}")
        )


def test_study_crud(conn: sqlite3.Connection) -> None:
    study = store.Study(
        id="omega26",
        recovery_tier="tier1",
        validation_reported="none",
        doi="10.1000/xyz",
        year=2023,
        venue_kind="preprint",
        model_class="diffusion",
        n_proposed=26,
        recovery_source="Supplementary Table S3",
        code_available="public",
    )
    store.insert_study(conn, study)
    assert store.get_study(conn, "omega26") == study
    assert store.list_studies(conn) == [study]


# --------------------------------------------------------------------------- #
# stage_runs: rule 5 and the unique run key
# --------------------------------------------------------------------------- #


def test_stage_run_crud_and_update(conn: sqlite3.Connection) -> None:
    run = make_stage_run("r1")
    store.insert_stage_run(conn, run)
    assert store.get_stage_run(conn, "r1") == run
    assert store.get_stage_run_by_key(conn, "key-r1") == run
    assert store.list_stage_runs(conn, stage="demo") == [run]
    assert store.list_stage_runs(conn, stage="dock") == []

    store.update_stage_run(conn, "r1", status="complete", finished_at="2026-01-01T00:00:00+00:00")
    updated = store.get_stage_run(conn, "r1")
    assert updated is not None and updated.status == "complete"
    assert updated.finished_at == "2026-01-01T00:00:00+00:00"
    store.update_stage_run(conn, "r1")  # no fields: a harmless no-op
    with pytest.raises(StoreError, match="No stage_run"):
        store.update_stage_run(conn, "ghost", status="failed")


def test_stage_run_requires_tool_versions(conn: sqlite3.Connection) -> None:
    for empty in ("", "{}", "null"):
        with pytest.raises(StoreError, match="tool_versions"):
            store.insert_stage_run(conn, make_stage_run("r1", tool_versions_json=empty))
    assert store.list_stage_runs(conn) == []


def test_run_key_is_unique(conn: sqlite3.Connection) -> None:
    store.insert_stage_run(conn, make_stage_run("r1"))
    with pytest.raises(StoreError):
        store.insert_stage_run(conn, make_stage_run("r2", run_key="key-r1"))


# --------------------------------------------------------------------------- #
# results: narrow storage, history preserved
# --------------------------------------------------------------------------- #


def test_results_are_narrow_and_history_is_preserved(conn: sqlite3.Connection) -> None:
    store.insert_compounds(conn, [make_compound("c1"), make_compound("c2")])
    store.insert_stage_run(conn, make_stage_run("r1"))
    store.insert_stage_run(conn, make_stage_run("r2"))

    store.write_results(
        conn,
        [
            store.ResultRow("r1", "c1", "vina_score", value_num=-9.4),
            store.ResultRow("r1", "c1", "pb_pass_all", value_num=1),
            store.ResultRow("r1", "c2", "vina_score", value_num=-7.1),
        ],
    )
    # A re-run with different settings is a second stage_run, not an overwrite.
    store.write_results(conn, [store.ResultRow("r2", "c1", "vina_score", value_num=-10.2)])

    assert store.count_results(conn) == 4
    assert [
        r.value_num for r in store.list_results(conn, compound_id="c1", metric="vina_score")
    ] == [
        -9.4,
        -10.2,
    ]
    assert store.count_results(conn, stage_run_id="r1") == 3
    assert {r.metric for r in store.list_results(conn, stage_run_id="r1")} == {
        "vina_score",
        "pb_pass_all",
    }
    assert store.write_results(conn, []) == 0


def test_result_rewrite_within_one_run_replaces(conn: sqlite3.Connection) -> None:
    store.insert_compound(conn, make_compound("c1"))
    store.insert_stage_run(conn, make_stage_run("r1"))
    store.write_results(conn, [store.ResultRow("r1", "c1", "vina_score", value_num=-9.4)])
    store.write_results(conn, [store.ResultRow("r1", "c1", "vina_score", value_num=-9.9)])
    rows = store.list_results(conn, stage_run_id="r1")
    assert len(rows) == 1 and rows[0].value_num == -9.9


def test_results_require_a_compound_and_a_run(conn: sqlite3.Connection) -> None:
    store.insert_stage_run(conn, make_stage_run("r1"))
    with pytest.raises(StoreError, match="existing compounds row"):
        store.write_results(conn, [store.ResultRow("r1", "ghost", "vina_score", value_num=-1.0)])
    store.insert_compound(conn, make_compound("c1"))
    with pytest.raises(StoreError):
        store.write_results(conn, [store.ResultRow("ghost", "c1", "vina_score", value_num=-1.0)])


def test_deleting_a_stage_run_cascades(conn: sqlite3.Connection) -> None:
    store.insert_compound(conn, make_compound("c1"))
    store.insert_stage_run(conn, make_stage_run("r1"))
    store.write_results(conn, [store.ResultRow("r1", "c1", "vina_score", value_num=-9.4)])
    store.mark_item(conn, "r1", "c1", "done")
    conn.execute("DELETE FROM stage_runs WHERE id = 'r1'")
    assert store.count_results(conn) == 0
    assert store.list_items(conn, "r1") == []


# --------------------------------------------------------------------------- #
# stage_items: the resume ledger
# --------------------------------------------------------------------------- #


def test_mark_item_accumulates_attempts(conn: sqlite3.Connection) -> None:
    store.insert_stage_run(conn, make_stage_run("r1"))
    store.mark_item(conn, "r1", "c1", "running", increment_attempts=True)
    first = store.get_item(conn, "r1", "c1")
    assert first is not None and first.attempts == 1 and first.status == "running"

    store.mark_item(conn, "r1", "c1", "failed", error_text="vina exited 1")
    failed = store.get_item(conn, "r1", "c1")
    assert failed is not None and failed.attempts == 1 and failed.error_text == "vina exited 1"

    store.mark_item(conn, "r1", "c1", "running", increment_attempts=True)
    store.mark_item(conn, "r1", "c1", "done")
    done = store.get_item(conn, "r1", "c1")
    assert done is not None and done.attempts == 2 and done.status == "done"
    assert done.error_text is None


def test_item_queries(conn: sqlite3.Connection) -> None:
    store.insert_stage_run(conn, make_stage_run("r1"))
    store.mark_item(conn, "r1", "c1", "done")
    store.mark_item(conn, "r1", "c2", "done")
    store.mark_item(conn, "r1", "c3", "failed", error_text="boom")
    store.mark_item(conn, "r1", "c4", "pending")

    assert store.done_item_ids(conn, "r1") == {"c1", "c2"}
    assert store.item_status_counts(conn, "r1") == {"done": 2, "failed": 1, "pending": 1}
    assert [i.item_id for i in store.list_items(conn, "r1", status="failed")] == ["c3"]
    assert len(store.list_items(conn, "r1")) == 4
    assert store.get_item(conn, "r1", "ghost") is None


def test_items_done_without_results_is_detectable(conn: sqlite3.Connection) -> None:
    """The ledger is written before results precisely so this is visible."""
    store.insert_compounds(conn, [make_compound("c1"), make_compound("c2")])
    store.insert_stage_run(conn, make_stage_run("r1"))
    store.mark_item(conn, "r1", "c1", "done")
    store.write_results(conn, [store.ResultRow("r1", "c1", "demo_score", value_num=-5.0)])
    store.mark_item(conn, "r1", "c2", "done")  # crashed before writing results

    assert store.items_done_without_results(conn, "r1") == ["c2"]


# --------------------------------------------------------------------------- #
# transactions and summaries
# --------------------------------------------------------------------------- #


def test_transaction_rolls_back(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), store.transaction(conn):
        store.insert_compound(conn, make_compound("c1"))
        raise RuntimeError("abort")
    assert store.count_compounds(conn) == 0

    with store.transaction(conn):
        store.insert_compound(conn, make_compound("c1"))
    assert store.count_compounds(conn) == 1


def test_stage_run_summaries(conn: sqlite3.Connection) -> None:
    store.insert_compound(conn, make_compound("c1"))
    store.insert_stage_run(conn, make_stage_run("r1"))
    store.mark_item(conn, "r1", "c1", "done")
    store.mark_item(conn, "r1", "c2", "failed", error_text="boom")
    store.write_results(conn, [store.ResultRow("r1", "c1", "demo_score", value_num=-5.0)])

    (summary,) = store.stage_run_summaries(conn)
    assert summary.run.id == "r1"
    assert (summary.items_total, summary.items_done, summary.items_failed) == (2, 1, 1)
    assert summary.results == 1


def test_table_counts_covers_every_table(conn: sqlite3.Connection) -> None:
    assert set(store.table_counts(conn)) == {
        "targets",
        "compounds",
        "compound_sets",
        "set_members",
        "studies",
        "stage_runs",
        "results",
        "stage_items",
    }
    assert all(v == 0 for v in store.table_counts(conn).values())
