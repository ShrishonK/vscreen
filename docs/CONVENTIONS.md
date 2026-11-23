# Conventions and constraints

Project rules. Read alongside `docs/ARCHITECTURE.md` before writing code.

## What this is

A reusable virtual screening pipeline serving two workflows:

1. **Audit** — run molecules extracted from published papers through a fixed
   checklist and compare survival against reference sets.
2. **Discovery** — screen or generate candidates against a protein target and
   produce a ranked shortlist for experimental testing.

This code backs two academic papers. Reproducibility is a hard requirement,
not a nice-to-have.

## Hard constraints

- Python 3.11+.
- **The primary development machine has no GPU.** Never assume CUDA. A stage
  needing a GPU must detect its absence, print a clear message, emit a
  runnable notebook for remote execution, and exit cleanly. Never silently
  fall back to a CPU path that is two orders of magnitude slower.
- Everything runs offline after first fetch. Cache all API responses to disk.
  Tests never hit the network.
- **No commercial software, ever.** Schrodinger, OpenEye, MOE and ChemAxon
  are excluded by policy — they would restrict reuse of the released
  pipeline and prevent the licensing statement the papers need.
- `meeko` is LGPL-2.1: installed dependency only, never vendored or copied
  into this repository.

## The six architecture rules

Stated here, explained in `docs/ARCHITECTURE.md`.

1. **Config is the contract.** A YAML file fully determines a run. `cli.py`
   parses arguments, writes config, invokes a runner, renders results. It
   holds no logic.
2. **No science in the UI layer.** Core emits events; `ui/` renders them.
   `ui/plain.py` must be swappable for `ui/live.py` with no change to any
   stage.
3. **Every stage is resumable at item granularity.** Completed items are
   skipped on re-run. Interrupting a run and restarting must continue, not
   restart.
4. **Results are stored narrow** (one row per compound per metric per
   stage run) **and exported wide.** Never add a score column to the
   `compounds` table.
5. **External tool versions are recorded on every stage run.** A result
   without a recorded tool version is not publishable.
6. **GPU is an optional accelerator**, declared per stage, never assumed.

## Code conventions

- Type hints everywhere. pydantic v2 for all config models.
- Pure functions in `core/`. Side effects confined to `store.py` and
  `runners/`.
- Errors: raise typed exceptions from `errors.py` with actionable messages.
  `"Vina not found. Install with: conda install -c conda-forge vina"` is
  good. A bare `FileNotFoundError` is not.
- Every external tool call goes through `tools.py`, which records the
  version.
- `pathlib` only. Never hardcode an absolute path.
- Logging goes to `logs/` inside the project directory. stdout belongs to the
  user interface.
- Random seeds are explicit and recorded. Docking and sampling must be
  reproducible run to run.

## Testing

- pytest. Unit tests run in under 30 seconds with no external binaries and no
  network: mock subprocess calls, use cached fixtures.
- Integration tests marked `@pytest.mark.integration`, skipped when the
  required binary is absent.
- Fixtures in `tests/fixtures/`: one small receptor, its cognate ligand, and
  20 ligands. Small enough to commit.
- Every stage needs a test proving resumability: run, interrupt, re-run,
  assert no duplicated work.

## Dependencies

The list in `pyproject.toml` is fixed. Adding one is a decision, not a
convenience: each addition is a reproducibility liability and one more thing
to install inside a container for distributed execution. If a new dependency
is genuinely needed, add it deliberately and note why in the commit message.

## Things that must not happen

- Logic in `cli.py`.
- Invented scientific defaults. If a parameter needs a value and none is
  specified, stop and decide it explicitly. A wrong docking box or
  protonation state silently corrupts every downstream result and raises no
  error.
- Results that cannot be regenerated from the config plus the database.
- Refactoring earlier phases while building a later one. Each phase builds on
  tested code; rewriting it mid-stream invalidates tests already trusted.
- Building beyond the current phase. See `docs/PHASES.md`.
