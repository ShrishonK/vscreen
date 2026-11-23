"""Typer commands. Parses arguments, writes config, invokes a runner, renders.

No science and no branching beyond argument handling (rule 1). A future
graphical interface writes the same config and invokes the same runner.
"""

from __future__ import annotations

import json
import sys
import tempfile
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
from .fetch import pdb as pdb_fetch
from .fetch import uniprot as uniprot_fetch
from .fetch.cache import build_cache_for
from .log import configure_logging
from .stages.base import Context, StageRunner
from .stages.registry import build_stage, stage_names
from .stages.target_add import NO_COGNATE_LIGAND_WARNING, TargetAddPlan, TargetAddStage
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


def _make_demo_scratch_project() -> Path:
    """Create a fresh throwaway project under the system temp dir for `vscreen demo`.

    `DemoStage` writes 50 synthetic compounds; running it inside a real project
    would contaminate the corpus. The default is therefore a disposable
    project that leaves no trace in the caller's directory.
    """
    scratch_root = Path(tempfile.mkdtemp(prefix="vscreen-demo-"))
    project_dir = scratch_root / "demo"
    project_dir.mkdir()
    for sub in PROJECT_SUBDIRS:
        (project_dir / sub).mkdir()
    save_config(
        ProjectConfig(name="demo-scratch", created_at=utc_now_iso(), seed=0),
        project_dir / PROJECT_CONFIG_NAME,
    )
    with store.open_store(project_dir):
        pass
    return project_dir


@app.command(hidden=True)
def demo(
    items: Annotated[int, typer.Option(help="Number of fake items.")] = 50,
    sleep: Annotated[float, typer.Option(help="Seconds of fake work per item.")] = 0.15,
    plain: Annotated[bool, typer.Option("--plain", help="Force the plain reporter.")] = False,
    here: Annotated[
        bool,
        typer.Option(
            "--here",
            help="Run inside the current project instead of a throwaway temp project.",
        ),
    ] = False,
    project: Annotated[
        Path | None,
        typer.Option("--project", help="Run inside this existing project.", file_okay=False),
    ] = None,
) -> None:
    """Run DemoStage over fake items to exercise the dashboard and resume.

    By default `vscreen demo` creates a disposable project in a system temp
    directory so its 50 synthetic compounds cannot contaminate a real corpus.
    Pass `--here` (or `--project PATH`) to run it against an existing project.
    """
    scratch = False
    try:
        if here or project is not None:
            root = find_project_root(project)
        else:
            root = _make_demo_scratch_project()
            scratch = True
            console.print(
                f"[dim]using throwaway project at {root} "
                f"(pass --here to run in the current project instead)[/]"
            )
        config = load_config(root / PROJECT_CONFIG_NAME)
        configure_logging(root)
        stage = build_stage("demo", n_items=items, sleep_s=sleep)
        with store.open_store(root) as conn:
            reporter = make_reporter(config.name, plain=plain)
            ctx = Context(project_dir=root, config=config, conn=conn, reporter=reporter)
            with reporter:
                outcome = StageRunner().run(stage, ctx)
    except KeyboardInterrupt:
        if scratch:
            console.print(
                "\n[yellow]interrupted[/] — the throwaway project at "
                f"{root} is incomplete; delete it or re-run `vscreen demo --here --project {root}`"
            )
        else:
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


# --------------------------------------------------------------------------- #
# target subcommands (Phase 1)
# --------------------------------------------------------------------------- #

target_app = typer.Typer(
    name="target",
    help="Search databases, pick a structure, prepare a target.",
    no_args_is_help=True,
)
app.add_typer(target_app)


def _parse_triple(value: str, *, label: str) -> tuple[float, float, float]:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 3:
        raise typer.BadParameter(f"{label} must be three comma-separated numbers, got {value!r}")
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError as exc:
        raise typer.BadParameter(f"{label} must be numeric, got {value!r}: {exc}") from exc


def _derive_target_id(
    explicit: str | None, pdb_id: str | None, uniprot: str | None, input_file: Path | None
) -> str:
    if explicit:
        return explicit.strip()
    if pdb_id:
        return pdb_id.strip().lower()
    if uniprot:
        return uniprot.strip().lower()
    if input_file is not None:
        return input_file.stem
    raise typer.BadParameter(
        "Pass --id to name this target, or --pdb / --uniprot / --file so vscreen can derive one."
    )


