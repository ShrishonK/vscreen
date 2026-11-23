# Build phases

> **Build one phase at a time.** All nine are listed for context, so the
> dependencies between them are visible. Building ahead creates untested code
> that the next phase has to work around.

Read `docs/CONVENTIONS.md` and `docs/ARCHITECTURE.md` before starting any
phase. Run that phase's acceptance checks, commit, update the Build status
table below, then move on.

| Phase | Delivers | Hardware |
|---|---|---|
| 0 | Scaffold, config, store, dashboard | laptop |
| 1 | Database fetch and target input | laptop |
| 2 | Ligand prep, docking, re-docking check | laptop |
| 3 | Reference sets and the validate gate | laptop |
| 4 | The checklist filter stages | laptop |
| 5 | Audit commands and comparison analysis | laptop |
| 6 | Active learning loop | laptop |
| 7 | Molecular dynamics and remote runners | GPU or OSPool |
| 8 | Report and figure generation | laptop |

Phases 0–5 and 8 are everything Paper 1 needs.

## Build status

Update this table as phases land, so the next session knows what exists.

| Phase | Status | Commit |
|---|---|---|
| 0 | not started | |
| 1 | not started | |
| 2 | not started | |
| 3 | not started | |
| 4 | not started | |
| 5 | not started | |
| 6 | not started | |
| 7 | not started | |
| 8 | not started | |

---

## Phase 0 — scaffold, config, store, dashboard

No science. A skeleton with a working dashboard driven by fake progress, so
the shape is right before real work goes in.

Build:

1. Package layout per `docs/ARCHITECTURE.md`. Entry point
   `vscreen = "vscreen.cli:app"`.
2. `config.py` — pydantic v2 models: `ProjectConfig` (name, created_at,
   targets, sets, stages), `TargetConfig`, `SetConfig`. A
   `stage_hash(stage_name, config, input_ids, tool_versions) -> str` using
   sha256 over a canonical JSON dump with deterministic key ordering.
   `load_config` / `save_config` for YAML.
3. `store.py` — SQLite using `docs/SCHEMA.sql` verbatim. `connect`,
   `init_schema`, typed CRUD for every table. Context managers, WAL mode,
   foreign keys ON.
4. `events.py` — frozen dataclasses and the `Reporter` protocol per
   `docs/ARCHITECTURE.md`.
5. `ui/plain.py` — one line per event. `ui/live.py` — Rich dashboard: header
   panel with project name and current stage, one progress bar per running
   stage, scrolling tail of the last 8 log lines. Degrades to `PlainReporter`
   when stdout is not a TTY.
6. `tools.py` — `ExternalTool` with name, binary candidates, version command,
   and `resolve()` capturing the version string. Register `vina`, `gnina`,
   `aizynthcli`. Missing tool raises `ToolNotFound` with an install hint.
7. `stages/base.py` — the `Stage` ABC and `Requirements` exactly as specified
   in `docs/ARCHITECTURE.md`, plus `StageRunner` handling run keys, resume,
   the thread pool, result writing, events and completion.
8. `stages/registry.py` — name to class mapping.
9. A `DemoStage` in tests that sleeps briefly per item.
10. `cli.py` — `vscreen init NAME`, `vscreen status`, and a hidden
    `vscreen demo` running `DemoStage` over 50 fake items.
11. `tests/unit/` — config round-trip; `stage_hash` determinism and
    sensitivity to each input; store CRUD; `StageRunner` resumability (run 20
    items, kill after 10, re-run, assert exactly 10 more execute).

Acceptance:

- `vscreen init demo && cd demo && vscreen status` prints an empty table
- `vscreen demo` shows a live dashboard that completes
- Ctrl-C during `vscreen demo`, then re-run, resumes
- `pytest` green in under 30 seconds, no network, no external binaries

Do not add cheminformatics, fetch code, or any stage beyond `DemoStage`.

---

## Phase 1 — database fetch and target input

Six input modes, one internal representation.

Build:

