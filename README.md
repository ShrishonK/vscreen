# vscreen

A reusable virtual screening pipeline, built for two studies:

1. **An audit** of small molecules proposed as drug candidates in published
   computational design papers, run through a uniform checklist and compared
   against reference sets.
2. **A prospective screen** producing a ranked, pre-registered shortlist of
   purchasable compounds for experimental testing.

Everything is open source under permissive licences. No commercial software
is used at any stage, so the pipeline can be redistributed and rerun without
restriction.

## Status

In development. See `docs/PHASES.md` for the roadmap and current build
status.

## Setup

```bash
conda create -n vscreen python=3.11
conda activate vscreen
pip install -e ".[dev]"

# External binaries, located at runtime
conda install -c conda-forge vina

# Optional
pip install "aizynthfinder[all]"

# Molecular dynamics only
conda install -c conda-forge openmm openmmforcefields openff-toolkit
```

## Quick start

Available once Phase 3 lands.

```bash
vscreen init my-run && cd my-run
vscreen target search EGFR
vscreen target add --pdb 4I22 --box-from-ligand
vscreen sets build actives --target egfr-4i22
vscreen sets build decoys --from actives:P00533 --library zinc_subset.smi
vscreen validate --target egfr-4i22      # must PASS before anything else
```

## Documentation

| File | Contents |
|---|---|
| `docs/CONVENTIONS.md` | Constraints, architecture rules, code and test conventions |
| `docs/ARCHITECTURE.md` | Design rationale, stage interface, scientific invariants |
| `docs/SCHEMA.sql` | SQLite schema |
| `docs/PHASES.md` | Build roadmap and status |

## The one rule worth knowing up front

`vscreen validate` is a blocking gate, not a diagnostic. It re-docks a
crystal structure's own ligand and checks that known actives rank above
property-matched decoys. Until it passes, every number produced downstream is
meaningless — however polished the output looks.

## Licence

MIT. See `LICENSE`.