@target_app.command("search")
def target_search(
    query: Annotated[str, typer.Argument(help="Free-text UniProt query, e.g. 'EGFR human'.")],
    limit: Annotated[int, typer.Option("--limit", help="Max hits.")] = 25,
    project: Annotated[
        Path | None, typer.Option("--project", help="Project directory.", file_okay=False)
    ] = None,
) -> None:
    """Search UniProt and print accession, gene, protein, organism, length."""
    try:
        root = find_project_root(project)
        cache = build_cache_for(root)
        hits = uniprot_fetch.search(cache, query, limit=limit)
    except VscreenError as exc:
        _fail(exc)
        return
    if not hits:
        console.print(f"[yellow]no UniProt hits for[/] {query!r}")
        return
    table = Table(title=f"uniprot search: {query}", title_justify="left", header_style="bold")
    table.add_column("accession")
    table.add_column("gene")
    table.add_column("protein")
    table.add_column("organism")
    table.add_column("length", justify="right")
    for hit in hits:
        table.add_row(
            hit.accession,
            hit.gene_name or "-",
            hit.protein_name or "-",
            hit.organism or "-",
            str(hit.length) if hit.length is not None else "-",
        )
    console.print(table)


@target_app.command("structures")
def target_structures(
    accession: Annotated[str, typer.Argument(help="UniProt accession, e.g. P00533.")],
    project: Annotated[
        Path | None, typer.Option("--project", help="Project directory.", file_okay=False)
    ] = None,
) -> None:
    """List PDB structures for a UniProt accession, flagging ligand-bound entries."""
    try:
        root = find_project_root(project)
        cache = build_cache_for(root)
        summaries = pdb_fetch.structures_for_uniprot(cache, accession)
    except VscreenError as exc:
        _fail(exc)
        return
    if not summaries:
        console.print(f"[yellow]no PDB structures cross-reference[/] {accession}")
        return
    table = Table(
        title=f"pdb structures for {accession}", title_justify="left", header_style="bold"
    )
    table.add_column("pdb")
    table.add_column("res Å", justify="right")
    table.add_column("method")
    table.add_column("ligand")
    table.add_column("chains")
    table.add_column("title")
    for summary in summaries:
        ligand_cell = (
            ", ".join(lig.comp_id for lig in summary.bound_ligands)
            if summary.has_bound_ligand
            else "[red]apo[/]"
        )
        table.add_row(
            summary.pdb_id,
            f"{summary.resolution_a:.2f}" if summary.resolution_a is not None else "-",
            summary.method or "-",
            ligand_cell,
            ",".join(summary.chains) if summary.chains else "-",
            (summary.title or "")[:60],
        )
    console.print(table)


