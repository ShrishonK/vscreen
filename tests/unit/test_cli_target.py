"""The `vscreen target` subcommands end-to-end against the fixture cache.

These tests go through the typer CLI — they ensure the user-visible
rendering is right, the box-method flags wire through to ``TargetAddPlan``,
and the no-cognate-ligand warning surfaces prominently (``docs/PHASES.md``).
Everything is served from the on-disk cache primed with fixture payloads,
so no network call is possible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import FIXTURES_ROOT
from vscreen import store
from vscreen.cli import app
from vscreen.fetch import pdb as pdb_fetch
from vscreen.fetch import uniprot as uniprot_fetch
from vscreen.fetch.cache import DiskCache, offline_fetcher

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})

TINY_HOLO = FIXTURES_ROOT / "structures" / "tiny_holo.pdb"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", "egfr-run", "--in", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path / "egfr-run"


def _prime_fetch_cache(project_dir: Path) -> None:
    """Fill the project's cache/ directory with every fixture Phase 1 touches."""
    cache = DiskCache(project_dir / "cache", fetcher=offline_fetcher)

    cache.put_text(
        uniprot_fetch.SEARCH_URL,
        (FIXTURES_ROOT / "uniprot" / "search_EGFR.json").read_text(encoding="utf-8"),
        params=uniprot_fetch.search_params("EGFR"),
    )
    cache.put_text(
        uniprot_fetch.ENTRY_URL.format(accession="P00533"),
        (FIXTURES_ROOT / "uniprot" / "get_P00533.json").read_text(encoding="utf-8"),
        params={"format": "json"},
    )
    search_body = pdb_fetch.search_query_for_uniprot("P00533")
    cache.put_text(
        pdb_fetch.SEARCH_URL,
        (FIXTURES_ROOT / "rcsb" / "search_P00533.json").read_text(encoding="utf-8"),
        method="POST",
        body=json.dumps(search_body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    graphql_body = pdb_fetch.entries_graphql_query(["4I22", "1M17"])
    cache.put_text(
        pdb_fetch.GRAPHQL_URL,
        (FIXTURES_ROOT / "rcsb" / "graphql_entries.json").read_text(encoding="utf-8"),
        method="POST",
        body=json.dumps(graphql_body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    cache.put_text(
        "https://files.rcsb.org/download/4I22.pdb",
        TINY_HOLO.read_text(encoding="utf-8"),
    )


# --------------------------------------------------------------------------- #
# search / structures
# --------------------------------------------------------------------------- #


def test_target_search_prints_P00533(project: Path) -> None:
    _prime_fetch_cache(project)
    result = runner.invoke(app, ["target", "search", "EGFR", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "P00533" in result.output
    assert "Epidermal growth factor receptor" in result.output


def test_target_structures_flags_ligand_bound_vs_apo(project: Path) -> None:
    _prime_fetch_cache(project)
    result = runner.invoke(
        app, ["target", "structures", "P00533", "--project", str(project)]
    )
    assert result.exit_code == 0, result.output
    assert "4I22" in result.output
    assert "1M17" in result.output
    assert "ADP" in result.output, "the holo entry's ligand must be listed"
    assert "apo" in result.output, "the apo entry must be flagged as such"


# --------------------------------------------------------------------------- #
# target add — from a PDB download + box from ligand (acceptance check 3)
# --------------------------------------------------------------------------- #


def test_target_add_pdb_with_box_from_ligand_writes_every_artifact(project: Path) -> None:
    _prime_fetch_cache(project)
    result = runner.invoke(
        app,
        [
            "target",
            "add",
            "--pdb",
            "4I22",
            "--box-from-ligand",
            "--plain",
            "--project",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "target 4i22 added" in result.output

    target_dir = project / "targets" / "4i22"
    assert (target_dir / "receptor.pdbqt").is_file()
    assert (target_dir / "box.json").is_file()
    assert (target_dir / "provenance.json").is_file()
    assert (target_dir / "cognate_ligand.pdb").is_file()

    box = json.loads((target_dir / "box.json").read_text(encoding="utf-8"))
    assert set(box) == {"center", "size", "method"}
    assert box["method"].startswith("cognate-ligand")

    provenance = json.loads((target_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["pdb_id"] == "4I22"
    assert provenance["chain"] in {"A", "B"}
    assert provenance["source"]["origin"] == "rcsb"
    assert "warning" not in provenance, "holo target must not carry the no-ligand warning"


def test_target_add_rejects_box_from_ligand_when_apo_chain_picked(project: Path) -> None:
    """The stage marks the item failed; the CLI prints status 'failed' for the run."""
    _prime_fetch_cache(project)
    result = runner.invoke(
        app,
        [
            "target",
            "add",
            "--file",
            str(TINY_HOLO),
            "--chain",
            "B",  # Chain B in the fixture has no cognate ligand.
            "--box-from-ligand",
            "--plain",
            "--id",
            "tiny-apo",
            "--project",
            str(project),
        ],
    )
    # The stage runner records the failure; the CLI still exits 0 because the
    # run completed (with a failed item). No target row is written.
    assert "FAILED" in result.output or "failed" in result.output
    with store.open_store(project) as conn:
        assert store.get_target(conn, "tiny-apo") is None


def test_target_add_without_a_box_mode_fails_fast(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "target",
            "add",
            "--file",
            str(TINY_HOLO),
            "--plain",
            "--project",
            str(project),
        ],
    )
    assert result.exit_code == 1
    assert "docking box" in result.output


def test_target_add_rejects_two_input_modes(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "target",
            "add",
            "--pdb",
            "4I22",
            "--file",
            str(TINY_HOLO),
            "--box-from-ligand",
            "--project",
            str(project),
        ],
    )
    assert result.exit_code == 1
    assert "exactly one of" in result.output


# --------------------------------------------------------------------------- #
# target show — surfaces the no-cognate-ligand warning prominently
# --------------------------------------------------------------------------- #


def test_target_show_renders_apo_warning_prominently(project: Path, tmp_path: Path) -> None:
    """A FASTA-derived target shows `COGNATE LIGAND MISSING` loudly."""
    fasta = tmp_path / "egfr.fasta"
    fasta.write_text(">EGFR\nMRPSGAA\n", encoding="utf-8")
    add_result = runner.invoke(
        app,
        [
            "target",
            "add",
            "--fasta",
            str(fasta),
            "--id",
            "egfr-fold",
            "--plain",
            "--project",
            str(project),
        ],
    )
    assert add_result.exit_code == 0, add_result.output
    assert "folding required" in add_result.output

    show = runner.invoke(
        app, ["target", "show", "egfr-fold", "--project", str(project)]
    )
    assert show.exit_code == 0, show.output
    assert "COGNATE LIGAND MISSING" in show.output
    # The warning text itself appears verbatim.
    assert "re-docking gate cannot run" in show.output


def test_target_show_on_missing_target_hints_at_target_list(project: Path) -> None:
    result = runner.invoke(
        app, ["target", "show", "ghost", "--project", str(project)]
    )
    assert result.exit_code == 1
    assert "vscreen target list" in result.output


def test_target_list_shows_cognate_flag(project: Path) -> None:
    _prime_fetch_cache(project)
    runner.invoke(
        app,
        [
            "target",
            "add",
            "--pdb",
            "4I22",
            "--box-from-ligand",
            "--plain",
            "--project",
            str(project),
        ],
    )
    result = runner.invoke(app, ["target", "list", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "4i22" in result.output
    assert "yes" in result.output.lower()


def test_target_list_empty_says_so(project: Path) -> None:
    result = runner.invoke(app, ["target", "list", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "no targets" in result.output
