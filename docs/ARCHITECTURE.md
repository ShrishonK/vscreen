# vscreen architecture

Read this before writing code, alongside `docs/CONVENTIONS.md`. That file
states the rules; this one explains them and fixes the interfaces that later
work depends on.

## What the tool does

Two workflows over one engine.

**Audit (Paper 1).** Molecules extracted from published papers go through a
fixed checklist. Survival is compared against reference sets. The question is
"how many published candidates meet objective criteria their own papers never
checked?"

**Discovery (Paper 2).** Candidates are generated or screened against a
target, ranked, and shortlisted for purchase and experimental testing.

The audit is the discovery pipeline pointed at someone else's molecules.

## The stage pipeline

```
  prepare target        CPU   fetch, clean, protonate, define box
         |
  build compound sets   CPU   corpus, actives, decoys, drugs, random
         |
  dock                  CPU   AutoDock Vina, one process per compound
         |
  [ VALIDATE GATE ]           redock RMSD <= 2.0 AND EF1% >= 10
         |                    fails -> fix setup, nothing downstream runs
  checklist stages      CPU   filters, posecheck, retrosynthesis, novelty
         |
  confirm               GPU   Boltz-2 affinity, OpenMM dynamics
         |
  analyze and report    CPU   survival stats, enrichment, figures, CSV
```

Only `confirm` needs a GPU. The entire validation path is CPU work and runs
on a laptop.

## The six rules, explained

### 1. Config is the contract

A YAML file fully determines a run. The run is reproducible by handing
someone the config and the input files.

`cli.py` parses arguments, writes config, invokes a runner, renders the
result. It contains no science and no branching logic beyond argument
handling. A future graphical interface writes the same config and invokes the
same runner — that is the whole reason for the split.

### 2. No science in the UI layer

Core code emits events. `ui/` renders them. Nothing in `core/` or `stages/`
imports `rich`.

```python
class Reporter(Protocol):
    def emit(self, event: Event) -> None: ...
```

`PlainReporter` prints one line per event. `LiveReporter` draws the Rich
dashboard. Swapping them must require no change to any stage. This is what
makes stages testable and what lets the pipeline run headless inside a
notebook or an OSPool job.

Events: `StageStarted(stage, total)`, `ItemCompleted(stage, item_id, ok)`,
`StageFinished(stage, counts)`, `LogLine(level, text)`, `Warning(text)`.

### 3. Resumable at item granularity

Remote sessions get disconnected and laptops get closed. Every stage must
survive it.

A stage computes a `run_key` from its own config subtree, its input compound
IDs, and the versions of every external tool it calls. If a `stage_run` with
that key exists, it is resumed rather than restarted. Per-item completion
lives in `stage_items`, written before results so a crash between the two is
detectable.

Molecular dynamics resumes at checkpoint granularity, not item granularity: a
replicate killed at 30 of 50 ns resumes at 30.

### 4. Narrow storage, wide export

One `results` row per compound per metric per stage run. Re-running docking
with different exhaustiveness creates a second `stage_run` and a second set
of rows; the first is preserved. A wide table would overwrite history and
make reproducing an earlier figure impossible.

Never add a score column to `compounds`. Reports pivot narrow to wide.

### 5. Tool versions recorded every run

Vina, GNINA, RDKit, AiZynthFinder, OpenMM. Captured by `tools.py` at
resolution time and written into `stage_runs.tool_versions_json`.

A result without a recorded tool version cannot go in a paper, and silent
version drift over a multi-week build is a real risk.

### 6. GPU optional, declared per stage

```python
@dataclass(frozen=True)
class Requirements:
    cpu_cores: int = 1
    needs_gpu: bool = False
    external_tools: tuple[str, ...] = ()
    needs_network: bool = False
```

A stage with `needs_gpu=True` on a machine without one prints a clear
message, emits a runnable Colab notebook, and exits 0. It never crashes and
never silently runs a path that is two orders of magnitude slower.

## The Stage interface

Fixed in Phase 0. Every later phase implements against it.