1. `fetch/cache.py` — disk cache keyed by URL hash under the project's
   `cache/`, with TTL and force-refresh. Every fetch module goes through it.
   Tests prime it from `tests/fixtures/` so nothing touches the network.
2. `fetch/uniprot.py` — `search(query)` returning accession, gene name,
   protein name, organism, length; `get(accession)` returning the record with
   sequence and PDB cross-references. Handle zero hits gracefully.
3. `fetch/pdb.py` — `structures_for_uniprot(accession)` returning pdb_id,
   resolution, method, title, bound ligand chem_comp_ids, chain mapping;
   `download(pdb_id, fmt)` to the cache. RCSB REST and GraphQL.
4. `fetch/chembl.py` — `actives_for_target(uniprot, pchembl_min,
   confidence_min, activity_types)` returning a dataframe, aggregating repeat
   measurements per compound to the median pChEMBL.
5. `core/prep.py` — load structure, select chain, strip waters and ions,
   extract and save the cognate ligand, add hydrogens, protonate at a given
   pH. `define_box_from_ligand` (padding default 8 Å),
   `define_box_from_residues`, `define_box_explicit`. Write receptor PDBQT.
   Record source, fetch date, chain, pH, box method and tool versions in
   `provenance_json`.
6. `stages/target_add.py` supporting `--uniprot`, `--pdb`, `--file`,
   `--fasta`, `--cds`, `--mutate`. A FASTA or CDS input emits a Colab
   notebook for folding and stops — correct for this phase.

   **Set `has_cognate_ligand` correctly.** When false, the target record
   carries a warning that the re-docking gate cannot run, and
   `vscreen target show` displays it prominently.
7. CLI: `target search`, `target structures`, `target add`, `target show`,
   `target list`.
8. Tests against cached fixtures for all three APIs; box definition from a
   fixture structure; a test asserting a FASTA-derived target has
   `has_cognate_ligand` false and carries the warning.

Acceptance:

- `vscreen target search EGFR` prints a table including P00533
- `vscreen target structures P00533` lists structures and flags ligand-bound
- `vscreen target add --pdb 4I22 --box-from-ligand` writes receptor.pdbqt,
  box.json and provenance.json, rendered by `target show`
- any fetch re-runs successfully with the network disabled
- `pytest` green, no network

Do not implement docking, folding, or pocket detection.

---

## Phase 2 — ligand prep, docking, re-docking check

Budget extra time. Format conversion is where docking pipelines lose days.

Build:

1. `core/standardize.py` — parse, sanitize, strip salts and solvents keeping
   the largest organic fragment, neutralize, canonical tautomer, preserve
   given stereochemistry, canonical SMILES, InChIKey. Return
   `StandardizeResult(ok, canonical_smiles, inchikey, warnings)`. Never raise
   on a bad molecule; record `parse_ok = 0` and the reason.
2. `core/ligprep.py` — ETKDGv3 embedding, MMFF or UFF minimization, PDBQT via
   meeko. Configurable conformer count, default 1. Cache by InChIKey.
3. `core/dock.py` — Vina through `tools.py`. Config: exhaustiveness
   (default 8), num_modes (9), seed, energy_range. Parse into `DockResult`
   (best score, all mode scores, pose path).

   **One Vina process per compound with `cpu=1`, parallelised across
   compounds by `StageRunner`.** Do not hand Vina a large core count for a
   single ligand. Set and record the seed.
4. `stages/dock_stage.py` — item is one compound, fully resumable. Poses to
   `poses/<stage_run_id>/<compound_id>.pdbqt`. Metrics: `vina_score`,
   `vina_modes_json`, `pose_path`.
5. `core/redock.py` — extract the cognate ligand, standardize and prepare it
   as if unknown, dock into its own receptor, compute **symmetry-corrected**
   heavy-atom RMSD with RDKit `GetBestRMS`. Return `RedockResult(rmsd_top,
   rmsd_best_of_n, passed)` at a configurable threshold, default 2.0 Å.
   Refuse with a clear message when `has_cognate_ligand` is false.
