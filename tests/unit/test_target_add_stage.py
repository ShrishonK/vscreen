"""TargetAddStage: has_cognate_ligand, warnings, provenance, FASTA fold path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import FIXTURES_ROOT
from vscreen import store
from vscreen.events import CollectingReporter
from vscreen.fetch.cache import DiskCache, offline_fetcher
from vscreen.stages.base import Context, StageRunner
from vscreen.stages.target_add import (
    NO_COGNATE_LIGAND_WARNING,
    CognateLigandMissing,
    TargetAddPlan,
    TargetAddStage,
)

TINY_HOLO = FIXTURES_ROOT / "structures" / "tiny_holo.pdb"


def _build_ctx(project_dir: Path, conn, reporter: CollectingReporter) -> Context:
    from vscreen.config import PROJECT_CONFIG_NAME, load_config

    return Context(
        project_dir=project_dir,
        config=load_config(project_dir / PROJECT_CONFIG_NAME),
        conn=conn,
        reporter=reporter,
    )


def _run(project_dir: Path, conn, plan: TargetAddPlan) -> Context:
    reporter = CollectingReporter()
    ctx = _build_ctx(project_dir, conn, reporter)
    cache = DiskCache(project_dir / "cache", fetcher=offline_fetcher)
    stage = TargetAddStage(plan, cache=cache)
    StageRunner().run(stage, ctx)
    return ctx


def test_target_add_file_mode_writes_receptor_and_target_row(
    project_dir: Path, conn
) -> None:
    plan = TargetAddPlan(
        target_id="tiny-holo",
        source_kind="file",
        input_file=TINY_HOLO,
        chain="A",
        box_method="from-ligand",
        box_padding_a=8.0,
    )
    _run(project_dir, conn, plan)
    target = store.get_target(conn, "tiny-holo")
    assert target is not None
    assert target.has_cognate_ligand == 1
    receptor_path = project_dir / target.receptor_path
    assert receptor_path.is_file()
    assert receptor_path.read_text().startswith("ATOM")
    assert target.cognate_ligand_path is not None
    assert (project_dir / target.cognate_ligand_path).is_file()
    # Provenance has the explicit fields the convention requires.
    provenance = json.loads(target.provenance_json)
    assert provenance["chain"] == "A"
    assert provenance["source_kind"] == "file"
    assert "box" in provenance and provenance["box"]["method"].startswith("cognate-ligand")
    assert provenance["has_cognate_ligand"] is True
    # No warning on a holo target.
    assert "warning" not in provenance


def test_target_add_chain_without_ligand_sets_has_cognate_ligand_false(
    project_dir: Path, conn
) -> None:
    plan = TargetAddPlan(
        target_id="tiny-apo-b",
        source_kind="file",
        input_file=TINY_HOLO,
        chain="B",  # Chain B in the fixture has no ligand.
        box_method="explicit",
        box_center=(30.0, 30.0, 30.0),
        box_size=(16.0, 16.0, 16.0),
    )
    _run(project_dir, conn, plan)
    target = store.get_target(conn, "tiny-apo-b")
    assert target is not None
    assert target.has_cognate_ligand == 0
    provenance = json.loads(target.provenance_json)
    assert provenance["warning"] == NO_COGNATE_LIGAND_WARNING


def test_target_add_rejects_box_from_ligand_without_a_ligand(
    project_dir: Path, conn
) -> None:
    plan = TargetAddPlan(
        target_id="apo-nobox",
        source_kind="file",
        input_file=TINY_HOLO,
        chain="B",
        box_method="from-ligand",
    )
    # The stage runner catches the exception and marks the item failed; the
    # explicit intent of this test is that the typed exception is raised.
    reporter = CollectingReporter()
    ctx = _build_ctx(project_dir, conn, reporter)
    cache = DiskCache(project_dir / "cache", fetcher=offline_fetcher)
    stage = TargetAddStage(plan, cache=cache)
    with pytest.raises(CognateLigandMissing):
        stage.run_item(ctx, next(iter(stage.iter_items(ctx))))


def test_target_add_fasta_mode_emits_notebook_and_records_warning(
    project_dir: Path, conn, tmp_path
) -> None:
    fasta_path = tmp_path / "egfr.fasta"
    fasta_path.write_text(">EGFR_kinase\nMRPSGAAA\n", encoding="utf-8")
    plan = TargetAddPlan(
        target_id="egfr-fold",
        source_kind="fasta",
        input_file=fasta_path,
    )
    _run(project_dir, conn, plan)

    target = store.get_target(conn, "egfr-fold")
    assert target is not None
    assert target.source_kind == "fasta"
    assert target.has_cognate_ligand == 0, "a FASTA-derived target cannot have a cognate ligand"
    provenance = json.loads(target.provenance_json)
    assert provenance["warning"] == NO_COGNATE_LIGAND_WARNING
    assert provenance["status"] == "pending-fold"
    # Notebook exists and is valid JSON.
    notebook_path = project_dir / "targets" / "egfr-fold" / "folding.ipynb"
    assert notebook_path.is_file()
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    assert notebook.get("nbformat") == 4
    # The copied sequence file is beside the notebook.
    assert (project_dir / "targets" / "egfr-fold" / "input.fasta").is_file()


def test_target_add_is_resumable_at_item_granularity(
    project_dir: Path, conn
) -> None:
    plan = TargetAddPlan(
        target_id="tiny-resume",
        source_kind="file",
        input_file=TINY_HOLO,
        chain="A",
        box_method="from-ligand",
    )
    _run(project_dir, conn, plan)
    # A second run should be a no-op: the one item is already done, so the
    # stage reports zero executed this run.
    reporter = CollectingReporter()
    ctx = _build_ctx(project_dir, conn, reporter)
    cache = DiskCache(project_dir / "cache", fetcher=offline_fetcher)
    outcome = StageRunner().run(TargetAddStage(plan, cache=cache), ctx)
    assert outcome.executed == 0
    assert outcome.done == 1