@target_app.command("add")
def target_add(
    id_: Annotated[str | None, typer.Option("--id", help="Project-local target id.")] = None,
    uniprot: Annotated[
        str | None, typer.Option("--uniprot", help="Pick a PDB via UniProt cross-refs.")
    ] = None,
    pdb: Annotated[str | None, typer.Option("--pdb", help="A specific PDB id.")] = None,
    file_: Annotated[
        Path | None, typer.Option("--file", help="A local PDB file.", exists=True, dir_okay=False)
    ] = None,
    fasta: Annotated[
        Path | None,
        typer.Option("--fasta", help="A FASTA sequence; emits a Colab folding notebook."),
    ] = None,
    cds: Annotated[
        Path | None,
        typer.Option("--cds", help="A coding sequence; emits a Colab folding notebook."),
    ] = None,
    mutate: Annotated[
        str | None,
        typer.Option(
            "--mutate",
            help="Template PDB id and mutations as 'PDB:MUT1,MUT2' (requires --file template).",
        ),
    ] = None,
    chain: Annotated[
        str | None, typer.Option("--chain", help="Chain to keep; auto-selected otherwise.")
    ] = None,
    ligand_resname: Annotated[
        str | None,
        typer.Option("--ligand", help="Residue name of the cognate ligand (default: largest)."),
    ] = None,
    ph: Annotated[
        float, typer.Option("--pH", help="Protonation pH, recorded on provenance.")
    ] = 7.4,
    box_from_ligand: Annotated[
        bool,
        typer.Option("--box-from-ligand", help="Box from the cognate ligand plus --box-padding."),
    ] = False,
    box_explicit: Annotated[
        bool,
        typer.Option(
            "--box-explicit", help="Explicit box; requires --box-center and --box-size."
        ),
    ] = False,
    box_padding: Annotated[
        float,
        typer.Option("--box-padding", help="Padding in Å around the cognate ligand."),
    ] = 8.0,
    box_center: Annotated[
        str | None,
        typer.Option("--box-center", help="Explicit box center as 'x,y,z' in Å."),
    ] = None,
    box_size: Annotated[
        str | None,
        typer.Option("--box-size", help="Explicit box size as 'dx,dy,dz' in Å."),
    ] = None,
    plain: Annotated[bool, typer.Option("--plain", help="Force the plain reporter.")] = False,
    project: Annotated[
        Path | None, typer.Option("--project", help="Project directory.", file_okay=False)
    ] = None,
) -> None:
    """Prepare a target from one of six input modes and record it in the store."""
    modes = [
        name
        for name, value in (
            ("--uniprot", uniprot),
            ("--pdb", pdb),
            ("--file", file_),
            ("--fasta", fasta),
            ("--cds", cds),
            ("--mutate", mutate),
        )
        if value
    ]
    if len(modes) != 1:
        _fail(
            VscreenError(
                "Pass exactly one of --uniprot / --pdb / --file / --fasta / --cds / --mutate "
                f"(got: {modes or 'none'})."
            )
        )
        return

    if box_from_ligand and box_explicit:
        _fail(VscreenError("Choose one of --box-from-ligand or --box-explicit, not both."))
        return
    # FASTA / CDS skip box definition; the folding notebook defers it.
    if not box_from_ligand and not box_explicit and not (fasta or cds):
        _fail(
            VscreenError(
                "Pick a docking box: --box-from-ligand (uses the cognate ligand) or "
                "--box-explicit --box-center x,y,z --box-size dx,dy,dz. No default box "
                "is accepted (docs/CONVENTIONS.md)."
            )
        )
        return

    source_kind: str
    input_file: Path | None = None
    pdb_id = pdb.strip().upper() if pdb else None
    mutations: tuple[str, ...] = ()
    if uniprot:
        source_kind = "uniprot"
    elif pdb:
        source_kind = "pdb"
    elif file_:
        source_kind = "file"
        input_file = file_
    elif fasta:
        source_kind = "fasta"
        input_file = fasta
    elif cds:
        source_kind = "cds"
        input_file = cds
    elif mutate:
        source_kind = "mutant"
        if ":" in mutate:
            template, muts = mutate.split(":", 1)
            pdb_id = template.strip().upper() or pdb_id
            mutations = tuple(m.strip() for m in muts.split(",") if m.strip())
        else:
            mutations = (mutate.strip(),)
        input_file = file_
    else:  # pragma: no cover - guarded above
        _fail(VscreenError("internal: no input mode selected"))
        return

    try:
        target_id = _derive_target_id(id_, pdb_id, uniprot, input_file)
        if box_explicit and (box_center is None or box_size is None):
            raise VscreenError(
                "--box-explicit requires both --box-center and --box-size "
                "(no default box, docs/CONVENTIONS.md)."
            )
        plan = TargetAddPlan(
            target_id=target_id,
            source_kind=source_kind,  # type: ignore[arg-type]
            pdb_id=pdb_id,
            uniprot=uniprot.strip().upper() if uniprot else None,
            input_file=input_file,
            chain=chain,
            ph=ph,
            box_method="explicit" if box_explicit else "from-ligand",
            box_padding_a=box_padding,
            box_center=_parse_triple(box_center, label="--box-center") if box_center else None,
            box_size=_parse_triple(box_size, label="--box-size") if box_size else None,
            ligand_resname=ligand_resname,
            mutations=mutations,
        )

        root = find_project_root(project)
        config = load_config(root / PROJECT_CONFIG_NAME)
        configure_logging(root)
        cache = build_cache_for(root)
        stage = TargetAddStage(plan, cache=cache)
        with store.open_store(root) as conn:
            reporter = make_reporter(config.name, plain=plain)
            ctx = Context(project_dir=root, config=config, conn=conn, reporter=reporter)
            with reporter:
                outcome = StageRunner().run(stage, ctx)
            target_row = store.get_target(conn, target_id)
    except VscreenError as exc:
        _fail(exc)
        return

    if plan.needs_folding:
        notebook = ctx.path("targets", target_id, "folding.ipynb")
        console.print(
            f"[yellow]folding required[/] — open {notebook} on Colab and come back with "
            "`receptor.pdb`, then rerun with --file."
        )
        console.print(f"[dim]stage run {outcome.run_id[:8]} status {outcome.status}[/]")
        return

    if target_row is None:
        _fail(VscreenError("target add completed without writing a target row (unexpected)"))
        return
    console.print(
        f"[bold green]target[/] {target_id} added "
        f"[dim](stage run {outcome.run_id[:8]} {outcome.status})[/]"
    )
    if not target_row.has_cognate_ligand:
        console.print(f"[bold yellow]WARNING[/] {NO_COGNATE_LIGAND_WARNING}")
    console.print(f"  receptor  {target_row.receptor_path}")
    if target_row.cognate_ligand_path:
        console.print(f"  ligand    {target_row.cognate_ligand_path}")
    console.print(f"  box       {target_row.box_json}")