6. CLI: `compounds add --smiles-file FILE --set NAME`, `compounds list`,
   `dock --set NAME --target ID`, `redock --target ID`.
7. Tests: standardization of salts, mixtures, charged species, a broken
   SMILES, and a duplicate pair collapsing to one InChIKey; dock resumability
   (20 ligands, kill after 8, assert exactly 12 more Vina calls); redock on
   the fixture asserting RMSD under 2.0; unit tests with subprocess mocked
   plus a `@pytest.mark.integration` test using real Vina.

Acceptance:

- `vscreen redock --target <fixture>` prints an RMSD and a PASS verdict
- `vscreen dock` on 20 compounds shows the dashboard and resumes after
  interruption
- same compound, same seed, same score
- `pytest` green; integration tests skip cleanly when Vina is absent

Do not implement GNINA, PoseBusters, or any filter.

---

## Phase 3 — reference sets and the validate gate

The scientific heart. When this passes, both papers have their foundation.

Build:

1. `sets/actives.py` — ChEMBL actives by UniProt. Filter on pChEMBL (default
   7.0), assay confidence (default 8), activity types IC50/Ki/Kd, median
   aggregation. Cluster by Bemis-Murcko scaffold then Butina on ECFP4, and
   **sample stratified across clusters**: a random draw of EGFR actives is
   almost all quinazolines and would measure one chemotype rather than
   actives generally. Flag likely covalent binders by acrylamide and related
   warhead SMARTS and store the flag as a metric — do not drop them silently.
2. `sets/decoys.py` — property-matched decoys built locally. For each active,
   sample N decoys (default 50) matched within configurable tolerances on MW,
   cLogP, net charge, rotatable bonds, HBD, HBA, while enforcing ECFP4
   Tanimoto below a dissimilarity threshold (default 0.35) against every
   active. Record exact criteria in `provenance_json`. Also provide a DUD-E
   importer for secondary comparison.
3. `sets/drugs.py` — loader for a hand-curated YAML of approved drugs per
   target: name, SMILES, covalent flag.
4. `sets/random_set.py` — unmatched random drug-like sample as a baseline.
5. `analysis/enrichment.py` — `ef_at(scores, labels, fraction)` for 1% and
   5%, `roc_auc`, `bedroc(alpha=20)`, bootstrap CIs (default 1000 resamples).
   Pure functions over arrays, unit tested against hand-computed values.
6. `stages/validate.py` — the gate. Re-dock the cognate ligand; dock actives
   plus decoys; report EF1%, EF5%, AUC, BEDROC with CIs. Verdict `PASS` only
   if `rmsd <= 2.0 AND ef1 >= 10.0`, both configurable. When
   `has_cognate_ligand` is false, report enrichment but mark the verdict
   `INCOMPLETE`, never `PASS`, and explain why.
7. CLI: `sets build actives|decoys|drugs|random`, `sets list`,
   `validate --target ID`. `validate` prints a summary panel and exits
   non-zero on failure so it can gate a script.
8. Tests: enrichment maths against hand-computed cases including EF
   saturating at the actives-to-decoys ratio; stratified sampling spreading
   across clusters; decoy matching respecting every tolerance and the
   dissimilarity floor; `validate` returning `INCOMPLETE` for a target with
   no cognate ligand.

Acceptance:

- `vscreen validate --target <fixture>` prints RMSD, EF1%, AUC with CIs and a
  one-word verdict
- non-zero exit when a gate fails
- `pytest` green

Do not implement any other checklist stage.

---

## Phase 4 — the checklist filter stages

Five independent stages sharing one shape.

Build:

1. `core/filters.py` + `stages/filters_stage.py` — PAINS A/B/C via RDKit
   `FilterCatalog`, plus Brenk and NIH. Metrics: `pains_alerts`,
   `pains_names`, `brenk_alerts`, `nih_alerts`, `filter_pass`. Property
   envelope: MW, cLogP, TPSA, HBD, HBA, rotatable bonds, aromatic rings,
   heavy atoms, QED, fraction sp3.

   **Properties are reported, never pass/fail.** Emit them as metrics; let
   analysis decide. Do not invent a drug-likeness verdict.
