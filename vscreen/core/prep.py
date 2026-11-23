"""Target preparation: clean a structure, define a docking box, write PDBQT.

Phase 1 responsibilities (``docs/PHASES.md``):

- Load a PDB structure; select a chain; strip waters and ions.
- Extract and save the cognate ligand when the structure carries one.
- Add hydrogens and write a Vina-ready receptor ``.pdbqt`` via meeko.
- Define the docking box from the cognate ligand, from residues, or explicitly.

The box is explicit everywhere: there is no invented default
(``docs/CONVENTIONS.md`` — invented scientific defaults are forbidden; they
silently corrupt every downstream result). Which box method was used, with
which padding, lands in ``provenance_json`` alongside the source, fetch date,
chain, pH and tool versions so a target is fully reproducible.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import VscreenError

__all__ = [
    "PrepError",
    "StandardResidue",
    "CleanedStructure",
    "Box",
    "StructureAtom",
    "load_pdb_text",
    "split_chains",
    "atoms_of",
    "select_chain",
    "strip_waters_and_ions",
    "extract_cognate_ligand",
    "clean_structure",
    "define_box_from_ligand",
    "define_box_from_residues",
    "define_box_explicit",
    "write_receptor_pdbqt",
    "DEFAULT_BOX_PADDING_A",
    "WATER_RESIDUES",
    "ION_RESIDUES",
]

log = logging.getLogger(__name__)

DEFAULT_BOX_PADDING_A: float = 8.0

#: Canonical amino-acid three-letter codes. Non-standard residues are kept but
#: flagged when a chain is selected; a mutant kinase with an MSE for a SEC is
#: still a protein chain and must not be silently dropped.
STANDARD_RESIDUES: frozenset[str] = frozenset(
    {
        "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS",
        "ILE", "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP",
        "TYR", "VAL",
    }
)

WATER_RESIDUES: frozenset[str] = frozenset({"HOH", "WAT", "DOD", "H2O"})
ION_RESIDUES: frozenset[str] = frozenset(
    {
        "NA", "K", "LI", "RB", "CS",
        "MG", "CA", "ZN", "FE", "MN", "CU", "NI", "CO", "CD", "HG", "AL",
        "CL", "BR", "I", "F",
        "SO4", "PO4", "ACT", "FMT", "EDO", "GOL", "PEG", "PGE", "DMS",
    }
)


class PrepError(VscreenError):
    """Structure preparation could not continue."""


# --------------------------------------------------------------------------- #
# Structure types. A minimal PDB atom representation is enough for Phase 1;
# the next phase reaches for RDKit when stereochemistry gets involved.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StructureAtom:
    """One atom from an ``ATOM`` or ``HETATM`` record."""

    record: str  # 'ATOM' or 'HETATM'
    serial: int
    name: str
    alt_loc: str
    res_name: str
    chain: str
    res_seq: int
    i_code: str
    x: float
    y: float
    z: float
    occupancy: float
    temp_factor: float
    element: str
    line: str

    @property
    def is_hetatm(self) -> bool:
        return self.record == "HETATM"


@dataclass(frozen=True)
class StandardResidue:
    """A small pointer to one residue by its chain id and numbering."""

    chain: str
    res_seq: int
    i_code: str = ""


@dataclass(frozen=True)
class Box:
    """Docking box in Angstroms. Center + size + how it was derived."""

    center: tuple[float, float, float]
    size: tuple[float, float, float]
    method: str

    def to_dict(self) -> dict[str, Any]:
        return {"center": list(self.center), "size": list(self.size), "method": self.method}


@dataclass(frozen=True)
class CleanedStructure:
    """A parsed and filtered PDB. Order is preserved to keep line numbers stable."""

    source_pdb_id: str | None
    chain: str
    protein_atoms: tuple[StructureAtom, ...]
    cognate_ligand_atoms: tuple[StructureAtom, ...] = field(default_factory=tuple)
    cognate_ligand_resname: str | None = None
    stripped_counts: dict[str, int] = field(default_factory=dict)

    @property
    def has_cognate_ligand(self) -> bool:
        return len(self.cognate_ligand_atoms) > 0

    def protein_pdb_text(self) -> str:
        return _atoms_to_pdb_text(self.protein_atoms)

    def ligand_pdb_text(self) -> str:
        if not self.cognate_ligand_atoms:
            raise PrepError("No cognate ligand to serialise")
        return _atoms_to_pdb_text(self.cognate_ligand_atoms)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def load_pdb_text(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise PrepError(f"PDB file not found: {path}")
    return path.read_text(encoding="utf-8")


def _parse_atom_line(line: str) -> StructureAtom | None:
    record = line[:6].strip()
    if record not in ("ATOM", "HETATM"):
        return None
    if len(line) < 54:  # a valid PDB atom line is at least 54 chars
        return None
    try:
        serial = int(line[6:11].strip() or "0")
        name = line[12:16].strip()
        alt_loc = line[16:17].strip()
        res_name = line[17:20].strip().upper()
        chain = line[21:22].strip()
        res_seq = int(line[22:26].strip() or "0")
        i_code = line[26:27].strip()
        x = float(line[30:38])
        y = float(line[38:46])
        z = float(line[46:54])
        occupancy = float(line[54:60].strip() or "1.00") if len(line) >= 60 else 1.0
        temp_factor = float(line[60:66].strip() or "0.00") if len(line) >= 66 else 0.0
        element = line[76:78].strip().upper() if len(line) >= 78 else ""
    except ValueError as exc:
        raise PrepError(f"Malformed PDB atom line: {line.rstrip()!r} — {exc}") from exc
    if not element:
        # Derive from atom name as a fallback for older PDB files.
        element = _element_from_name(name)
    return StructureAtom(
        record=record,
        serial=serial,
        name=name,
        alt_loc=alt_loc,
        res_name=res_name,
        chain=chain,
        res_seq=res_seq,
        i_code=i_code,
        x=x,
        y=y,
        z=z,
        occupancy=occupancy,
        temp_factor=temp_factor,
        element=element,
        line=line.rstrip("\n"),
    )


def _element_from_name(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        return ""
    if len(stripped) >= 2 and stripped[:2].upper() in {"NA", "MG", "CL", "ZN", "FE", "MN", "CU"}:
        return stripped[:2].upper()
    first = stripped[0]
    return first.upper() if first.isalpha() else ""


def atoms_of(pdb_text: str) -> list[StructureAtom]:
    """Parse ATOM/HETATM lines and return them in file order."""
    atoms: list[StructureAtom] = []
    for line in pdb_text.splitlines():
        atom = _parse_atom_line(line)
        if atom is not None:
            atoms.append(atom)
    return atoms


def split_chains(atoms: Iterable[StructureAtom]) -> dict[str, list[StructureAtom]]:
    chains: dict[str, list[StructureAtom]] = {}
    for atom in atoms:
        chains.setdefault(atom.chain, []).append(atom)
    return chains


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def select_chain(atoms: Iterable[StructureAtom], chain: str) -> list[StructureAtom]:
    chain = chain.strip()
    if not chain:
        raise PrepError("select_chain requires a non-empty chain identifier")
    kept = [atom for atom in atoms if atom.chain == chain]
    if not kept:
        available = sorted({atom.chain for atom in atoms if atom.chain})
        raise PrepError(
            f"Chain {chain!r} is not in the structure; available chains: {available or 'none'}"
        )
    return kept


def strip_waters_and_ions(
    atoms: Iterable[StructureAtom],
) -> tuple[list[StructureAtom], dict[str, int]]:
    """Return (kept, counts). ``counts`` names each stripped residue and how many atoms."""
    kept: list[StructureAtom] = []
    stripped: dict[str, int] = {}
    for atom in atoms:
        key = atom.res_name.upper()
        if key in WATER_RESIDUES or key in ION_RESIDUES:
            stripped[key] = stripped.get(key, 0) + 1
            continue
        kept.append(atom)
    return kept, stripped


def _is_protein_atom(atom: StructureAtom) -> bool:
    if atom.record != "ATOM":
        return False
    return atom.res_name.upper() in STANDARD_RESIDUES or atom.name.upper() in {
        "CA", "N", "C", "O", "CB"
    }


def extract_cognate_ligand(
    atoms: Iterable[StructureAtom],
    *,
    ligand_resname: str | None = None,
) -> tuple[list[StructureAtom], list[StructureAtom], str | None]:
    """Return (protein_atoms, ligand_atoms, chosen_resname).

    When ``ligand_resname`` is None, pick the largest ``HETATM`` residue that
    isn't a water or ion. Equal-sized candidates are resolved by first-seen
    residue, which is stable across runs for the same input.
    """
    atoms_list = list(atoms)
    protein: list[StructureAtom] = []
    het_groups: dict[tuple[str, str, int, str], list[StructureAtom]] = {}
    seen_order: list[tuple[str, str, int, str]] = []
    for atom in atoms_list:
        if atom.record == "ATOM":
            protein.append(atom)
            continue
        if atom.record == "HETATM":
            key = (atom.res_name.upper(), atom.chain, atom.res_seq, atom.i_code)
            if atom.res_name.upper() in WATER_RESIDUES or atom.res_name.upper() in ION_RESIDUES:
                continue
            if key not in het_groups:
                het_groups[key] = []
                seen_order.append(key)
            het_groups[key].append(atom)

    if not het_groups:
        return protein, [], None

    if ligand_resname is not None:
        target = ligand_resname.strip().upper()
        matches = [key for key in seen_order if key[0] == target]
        if not matches:
            available = sorted({k[0] for k in seen_order})
            raise PrepError(
                f"Ligand residue {target!r} is not in the structure; "
                f"candidates: {available}"
            )
        chosen = matches[0]
    else:
        chosen = max(seen_order, key=lambda k: (len(het_groups[k]), -seen_order.index(k)))

    ligand_atoms = het_groups[chosen]
    return protein, ligand_atoms, chosen[0]


def clean_structure(
    pdb_text: str,
    *,
    chain: str,
    source_pdb_id: str | None = None,
    ligand_resname: str | None = None,
) -> CleanedStructure:
    """Select a chain, strip waters and ions, and extract the cognate ligand."""
    atoms = atoms_of(pdb_text)
    if not atoms:
        raise PrepError("Structure contains no ATOM or HETATM records")

    chain_atoms = select_chain(atoms, chain)
    kept, stripped = strip_waters_and_ions(chain_atoms)
    protein_atoms, ligand_atoms, ligand_resname_found = extract_cognate_ligand(
        kept, ligand_resname=ligand_resname
    )
    # Filter altlocs: if an atom has an altloc other than 'A' (or blank), drop
    # it for the receptor. Many kinase structures carry alternative A and B
    # conformations; docking one of them (A, the default) is standard.
    protein_atoms = [a for a in protein_atoms if a.alt_loc in ("", "A")]
    ligand_atoms = [a for a in ligand_atoms if a.alt_loc in ("", "A")]
    return CleanedStructure(
        source_pdb_id=source_pdb_id,
        chain=chain,
        protein_atoms=tuple(protein_atoms),
        cognate_ligand_atoms=tuple(ligand_atoms),
        cognate_ligand_resname=ligand_resname_found,
        stripped_counts=stripped,
    )


# --------------------------------------------------------------------------- #
# Box definition. No default box — every call must say which method it used.
# --------------------------------------------------------------------------- #


def _bbox(
    atoms: Sequence[StructureAtom],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    xs = [a.x for a in atoms]
    ys = [a.y for a in atoms]
    zs = [a.z for a in atoms]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def define_box_from_ligand(
    ligand_atoms: Sequence[StructureAtom],
    *,
    padding_a: float = DEFAULT_BOX_PADDING_A,
) -> Box:
    if not ligand_atoms:
        raise PrepError("define_box_from_ligand requires at least one ligand atom")
    if padding_a < 0:
        raise PrepError(f"Box padding must be non-negative, got {padding_a}")
    (xmin, ymin, zmin), (xmax, ymax, zmax) = _bbox(ligand_atoms)
    center = ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, (zmin + zmax) / 2.0)
    size = (
        (xmax - xmin) + 2 * padding_a,
        (ymax - ymin) + 2 * padding_a,
        (zmax - zmin) + 2 * padding_a,
    )
    return Box(
        center=_round_triple(center),
        size=_round_triple(size),
        method=f"cognate-ligand+{padding_a:g}A",
    )


def define_box_from_residues(
    atoms: Sequence[StructureAtom],
    residues: Sequence[StandardResidue],
    *,
    padding_a: float = DEFAULT_BOX_PADDING_A,
) -> Box:
    if not residues:
        raise PrepError("define_box_from_residues requires at least one residue")
    if padding_a < 0:
        raise PrepError(f"Box padding must be non-negative, got {padding_a}")
    wanted = {(r.chain, r.res_seq, r.i_code or "") for r in residues}
    selected = [a for a in atoms if (a.chain, a.res_seq, a.i_code or "") in wanted]
    if not selected:
        raise PrepError(
            f"None of the requested residues are present in the structure: "
            f"{[(r.chain, r.res_seq) for r in residues]}"
        )
    (xmin, ymin, zmin), (xmax, ymax, zmax) = _bbox(selected)
    center = ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, (zmin + zmax) / 2.0)
    size = (
        (xmax - xmin) + 2 * padding_a,
        (ymax - ymin) + 2 * padding_a,
        (zmax - zmin) + 2 * padding_a,
    )
    return Box(
        center=_round_triple(center),
        size=_round_triple(size),
        method=f"residues:{len(residues)}+{padding_a:g}A",
    )


def define_box_explicit(
    center: tuple[float, float, float],
    size: tuple[float, float, float],
) -> Box:
    if any(s <= 0 for s in size):
        raise PrepError(f"Box size must be positive in all three dimensions, got {size}")
    return Box(center=_round_triple(center), size=_round_triple(size), method="explicit")


def _round_triple(values: tuple[float, float, float]) -> tuple[float, float, float]:
    return (round(values[0], 3), round(values[1], 3), round(values[2], 3))


# --------------------------------------------------------------------------- #
# Receptor PDBQT via meeko. Called by stages/target_add.py.
# --------------------------------------------------------------------------- #


def write_receptor_pdbqt(
    protein_pdb_text: str,
    out_path: Path,
    *,
    ph: float,
) -> Path:
    """Write a Vina-ready receptor ``.pdbqt`` using meeko.

    ``ph`` is recorded for provenance even when the underlying protonation is
    handled by meeko's defaults; a stage that lies about the pH it ran would
    make every docked score untraceable.
    """
    from meeko import (
        MoleculePreparation,
        PDBQTWriterLegacy,
        Polymer,
        ResidueChemTemplates,
    )

    try:
        chem_templates = ResidueChemTemplates.create_from_defaults()
        mk_prep = MoleculePreparation()
        polymer = Polymer.from_pdb_string(
            protein_pdb_text,
            chem_templates=chem_templates,
            mk_prep=mk_prep,
            allow_bad_res=True,
        )
        result = PDBQTWriterLegacy.write_string_from_polymer(polymer)
    except Exception as exc:  # noqa: BLE001 - meeko raises many types; surface as PrepError
        raise PrepError(
            f"meeko receptor prep failed (pH={ph}): {exc}. "
            f"Install meeko >= 0.6 and ensure the input PDB has complete residues "
            f"(fill gaps with pdbfixer or an equivalent before preparing the target)."
        ) from exc

    pdbqt_text = result[0] if isinstance(result, tuple) else result
    if not isinstance(pdbqt_text, str) or not pdbqt_text.strip():
        raise PrepError("meeko produced an empty receptor PDBQT; re-check the input PDB")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(pdbqt_text, encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# PDB text emitters
# --------------------------------------------------------------------------- #


def _atoms_to_pdb_text(atoms: Sequence[StructureAtom]) -> str:
    """Serialise atoms back to PDB. Uses each atom's original line."""
    lines: list[str] = [atom.line for atom in atoms]
    lines.append("END")
    return "\n".join(lines) + "\n"