@target_app.command("show")
def target_show(
    target_id: Annotated[str, typer.Argument(help="Target id to inspect.")],
    project: Annotated[
        Path | None, typer.Option("--project", help="Project directory.", file_okay=False)
    ] = None,
) -> None:
    """Print a target's receptor, box and provenance; flag missing cognate ligand prominently."""
    try:
        root = find_project_root(project)
        with store.open_store(root) as conn:
            target = store.get_target(conn, target_id)
    except VscreenError as exc:
        _fail(exc)
        return
    if target is None:
        _fail(VscreenError(f"No target {target_id!r}. Try: vscreen target list"))
        return

    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold")
    header.add_column()
    header.add_row("target", target.id)
    header.add_row("source", target.source_kind)
    if target.pdb_id:
        header.add_row("pdb", target.pdb_id)
    if target.uniprot:
        header.add_row("uniprot", target.uniprot)
    header.add_row("receptor", target.receptor_path)
    header.add_row("cognate ligand", target.cognate_ligand_path or "-")
    header.add_row("box", target.box_json)
    console.print(header)

    if not target.has_cognate_ligand:
        # Prominent, bordered block — the re-docking gate cannot run without one
        # (docs/ARCHITECTURE.md scientific invariants).
        banner = Table(
            title="[bold red]COGNATE LIGAND MISSING[/]",
            title_justify="left",
            show_header=False,
            border_style="red",
        )
        banner.add_column()
        banner.add_row(f"[yellow]{NO_COGNATE_LIGAND_WARNING}[/]")
        console.print(banner)

    try:
        provenance = json.loads(target.provenance_json)
    except json.JSONDecodeError:
        provenance = {}
    if provenance:
        table = Table(title="provenance", title_justify="left", header_style="bold")
        table.add_column("key")
        table.add_column("value")
        for key, value in sorted(provenance.items()):
            rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            table.add_row(key, rendered)
        console.print(table)


@target_app.command("list")
def target_list(
    project: Annotated[
        Path | None, typer.Option("--project", help="Project directory.", file_okay=False)
    ] = None,
) -> None:
    """List every target in the project's store."""
    try:
        root = find_project_root(project)
        with store.open_store(root) as conn:
            targets = store.list_targets(conn)
    except VscreenError as exc:
        _fail(exc)
        return
    if not targets:
        console.print("[dim]no targets yet[/]")
        return
    table = Table(title="targets", title_justify="left", header_style="bold")
    table.add_column("id")
    table.add_column("source")
    table.add_column("pdb")
    table.add_column("uniprot")
    table.add_column("ligand")
    table.add_column("receptor")
    for target in targets:
        table.add_row(
            target.id,
            target.source_kind,
            target.pdb_id or "-",
            target.uniprot or "-",
            "[green]yes[/]" if target.has_cognate_ligand else "[red]NO[/]",
            target.receptor_path,
        )
    console.print(table)


def main() -> None:  # pragma: no cover - console-script shim
    try:
        app()
    except VscreenError as exc:
        console.print(f"[bold red]error[/] {exc}")
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