2. `core/posecheck.py` + stage — PoseBusters per pose against its receptor.
   Store **one boolean per individual test** plus `pb_pass_all`; the paper
   reports which specific tests fail most often.
3. `core/retrosynth.py` + stage — AiZynthFinder through `tools.py`, hard
   timeout (default 300 s) and depth budget (default 6). Metrics:
   `retro_solved`, `retro_route_length`, `retro_n_precursors`,
   `retro_time_s`, `retro_route_json`. A timeout records `retro_solved=0`
   with a distinct reason, never a crash. Slow — test resumability properly.
4. `core/novelty.py` + stage — max ECFP4 Tanimoto against a named reference
   set. Metrics: `novelty_max_tanimoto`, `novelty_nearest_id`,
   `novelty_is_near_duplicate` (default cut 0.85). Use
   `BulkTanimotoSimilarity`; precompute reference fingerprints once per
   stage run.
5. `core/rescore.py` + stage — GNINA CNN rescoring of existing poses.
   **Optional**: when the binary is absent, skip with a clear message and
   record nothing rather than failing the run. Metrics: `gnina_cnn_score`,
   `gnina_cnn_affinity`.
6. CLI: `filter`, `posecheck`, `retro`, `novelty`, `rescore`, each taking
   `--set` and `--target`. Plus `compound show ID` rendering every metric for
   one compound grouped by stage — the main debugging tool from here on.
7. Tests: fixture compounds with known PAINS matches; a known unsynthesizable
   molecule; a near-duplicate pair for the novelty cut; resumability for the
   retro stage specifically.

Acceptance:

- each command runs on the fixture set and writes metrics
- `vscreen compound show <id>` renders all stages' metrics in one panel
- GNINA absent causes a clean skip, not a failure
- `pytest` green

Do not implement battery orchestration or comparison analysis.

---

## Phase 5 — audit commands and comparison analysis

This turns the pieces into Paper 1.

Build:

1. `audit/study.py` — CRUD over `studies` plus a `StudyRecord` pydantic model.
   Validate DOI format; `recovery_tier` and `validation_reported` are enums.
2. `audit/ingest.py` — read a CSV of `compound_label, smiles` (extra columns
   preserved as notes), standardize each, detect duplicates within the study
   and across the corpus by InChIKey (**report** cross-study matches, do not
   silently merge), assign IDs `<study_tag>-C<nn>`, record tier and source.

   A study with no recoverable structures is still a valid record with tier
   `not_recoverable` and zero compounds. That count is a headline result and
   must never be lost.
3. `audit/battery.py` — ordered execution with a per-stage gate rule:
   continue-all or continue-survivors-only. Default: stages 1–6 on
   everything, 7–9 on survivors only. Record `first_failure_stage` per
   compound. Emit events so the dashboard shows per-stage pass counts.
4. `analysis/survival.py` — `survival_by_stage(set_id)` with Wilson 95% CIs;
   `first_failure_distribution(set_id)`. Return tidy dataframes.
5. `analysis/stats.py` — `two_proportion_test` (risk difference, CI, p),
   `fisher_exact` wrapper, `kruskal_dunn`, `cochran_armitage`,
   `benjamini_hochberg`, `cliffs_delta`. Pure, unit tested against
   hand-computed or scipy-verified values. **Always return effect size and CI
   alongside p.**
6. CLI: `corpus add`, `corpus ingest`, `corpus status`, `audit run`,
   `audit compare --vs actives,decoys,own,random`. `audit compare` prints a
   horizontal bar summary plus a table of pairwise tests with effect sizes,
   CIs and BH-adjusted p-values.
7. Tests: ingestion of a fixture CSV with a deliberate duplicate and a
   malformed SMILES; battery gating (assert expensive stages see only
   survivors); every statistical function against known values.

