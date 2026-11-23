"""Typer commands. Parses arguments, writes config, invokes a runner, renders.

No science and no branching beyond argument handling (rule 1). A future
graphical interface writes the same config and invokes the same runner.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import store
from .config import (
    DB_NAME,
    PROJECT_CONFIG_NAME,
    PROJECT_SUBDIRS,
    ProjectConfig,
    find_project_root,
    load_config,
    save_config,
    utc_now_iso,
)
from .errors import GpuUnavailable, VscreenError
from .log import configure_logging
from .stages.base import Context, StageRunner
from .stages.registry import build_stage, stage_names
from .ui import make_reporter
from .ui.theme import status_style

app = typer.Typer(
    name="vscreen",
    help="Virtual screening and published-candidate audit pipeline.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


def _fail(exc: Exception) -> None:
    console.print(f"[bold red]error[/] {exc}")
    raise typer.Exit(code=1)


@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Project directory to create.")],
    parent: Annotated[
        Path, typer.Option("--in", help="Where to create it.", file_okay=False)
    ] = Path("."),
    seed: Annotated[int, typer.Option(help="Global random seed, recorded in the config.")] = 0,
) -> None:
    """Create a project directory with a config, a database and the run layout."""
    project_dir = (parent / name).resolve()
    config_path = project_dir / PROJECT_CONFIG_NAME
    if config_path.exists():
        _fail(VscreenError(f"{config_path} already exists; refusing to overwrite it"))

    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        for sub in PROJECT_SUBDIRS:
            (project_dir / sub).mkdir(exist_ok=True)
        config = ProjectConfig(name=name, created_at=utc_now_iso(), seed=seed)
        save_config(config, config_path)
        with store.open_store(project_dir):
            pass
        configure_logging(project_dir)
    except VscreenError as exc:
        _fail(exc)

    console.print(f"[bold green]created[/] project [bold cyan]{name}[/] at {project_dir}")
    console.print(f"  config   {config_path.name}")
    console.print(f"  database {DB_NAME}")
    console.print(f"  seed     {seed}")
    console.print(f"\nNext: cd {name} && vscreen status")


@app.command()
def status(
    project: Annotated[
        Path | None, typer.Option("--project", help="Project directory.", file_okay=False)
    ] = None,
) -> None:
    """Show what the project contains and what has been run."""
    try:
        root = find_project_root(project)
        config = load_config(root / PROJECT_CONFIG_NAME)
        with store.open_store(root) as conn:
            counts = store.table_counts(conn)
            summaries = store.stage_run_summaries(conn)
    except VscreenError as exc:
        _fail(exc)
        return

    console.print(
        f"[bold cyan]{config.name}[/]  [dim]created {config.created_at}  seed {config.seed}[/]"
    )
    console.print(f"[dim]{root}[/]\n")

    contents = Table(title="contents", title_justify="left", header_style="bold")
    contents.add_column("table")
    contents.add_column("rows", justify="right")
    for table_name, count in counts.items():
        contents.add_row(table_name, str(count))
    console.print(contents)

    runs = Table(title="stage runs", title_justify="left", header_style="bold")
    runs.add_column("stage")
    runs.add_column("status")
    runs.add_column("items", justify="right")
    runs.add_column("done", justify="right")
    runs.add_column("failed", justify="right")
    runs.add_column("results", justify="right")
    runs.add_column("run key")
    runs.add_column("started")
    for summary in summaries:
        run = summary.run
        runs.add_row(
            run.stage,
            f"[{status_style(run.status)}]{run.status}[/]",
            str(summary.items_total),
            str(summary.items_done),
            str(summary.items_failed),
            str(summary.results),
            run.run_key[:12],
            run.started_at or "-",
        )
    console.print(runs)
    if not summaries:
        console.print("[dim]no stage runs yet[/]")


@app.command(hidden=True)
def demo(
    items: Annotated[int, typer.Option(help="Number of fake items.")] = 50,
    sleep: Annotated[float, typer.Option(help="Seconds of fake work per item.")] = 0.15,
    plain: Annotated[bool, typer.Option("--plain", help="Force the plain reporter.")] = False,
    project: Annotated[
        Path | None, typer.Option("--project", help="Project directory.", file_okay=False)
    ] = None,
) -> None:
    """Run DemoStage over fake items to exercise the dashboard and resume."""
    try:
        root = find_project_root(project)
        config = load_config(root / PROJECT_CONFIG_NAME)
        configure_logging(root)
        stage = build_stage("demo", n_items=items, sleep_s=sleep)
        with store.open_store(root) as conn:
            reporter = make_reporter(config.name, plain=plain)
            ctx = Context(project_dir=root, config=config, conn=conn, reporter=reporter)
            with reporter:
                outcome = StageRunner().run(stage, ctx)
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/] — re-run `vscreen demo` to resume")
        raise typer.Exit(code=130) from None
    except GpuUnavailable as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(code=0) from None
    except VscreenError as exc:
        _fail(exc)
        return

    console.print(
        f"[bold green]{outcome.stage}[/] {outcome.status}: "
        f"{outcome.done}/{outcome.total} done, {outcome.executed} executed this run, "
        f"{outcome.failed} failed  [dim]run {outcome.run_id[:8]} key {outcome.run_key[:12]}[/]"
    )


@app.command(hidden=True)
def stages() -> None:
    """List registered stage names."""
    for name in stage_names():
        console.print(name)


def main() -> None:  # pragma: no cover - console-script shim
    try:
        app()
    except VscreenError as exc:
        console.print(f"[bold red]error[/] {exc}")
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
