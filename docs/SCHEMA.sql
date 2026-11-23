-- vscreen SQLite schema.
--
-- Design note: `results` is NARROW — one row per compound per metric per
-- stage run. Re-running a stage with different settings creates a second
-- stage_run and a second set of rows, preserving the first. A wide table
-- would overwrite history and make an earlier figure impossible to
-- reproduce. Reports pivot narrow -> wide with a single query.
--
-- Enable on every connection:
--   PRAGMA journal_mode = WAL;
--   PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS targets (
    id                  TEXT PRIMARY KEY,          -- 'egfr-4i22'
    uniprot             TEXT,
    pdb_id              TEXT,
    source_kind         TEXT NOT NULL,             -- pdb|uniprot|file|fasta|cds|mutant
    sequence_sha        TEXT,
    conformation        TEXT,                      -- free text, e.g. 'DFG-in, aC-out'
    box_json            TEXT NOT NULL,             -- {center:[x,y,z], size:[x,y,z], method}
    receptor_path       TEXT NOT NULL,
    has_cognate_ligand  INTEGER NOT NULL,          -- 0 blocks the re-docking gate
    cognate_ligand_path TEXT,
    provenance_json     TEXT NOT NULL,             -- source, fetch date, chain, pH, tools
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compounds (
    id                TEXT PRIMARY KEY,            -- 'omega26-C01'
    inchikey          TEXT NOT NULL,
    smiles_input      TEXT NOT NULL,
    smiles_canonical  TEXT,
    parse_ok          INTEGER NOT NULL,            -- 0 is a data point, not an error
    parse_note        TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compounds_inchikey ON compounds(inchikey);

CREATE TABLE IF NOT EXISTS compound_sets (
    id               TEXT PRIMARY KEY,             -- 'corpus' | 'actives:P00533'
    kind             TEXT NOT NULL,                -- corpus|actives|decoys|drugs|own|random
    target_id        TEXT,                         -- NULL for target-independent sets
    provenance_json  TEXT NOT NULL,                -- query params, filters, seed, criteria
    created_at       TEXT NOT NULL,
    FOREIGN KEY (target_id) REFERENCES targets(id)
);

CREATE TABLE IF NOT EXISTS set_members (
    set_id       TEXT NOT NULL,
    compound_id  TEXT NOT NULL,
    PRIMARY KEY (set_id, compound_id),
    FOREIGN KEY (set_id)      REFERENCES compound_sets(id) ON DELETE CASCADE,
    FOREIGN KEY (compound_id) REFERENCES compounds(id)     ON DELETE CASCADE
);

-- Audit workflow only (Paper 1).
CREATE TABLE IF NOT EXISTS studies (
    id                   TEXT PRIMARY KEY,         -- 'omega26'
    doi                  TEXT,
    year                 INTEGER,
    venue                TEXT,
    venue_kind           TEXT,                     -- journal|preprint
    model_class          TEXT,                     -- rnn|vae|gan|transformer|diffusion|rl|ga|other
    target_id            TEXT,
    authors_pdb          TEXT,                     -- structure the authors reported using
    n_proposed           INTEGER,                  -- candidates, not the full generated set
    recovery_tier        TEXT NOT NULL,            -- tier1|tier2|tier3|not_recoverable
    recovery_source      TEXT,                     -- e.g. 'Supplementary Table S3'
    validation_reported  TEXT NOT NULL,            -- none|biochemical|cellular|structural|in_vivo
    synthesis_reported   INTEGER,
    code_available       TEXT,                     -- none|on_request|public
    notes                TEXT,
    created_at           TEXT NOT NULL,
    FOREIGN KEY (target_id) REFERENCES targets(id)
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id                  TEXT PRIMARY KEY,
    stage               TEXT NOT NULL,
    run_key             TEXT NOT NULL UNIQUE,      -- hash(stage + config + inputs + tool versions)
    target_id           TEXT,
    set_id              TEXT,
    config_json         TEXT NOT NULL,
    tool_versions_json  TEXT NOT NULL,             -- rule 5: never empty
    status              TEXT NOT NULL,             -- pending|running|complete|failed
    error_text          TEXT,
    started_at          TEXT,
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_stage ON stage_runs(stage);

CREATE TABLE IF NOT EXISTS results (
    stage_run_id  TEXT NOT NULL,
    compound_id   TEXT NOT NULL,
    metric        TEXT NOT NULL,                   -- 'vina_score' | 'pb_pass_all' | ...
    value_num     REAL,
    value_text    TEXT,
    PRIMARY KEY (stage_run_id, compound_id, metric),
    FOREIGN KEY (stage_run_id) REFERENCES stage_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (compound_id)  REFERENCES compounds(id)  ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_results_metric      ON results(metric);
CREATE INDEX IF NOT EXISTS idx_results_compound    ON results(compound_id);

-- Per-item completion ledger, so a stage resumes at item granularity
-- rather than restarting. Written before results, so a crash between the
-- two is detectable.
CREATE TABLE IF NOT EXISTS stage_items (
    stage_run_id  TEXT NOT NULL,
    item_id       TEXT NOT NULL,                   -- usually compound_id, or 'compound:replicate'
    status        TEXT NOT NULL,                   -- pending|running|done|failed|skipped
    attempts      INTEGER NOT NULL DEFAULT 0,
    error_text    TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (stage_run_id, item_id),
    FOREIGN KEY (stage_run_id) REFERENCES stage_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_stage_items_status ON stage_items(stage_run_id, status);