Acceptance:

- the full sequence works on fixtures: corpus add, ingest, audit run, compare
- `vscreen audit run` renders a per-stage dashboard with pass counts
- `vscreen compound show` explains exactly why a compound failed and where
- `pytest` green

Do not implement figures. Printed tables are enough here.

---

## Phase 6 — active learning loop

Makes a 100k library tractable on a laptop. Needed for Paper 2, not Paper 1.

Context: dock a small batch, train a cheap model to predict docking scores
from fingerprints, use it to pick the next batch, repeat. **The model
imitates the docking program; it does not predict biology.**

Build:

1. `al/featurize.py` — ECFP4 (radius 2, 2048 bits) as a numpy matrix,
   computed once per library and cached to `.npy` keyed by a hash of the
   compound id list.
2. `al/surrogate.py` — a `Surrogate` protocol with `fit(X, y)` and
   `predict(X) -> (mean, std)`. `RidgeSurrogate` (std from bootstrap
   ensemble) and `LGBMSurrogate` (std from quantile models or a seed
   ensemble). Default LGBM. Both train in under 10 s on 10k × 2048.
3. `al/acquire.py` — `greedy`, `epsilon_greedy(eps=0.2)`, `ucb(kappa)`.

   **Vina scores are negative and lower is better.** Get the UCB sign right
   and write a test that asserts it.
4. `al/loop.py` — inputs: library ids, budget, n_rounds, seed_size, strategy,
   seed. Round 0 docks a random sample; each later round retrains from
   scratch on all labels, predicts over undocked compounds, acquires, docks.
   Checkpoint after every round (model state, labels, round number) so a
   killed run resumes at the round boundary. Record per round: n_docked, best
   score so far, cross-validated R² and Spearman ρ on held-out labels.
5. `analysis/recall.py` — given a library with known true scores, report what
   fraction of the true top 1% and top 100 the loop found for a given budget.
   A first-class reportable output, not a notebook one-off.
6. CLI: `dock --strategy active-learning --budget 10000 --rounds 5
   --seed-size 2000`. Dashboard shows current round, budget spent and
   remaining, model R² and ρ per round, best score found.
   `al recall --set NAME --target ID --budget N`.
7. Tests: on a 2000-compound fixture library with synthetic scores, assert
   recovery of ≥80% of the true top 1% within a 20% budget; checkpoint and
   resume at a round boundary; the UCB sign convention; determinism under a
   fixed seed.

Acceptance:

- the recall test passes at the stated threshold
- the dashboard shows per-round model quality
- killing mid-loop and re-running resumes at the correct round
- `pytest` green

Do not use active learning anywhere in the audit path.

---

## Phase 7 — molecular dynamics and remote runners

The only phase needing hardware not present. It emits jobs rather than
running them, and is fully testable without a GPU.

Build:

1. `core/md.py` — OpenMM for one protein-ligand complex. System build:
   ligand parameterization via OpenFF or GAFF, Amber14 protein force field,
   TIP3P water with 10 Å padding, neutralizing ions. Minimize, NVT, NPT, then
   production. Config: `ns_production` (50), `n_replicates` (3), timestep,
   temperature, per-replicate seed. Checkpoint every `checkpoint_ns` (5)
   using OpenMM's native checkpoint and resume from the latest. Analysis:
   ligand heavy-atom RMSD per frame after aligning on protein backbone,
   fraction of frames in pocket, mean RMSD over the final half. Metrics:
   `md_rmsd_mean_final`, `md_frac_in_pocket`, `md_replicates_passing`,
   `md_stable` (mean RMSD under 3.0 Å in ≥2 of 3 replicates).
2. `stages/md_stage.py` — item is (compound, replicate). **Resumable at
   checkpoint granularity**: a replicate killed at 30 of 50 ns resumes at 30.
3. `runners/base.py` — `Runner` protocol: `prepare`, `submit`, `collect`.
   Formalize the local runner here.