```python
class Stage(ABC):
    name: str
    requires: Requirements

    def run_key(self, ctx: Context) -> str: ...
    def iter_items(self, ctx: Context) -> Iterable[Item]: ...
    def is_item_done(self, ctx: Context, item: Item) -> bool: ...
    def run_item(self, ctx: Context, item: Item) -> list[Result]: ...
    def finalize(self, ctx: Context) -> None: ...
```

`StageRunner` owns everything else: computing the run key, creating or
resuming the `stage_run` row, skipping completed items, parallelism sized to
`requires.cpu_cores`, writing results, emitting events, and marking
completion. A stage author writes `run_item` and little else.

## Repository layout

```
vscreen/
  cli.py          typer commands only, zero logic
  config.py       pydantic schema, YAML load/save, per-stage hashing
  store.py        SQLite connection, schema, CRUD
  events.py       event dataclasses + Reporter protocol
  tools.py        locate external binaries, capture versions
  errors.py       typed exceptions with actionable messages

  fetch/          uniprot.py  pdb.py  chembl.py  cache.py
  core/           prep  standardize  ligprep  dock  redock  rescore
                  filters  posecheck  retrosynth  novelty  affinity
                  md  interactions
  sets/           actives.py  decoys.py  drugs.py  random_set.py
  al/             featurize.py  surrogate.py  acquire.py  loop.py
  audit/          study.py  ingest.py  battery.py
  analysis/       enrichment.py  survival.py  stats.py  recall.py
  report/         figures.py  tables.py  assay.py
  runners/        base.py  local.py  colab.py  osg.py
  stages/         base.py  registry.py  <one module per stage>
  ui/             live.py  plain.py  theme.py

tests/
  fixtures/       one small receptor, its cognate ligand, 20 ligands
  unit/           fast, no binaries, no network, subprocess mocked
  integration/    marked, skipped when the binary is absent
```

## Project directory on disk

A project is a directory, not scattered files.

```
my-run/
  vscreen.yaml        the config — the contract
  vscreen.db          SQLite
  targets/<id>/       receptor.pdbqt, box.json, provenance.json
  ligands/<set>/      prepared pdbqt, cached by InChIKey
  poses/<run>/        docked poses
  md/<compound>/      trajectories and checkpoints
  cache/              cached API responses
  reports/            figures and CSVs
  logs/
```

## Scientific invariants

These are correctness requirements, not style preferences. Violating one
produces plausible numbers that are wrong, with no error raised.

- **`has_cognate_ligand` gates the validation.** A predicted or apo structure
  has no crystallographic ligand to re-dock, so the gate cannot run. Such a
  target reports `INCOMPLETE`, never `PASS`, and carries a visible warning.
  A predicted structure is never silently treated as equivalent to a holo
  crystal structure.

- **Nothing downstream of the gate runs until it passes.** Re-docking RMSD
  and enrichment are not diagnostics to glance at; they are a blocking
  condition.

- **RMSD must be symmetry-corrected.** Use RDKit `GetBestRMS`. A benzene ring
  flipped 180 degrees is the same pose, and naive RMSD reports a failure that
  does not exist.

- **Vina scores are negative and lower is better.** Any ranking, acquisition
  function or sort must respect this. An acquisition function written for
  maximization selects the worst compounds confidently and silently.

- **Properties are reported, never scored.** Emit MW, cLogP, QED and the rest
  as metrics. Do not emit a drug-likeness verdict. An invented threshold ends
  up in a figure and then has to be defended.

- **A failed parse is a data point.** Record `parse_ok = 0` with a reason.
  Never drop a molecule silently and never raise.

- **Covalent binders dock badly for reasons unrelated to quality.** Flag them
  in reference sets rather than dropping them, so the user can see the effect.

- **Active learning never touches the audit path.** The audit corpus is small
  and is docked exhaustively.

- **One Vina process per compound, `cpu=1`, parallel across compounds.**
  Throughput is far better than handing Vina a large core count for a single
  ligand.

## Statistics conventions

- Always return effect size and confidence interval alongside any p-value.
- Wilson intervals for proportions, bootstrap for everything else.
- Fisher exact for small counts, not chi-square.
- Benjamini-Hochberg across the pre-specified family of tests.
- Non-parametric by default; IC50 and score distributions are skewed.
- Compounds inactive at the top tested concentration are right-censored,
  never imputed as the top concentration and never dropped.
