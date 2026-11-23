"""The ``target add`` workflow, implemented as a one-item stage.

Six input modes, one internal representation (``docs/PHASES.md``):

- ``--uniprot ACCESSION`` — pick a PDB from the UniProt cross-references
- ``--pdb PDB_ID`` — a concrete PDB entry
- ``--file PATH`` — a user-supplied PDB on disk
- ``--fasta PATH`` — a FASTA sequence; emits a Colab notebook and stops
- ``--cds PATH`` — a coding sequence; emits a Colab notebook and stops
- ``--mutate PDB_ID:MUTATIONS`` — a point-mutant variant (Phase 1: record only)

``has_cognate_ligand`` is set from what ``core/prep.clean_structure`` actually
finds in the chosen chain. When false, the Target carries a warning on its
provenance that the re-docking gate cannot run, which ``vscreen target show``
renders prominently (rule: a predicted or apo structure is never silently
treated as equivalent to a holo crystal structure).
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import store
from ..config import utc_now_iso
from ..core import prep
from ..errors import StageError, VscreenError
from ..fetch import pdb as pdb_fetch
from ..fetch.cache import DiskCache
from .base import Context, Item, Requirements, Result, Stage

__all__ = [
    "TargetAddPlan",
    "TargetAddStage",
    "NO_COGNATE_LIGAND_WARNING",
    "CognateLigandMissing",
    "render_folding_notebook",
]

log = logging.getLogger(__name__)

NO_COGNATE_LIGAND_WARNING: str = (
    "No cognate crystallographic ligand was found. The re-docking gate cannot "
    "run against this target; validation will report INCOMPLETE and nothing "
    "downstream of the gate is permitted to run (docs/ARCHITECTURE.md)."
)


BoxMethod = Literal["from-ligand", "explicit"]
SourceKind = Literal["pdb", "uniprot", "file", "fasta", "cds", "mutant"]


class CognateLigandMissing(VscreenError):
    """A box method depending on the cognate ligand was requested but none exists."""


@dataclass(frozen=True)
class TargetAddPlan:
    """Everything ``target add`` needs that is not already in the project config."""

    target_id: str
    source_kind: SourceKind
    pdb_id: str | None = None
    uniprot: str | None = None
    input_file: Path | None = None
    chain: str | None = None
    ph: float = 7.4
    box_method: BoxMethod = "from-ligand"
    box_padding_a: float = prep.DEFAULT_BOX_PADDING_A
    box_center: tuple[float, float, float] | None = None
    box_size: tuple[float, float, float] | None = None
    ligand_resname: str | None = None
    mutations: tuple[str, ...] = ()

    @property
    def needs_folding(self) -> bool:
        return self.source_kind in ("fasta", "cds")


class TargetAddStage(Stage):
    """Resolve the plan, prepare the receptor, write the target row."""

    name = "target_add"
    requires = Requirements(cpu_cores=1, external_tools=(), needs_network=False)
    writes_results = False  # Writes to the targets table, not results.

    def __init__(self, plan: TargetAddPlan, *, cache: DiskCache | None = None) -> None:
        self.plan = plan
        self.cache_override = cache
        # Populated in run_item (worker thread) and drained in finalize (main
        # thread): a SQLite connection may only be used by the thread that
        # opened it, so the DB write is deferred (stages/base.py).
        self._pending_target: store.Target | None = None

    # ------------------------------------------------------------------ Stage

    def iter_items(self, ctx: Context) -> Iterable[Item]:
        if self.plan.needs_folding:
            # These modes terminate with a Colab notebook rather than a target row.
            return [Item(id=f"fold:{self.plan.target_id}")]
        return [Item(id=self.plan.target_id)]

    def is_item_done(self, ctx: Context, item: Item) -> bool:
        # A target row in the store is the ground truth for "this target is
        # done"; the per-item ledger from StageRunner is checked too so a
        # partial run shows up as resumable rather than overwriting an
        # already-complete target with a fresh prep.
        if store.get_target(ctx.conn, self.plan.target_id) is not None:
            return True
        return super().is_item_done(ctx, item)

    def run_item(self, ctx: Context, item: Item) -> list[Result]:
        plan = self.plan
        if plan.needs_folding:
            out = render_folding_notebook(ctx, plan)
            self._pending_target = _pending_fold_target_row(
                ctx, plan, notebook_path=out, tool_versions=dict(ctx.tool_versions)
            )
            # No Result rows: target_add writes to the targets table, not
            # results (which has a FK to compounds and belongs to compound
            # work). Everything a reader needs is in provenance_json.
            return []

        cache = self.cache_override or DiskCache(ctx.path("cache"))
        pdb_text, provenance_source = self._load_structure_text(ctx, cache)
        chain = self._pick_chain(plan, pdb_text)
        cleaned = prep.clean_structure(
            pdb_text,
            chain=chain,
            source_pdb_id=plan.pdb_id,
            ligand_resname=plan.ligand_resname,
        )
        box = self._define_box(plan, cleaned)

        target_dir = ctx.path("targets", plan.target_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        receptor_path = target_dir / "receptor.pdbqt"
        cognate_path: Path | None = None
        raw_pdb_path = target_dir / "source.pdb"
        raw_pdb_path.write_text(cleaned.protein_pdb_text(), encoding="utf-8")

        if cleaned.has_cognate_ligand:
            cognate_path = target_dir / "cognate_ligand.pdb"
            cognate_path.write_text(cleaned.ligand_pdb_text(), encoding="utf-8")

        prep.write_receptor_pdbqt(cleaned.protein_pdb_text(), receptor_path, ph=plan.ph)

        provenance = _build_provenance(
            plan=plan,
            cleaned=cleaned,
            box=box,
            provenance_source=provenance_source,
            tool_versions=dict(ctx.tool_versions),
        )
        box_path = target_dir / "box.json"
        box_path.write_text(
            json.dumps(box.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        provenance_path = target_dir / "provenance.json"
        provenance_path.write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        self._pending_target = store.Target(
            id=plan.target_id,
            uniprot=plan.uniprot,
            pdb_id=plan.pdb_id,
            source_kind=plan.source_kind,
            sequence_sha=None,
            conformation=None,
            box_json=json.dumps(box.to_dict(), sort_keys=True),
            receptor_path=str(receptor_path.relative_to(ctx.project_dir)),
            has_cognate_ligand=1 if cleaned.has_cognate_ligand else 0,
            cognate_ligand_path=(
                str(cognate_path.relative_to(ctx.project_dir)) if cognate_path else None
            ),
            provenance_json=json.dumps(provenance, sort_keys=True),
            created_at=utc_now_iso(),
        )

        # No Result rows: target_add writes to the targets table, not results
        # (which has a FK to compounds and belongs to compound work).
        return []

    def finalize(self, ctx: Context) -> None:
        """Insert the Target row on the main thread (sqlite3 is thread-affine)."""
        if self._pending_target is None:
            return
        with store.transaction(ctx.conn):
            store.insert_target(ctx.conn, self._pending_target, replace=True)
        self._pending_target = None

    # ------------------------------------------------------------------ helpers

    def _load_structure_text(
        self, ctx: Context, cache: DiskCache
    ) -> tuple[str, dict[str, str]]:
        plan = self.plan
        if plan.source_kind == "file":
            if plan.input_file is None:
                raise StageError("target add --file requires an input path")
            source_path = Path(plan.input_file).expanduser().resolve()
            text = prep.load_pdb_text(source_path)
            return text, {"origin": "local-file", "path": str(source_path)}

        if plan.source_kind == "mutant":
            if plan.input_file is None:
                raise StageError("target add --mutate requires a template PDB on disk for Phase 1")
            text = prep.load_pdb_text(Path(plan.input_file))
            return text, {
                "origin": "mutant-template",
                "path": str(plan.input_file),
                "mutations": ",".join(plan.mutations),
            }

        pdb_id = plan.pdb_id
        if plan.source_kind == "uniprot":
            if not plan.uniprot:
                raise StageError("target add --uniprot requires an accession")
            summaries = pdb_fetch.structures_for_uniprot(cache, plan.uniprot)
            if not summaries:
                raise StageError(
                    f"No PDB structures cross-reference UniProt {plan.uniprot}. "
                    f"Pass --pdb with a specific entry or --fasta to fold the sequence."
                )
            pdb_id = _best_structure(summaries).pdb_id

        if not pdb_id:
            raise StageError(
                "target add could not determine a PDB id. Pass --pdb explicitly."
            )
        pdb_id = pdb_id.upper()
        target_cache_dir = ctx.path("targets", plan.target_id)
        target_cache_dir.mkdir(parents=True, exist_ok=True)
        out_path = pdb_fetch.download(cache, pdb_id, fmt="pdb", out_dir=target_cache_dir)
        text = out_path.read_text(encoding="utf-8")
        return text, {
            "origin": "rcsb",
            "pdb_id": pdb_id,
            "downloaded_to": str(out_path.relative_to(ctx.project_dir)),
        }

    def _pick_chain(self, plan: TargetAddPlan, pdb_text: str) -> str:
        if plan.chain:
            return plan.chain
        atoms = prep.atoms_of(pdb_text)
        chains = prep.split_chains(atoms)
        protein_chains = {
            chain_id: [a for a in atoms_ if a.record == "ATOM"]
            for chain_id, atoms_ in chains.items()
            if chain_id
        }
        if not protein_chains:
            raise StageError(
                "No protein chains (ATOM records) were found in the structure. "
                "Phase 1 requires the input to contain at least one polymer chain."
            )
        # Deterministic: the chain with the most protein atoms; ties broken alphabetically.
        chosen = max(protein_chains.items(), key=lambda kv: (len(kv[1]), -ord(kv[0])))[0]
        log.info("auto-selected chain %s (chains: %s)", chosen, sorted(protein_chains))
        return chosen

    def _define_box(self, plan: TargetAddPlan, cleaned: prep.CleanedStructure) -> prep.Box:
        if plan.box_method == "explicit":
            if plan.box_center is None or plan.box_size is None:
                raise StageError(
                    "Explicit box requires --box-center and --box-size "
                    "(scientific defaults are forbidden; see docs/CONVENTIONS.md)."
                )
            return prep.define_box_explicit(plan.box_center, plan.box_size)
        if plan.box_method == "from-ligand":
            if not cleaned.has_cognate_ligand:
                raise CognateLigandMissing(
                    "--box-from-ligand was requested but no cognate ligand is "
                    "present in the chosen chain. Pass --box-explicit or pick "
                    "a different chain/ligand."
                )
            return prep.define_box_from_ligand(
                cleaned.cognate_ligand_atoms, padding_a=plan.box_padding_a
            )
        raise StageError(f"Unknown box method {plan.box_method!r}")


# --------------------------------------------------------------------------- #
# Provenance helpers
# --------------------------------------------------------------------------- #


def _build_provenance(
    *,
    plan: TargetAddPlan,
    cleaned: prep.CleanedStructure,
    box: prep.Box,
    provenance_source: dict[str, str],
    tool_versions: dict[str, str],
) -> dict[str, object]:
    provenance: dict[str, object] = {
        "target_id": plan.target_id,
        "source_kind": plan.source_kind,
        "fetched_at": utc_now_iso(),
        "chain": cleaned.chain,
        "ph": plan.ph,
        "box": box.to_dict(),
        "source": provenance_source,
        "stripped": dict(sorted(cleaned.stripped_counts.items())),
        "tool_versions": dict(sorted(tool_versions.items())),
        "has_cognate_ligand": bool(cleaned.has_cognate_ligand),
    }
    if cleaned.cognate_ligand_resname:
        provenance["cognate_ligand_resname"] = cleaned.cognate_ligand_resname
    if plan.uniprot:
        provenance["uniprot"] = plan.uniprot
    if plan.pdb_id:
        provenance["pdb_id"] = plan.pdb_id
    if plan.mutations:
        provenance["mutations"] = list(plan.mutations)
    if not cleaned.has_cognate_ligand:
        provenance["warning"] = NO_COGNATE_LIGAND_WARNING
    return provenance


def _best_structure(summaries: list[pdb_fetch.StructureSummary]) -> pdb_fetch.StructureSummary:
    """Pick a structure: ligand-bound first, then best resolution, then alphabetical."""
    def key(s: pdb_fetch.StructureSummary) -> tuple[int, float, str]:
        resolution = s.resolution_a if s.resolution_a is not None else 99.0
        return (0 if s.has_bound_ligand else 1, resolution, s.pdb_id)

    return sorted(summaries, key=key)[0]


# --------------------------------------------------------------------------- #
# FASTA / CDS → Colab notebook (Phase 1 stops short of folding)
# --------------------------------------------------------------------------- #

def _folding_notebook(target_id: str, source_kind: str, input_name: str) -> dict[str, object]:
    return {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# vscreen target-add folding notebook\n",
                    "\n",
                    f"Target: `{target_id}`\n",
                    f"Source: `{source_kind}` from `{input_name}`\n",
                    "\n",
                    "This notebook folds the attached sequence on Colab's GPU runtime, "
                    "writes the predicted structure to `receptor.pdb`, and then calls "
                    "`vscreen target add --file receptor.pdb --box-explicit ...` locally. "
                    "vscreen will not auto-fold on a laptop: a predicted structure must "
                    "never be silently treated as equivalent to a holo crystal "
                    "(docs/ARCHITECTURE.md).",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "# TODO: fill in the sequence from the attached file and pick a "
                    "folding backend.\n",
                    "# Phase 7 lands AlphaFold/ESMFold invocation here; for Phase 1 "
                    "this notebook is a stub.",
                ],
            },
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _pending_fold_target_row(
    ctx: Context,
    plan: TargetAddPlan,
    *,
    notebook_path: Path,
    tool_versions: dict[str, str],
) -> store.Target:
    """Build (but do not insert) a provisional target row for a FASTA/CDS input.

    No receptor is ready, so ``has_cognate_ligand`` is 0 and the warning is on
    the provenance; ``target show`` renders it. ``receptor_path`` points at the
    folding notebook, so downstream stages that try to open it fail loudly
    rather than quietly dock a missing receptor.
    """
    relative_notebook = notebook_path.relative_to(ctx.project_dir)
    provenance: dict[str, object] = {
        "target_id": plan.target_id,
        "source_kind": plan.source_kind,
        "fetched_at": utc_now_iso(),
        "chain": None,
        "ph": plan.ph,
        "box": None,
        "source": {
            "origin": plan.source_kind,
            "input_file": str(plan.input_file) if plan.input_file else None,
        },
        "tool_versions": dict(sorted(tool_versions.items())),
        "has_cognate_ligand": False,
        "folding_notebook": str(relative_notebook),
        "status": "pending-fold",
        "warning": NO_COGNATE_LIGAND_WARNING,
    }
    return store.Target(
        id=plan.target_id,
        uniprot=plan.uniprot,
        pdb_id=plan.pdb_id,
        source_kind=plan.source_kind,
        sequence_sha=None,
        conformation=None,
        box_json=json.dumps({"method": "pending-fold"}, sort_keys=True),
        receptor_path=str(relative_notebook),
        has_cognate_ligand=0,
        cognate_ligand_path=None,
        provenance_json=json.dumps(provenance, sort_keys=True),
        created_at=utc_now_iso(),
    )


def render_folding_notebook(ctx: Context, plan: TargetAddPlan) -> Path:
    if plan.input_file is None:
        raise StageError(f"target add --{plan.source_kind} requires an input sequence file")
    target_dir = ctx.path("targets", plan.target_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    sequence_copy = target_dir / f"input.{plan.source_kind}"
    shutil.copyfile(plan.input_file, sequence_copy)
    notebook_path = target_dir / "folding.ipynb"
    notebook_path.write_text(
        json.dumps(
            _folding_notebook(plan.target_id, plan.source_kind, sequence_copy.name),
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    warning_path = target_dir / "README.md"
    warning_path.write_text(
        (
            f"# Target `{plan.target_id}` — folding required\n\n"
            f"The source is a {plan.source_kind.upper()} sequence; vscreen has not folded it. "
            "Run `folding.ipynb` on a GPU-equipped Colab runtime and come back with "
            "`receptor.pdb` before this target can be prepared.\n\n"
            f"{NO_COGNATE_LIGAND_WARNING}\n"
        ),
        encoding="utf-8",
    )
    return notebook_path
