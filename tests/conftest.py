"""Shared fixtures. Unit tests touch no network and no external binaries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from vscreen import store, tools
from vscreen.config import (
    DB_NAME,
    PROJECT_CONFIG_NAME,
    PROJECT_SUBDIRS,
    ProjectConfig,
    load_config,
    save_config,
    utc_now_iso,
)
from vscreen.events import CollectingReporter
from vscreen.fetch.cache import DiskCache, offline_fetcher
from vscreen.stages.base import Context

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: any cache built from the env must refuse to fetch (rule: no network)."""
    monkeypatch.setenv("VSCREEN_OFFLINE", "1")


@pytest.fixture
def offline_cache(project_dir: Path) -> DiskCache:
    """A ``DiskCache`` wired to the offline fetcher. Tests prime it per call."""
    return DiskCache(project_dir / "cache", fetcher=offline_fetcher)


def load_fixture_json(relative_path: str) -> object:
    """Read a fixture JSON payload. Fixtures live in ``tests/fixtures/``."""
    return json.loads((FIXTURES_ROOT / relative_path).read_text(encoding="utf-8"))


def load_fixture_text(relative_path: str) -> str:
    return (FIXTURES_ROOT / relative_path).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_tool_cache() -> Iterator[None]:
    """Tool resolution is cached per process; never leak it between tests."""
    tools.clear_cache()
    yield
    tools.clear_cache()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A project directory with a config and the on-disk layout, no database."""
    root = tmp_path / "proj"
    root.mkdir()
    for sub in PROJECT_SUBDIRS:
        (root / sub).mkdir()
    save_config(
        ProjectConfig(name="test-project", created_at=utc_now_iso(), seed=7),
        root / PROJECT_CONFIG_NAME,
    )
    return root


@pytest.fixture
def conn(project_dir: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(project_dir / DB_NAME)
    store.init_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def reporter() -> CollectingReporter:
    return CollectingReporter()


@pytest.fixture
def ctx(project_dir: Path, conn: sqlite3.Connection, reporter: CollectingReporter) -> Context:
    return Context(
        project_dir=project_dir,
        config=load_config(project_dir / PROJECT_CONFIG_NAME),
        conn=conn,
        reporter=reporter,
    )


# --------------------------------------------------------------------------- #
# Row builders
# --------------------------------------------------------------------------- #


def make_compound(compound_id: str = "c1", **kwargs: object) -> store.Compound:
    defaults: dict[str, object] = {
        "id": compound_id,
        "inchikey": "AAAAAAAAAAAAAA-BBBBBBBBBB-N",
        "smiles_input": "CCO",
        "smiles_canonical": "CCO",
        "parse_ok": 1,
        "parse_note": None,
    }
    defaults.update(kwargs)
    return store.Compound(**defaults)  # type: ignore[arg-type]


def make_target(target_id: str = "t1", **kwargs: object) -> store.Target:
    defaults: dict[str, object] = {
        "id": target_id,
        "source_kind": "pdb",
        "pdb_id": "4I22",
        "box_json": '{"center":[1.0,2.0,3.0],"size":[20.0,20.0,20.0],"method":"test"}',
        "receptor_path": "targets/t1/receptor.pdbqt",
        "has_cognate_ligand": 1,
        "cognate_ligand_path": "targets/t1/cognate.sdf",
        "provenance_json": '{"source":"test"}',
    }
    defaults.update(kwargs)
    return store.Target(**defaults)  # type: ignore[arg-type]


def make_stage_run(run_id: str = "r1", **kwargs: object) -> store.StageRun:
    defaults: dict[str, object] = {
        "id": run_id,
        "stage": "demo",
        "run_key": f"key-{run_id}",
        "config_json": "{}",
        "tool_versions_json": '{"python":"3.11.0","vscreen":"0.1.0"}',
        "status": "running",
        "started_at": utc_now_iso(),
    }
    defaults.update(kwargs)
    return store.StageRun(**defaults)  # type: ignore[arg-type]
