"""The command line: init, status, demo. Rendering only — the logic is tested
in the modules the commands call."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vscreen import store
from vscreen.cli import app
from vscreen.config import (
    DB_NAME,
    PROJECT_CONFIG_NAME,
    PROJECT_SUBDIRS,
    load_config,
)

runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})


@pytest.fixture
def project(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", "demo-run", "--in", str(tmp_path), "--seed", "11"])
    assert result.exit_code == 0, result.output
    return tmp_path / "demo-run"


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def test_init_creates_the_project_layout(project: Path) -> None:
    assert (project / PROJECT_CONFIG_NAME).is_file()
    assert (project / DB_NAME).is_file()
    for sub in PROJECT_SUBDIRS:
        assert (project / sub).is_dir(), sub

    config = load_config(project / PROJECT_CONFIG_NAME)
    assert config.name == "demo-run"
    assert config.seed == 11
    assert config.targets == [] and config.sets == [] and config.stages == {}

    with store.open_store(project) as conn:
        assert all(count == 0 for count in store.table_counts(conn).values())


def test_init_refuses_to_overwrite(project: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "demo-run", "--in", str(tmp_path)])
    assert result.exit_code == 1
    assert "already exists" in result.output


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_prints_an_empty_table_for_a_fresh_project(project: Path) -> None:
    result = runner.invoke(app, ["status", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "demo-run" in result.output
    assert "contents" in result.output
    assert "stage runs" in result.output
    assert "no stage runs yet" in result.output
    assert "compounds" in result.output


def test_status_without_a_project_says_how_to_make_one(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert "vscreen init" in result.output


def test_status_shows_a_stage_run_after_the_demo(project: Path) -> None:
    assert (
        runner.invoke(
            app, ["demo", "--items", "3", "--sleep", "0", "--plain", "--project", str(project)]
        ).exit_code
        == 0
    )
    result = runner.invoke(app, ["status", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "demo" in result.output
    assert "complete" in result.output
    assert "no stage runs yet" not in result.output


# --------------------------------------------------------------------------- #
# demo
# --------------------------------------------------------------------------- #


def test_demo_runs_and_is_idempotent(project: Path) -> None:
    first = runner.invoke(
        app, ["demo", "--items", "4", "--sleep", "0", "--plain", "--project", str(project)]
    )
    assert first.exit_code == 0, first.output
    assert "[demo] started: 4 item(s)" in first.output
    assert "4 executed this run" in first.output

    second = runner.invoke(
        app, ["demo", "--items", "4", "--sleep", "0", "--plain", "--project", str(project)]
    )
    assert second.exit_code == 0, second.output
    assert "0 executed this run" in second.output, "a second run must resume, not redo"

    with store.open_store(project) as conn:
        runs = store.list_stage_runs(conn)
        assert len(runs) == 1
        assert store.count_results(conn, stage_run_id=runs[0].id) == 8


def test_demo_is_hidden_but_documented_in_help(project: Path) -> None:
    help_text = runner.invoke(app, ["--help"]).output
    assert "init" in help_text and "status" in help_text
    assert "demo" not in help_text
    assert runner.invoke(app, ["demo", "--help"]).exit_code == 0


def test_demo_outside_a_project_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["demo", "--project", str(tmp_path), "--items", "2"])
    assert result.exit_code == 1
    assert "vscreen init" in result.output


def test_stages_command_lists_the_registry() -> None:
    result = runner.invoke(app, ["stages"])
    assert result.exit_code == 0
    assert result.output.strip() == "demo"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output