4. `runners/colab.py` — emits a self-contained notebook that installs
   dependencies, mounts Drive, pulls inputs, runs the named stage, and writes
   a JSON results bundle. Plus `vscreen results import FILE` merging it back,
   **validating that the run_key matches** — without that check, results
   computed under one configuration merge silently into a project configured
   differently.
5. `runners/osg.py` — HTCondor submit bundle for OSPool: one job per
   compound; an Apptainer container definition; a submit file with
   requirements, `transfer_input_files`, `transfer_output_files`. Respect
   OSPool constraints: jobs complete or checkpoint within 20 hours, under
   20 GB input plus output per job, software travels with the job. Plus a
   collect command merging returned files.
6. `Containerfile` at the repo root building the full stack, used by OSPool
   and for reproducibility of the published pipeline.
7. CLI: `simulate --set NAME --target ID [--ns 50 --replicates 3]`,
   `runner export --stage STAGE --target colab|osg --out DIR`,
   `results import FILE`.

   `simulate` with no GPU prints a clear message, emits the Colab notebook,
   and exits 0 — it does not crash and does not run on CPU.
8. Tests: MD on a tiny fixture system for 0.01 ns asserting completion and
   metrics (runs on CPU in seconds); checkpoint resume asserting correct
   total simulated time and no repeated work; the no-GPU path emitting a
   notebook and exiting cleanly; the OSG bundle containing every needed file;
   `results import` rejecting a mismatched run_key.

Acceptance:

- `vscreen simulate` on a laptop emits a Colab notebook and exits 0
- the tiny-system MD test passes on CPU
- `vscreen runner export --target osg` produces a bundle passing
  `condor_submit -dry-run` where available, otherwise structurally validated
- `pytest` green

---

## Phase 8 — report and figure generation

Every exhibit in both paper plans, generated from the database alone. If a
number appears in a paper, a script here produced it.

Build:

1. `report/tables.py` — CSV exports: `studies.csv` (one row per study, every
   field); `compounds.csv` (all metrics pivoted wide, with the stage_run each
   came from); `survival.csv` (per-set per-stage pass counts with Wilson
   CIs); `tests.csv` (every hypothesis test with effect size, CI, raw and
   BH-adjusted p).
2. `report/figures.py` — matplotlib, one function per figure, each taking a
   dataframe and returning a Figure. Colourblind-safe, greyscale-readable,
   300 dpi, sentence case, no chartjunk.

   Paper 1: `prisma_flow`, `survival_funnel` (one line per set with CI
   bands), `first_failure_stacked` (split by model class),
   `property_distributions`, `novelty_vs_score` (Tanimoto on x, GNINA score
   on y, 0.85 line marked), `pass_rate_by_year` (CIs plus trend),
   `md_traces` (a pass, a fail, a borderline).

   Paper 2: `study_design`, `dose_response` (small multiples, both lines,
   gefitinib overlaid), `predicted_vs_measured` (censored compounds marked at
   the bound, Spearman ρ annotated), `hit_rate_by_arm` (Wilson CIs, n on each
   bar), `selectivity` (line of identity, threefold band).
3. `report/assay.py` — CSV schema for importing measured assay results:
   `compound_code, cell_line, replicate, concentration, readout`. Four-
   parameter logistic fitting via scipy returning IC50 with 95% CI, Hill
   slope, R². **Right-censor** compounds with no measurable activity at the
   top dose; never impute them as the top concentration.
4. CLI: `report build [--paper 1|2] --out DIR`, `report tables --out DIR`,
   `assay import FILE`.
5. Tests: every figure function renders on fixture data and writes a
   non-trivial PNG; the curve fitter recovers a known IC50 from synthetic
   data within tolerance; the censoring rule tested explicitly.

Acceptance:

- `vscreen report build --paper 1` writes every Paper 1 figure and CSV from
  the fixture database
- every figure legible at 300 dpi and in greyscale
- `pytest` green

Do not hand-tune any figure. If it needs adjusting, adjust the function.
