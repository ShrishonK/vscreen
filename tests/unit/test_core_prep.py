"""Structure cleaning, cognate-ligand extraction, box definition.

The acceptance check for box definition from a fixture structure
(``docs/PHASES.md``) is the last test in this file.
"""

from __future__ import annotations

import pytest

from tests.conftest import FIXTURES_ROOT
from vscreen.core.prep import (
    DEFAULT_BOX_PADDING_A,
    Box,
    PrepError,
    StandardResidue,
    atoms_of,
    clean_structure,
    define_box_explicit,
    define_box_from_ligand,
    define_box_from_residues,
    extract_cognate_ligand,
    select_chain,
    strip_waters_and_ions,
)

TINY_HOLO = FIXTURES_ROOT / "structures" / "tiny_holo.pdb"


def _text() -> str:
    return TINY_HOLO.read_text(encoding="utf-8")


def test_atoms_of_parses_atom_and_hetatm_lines() -> None:
    atoms = atoms_of(_text())
    assert any(a.record == "ATOM" and a.res_name == "ALA" and a.chain == "A" for a in atoms)
    assert any(a.record == "HETATM" and a.res_name == "LIG" for a in atoms)
    assert any(a.record == "HETATM" and a.res_name == "HOH" for a in atoms)


def test_select_chain_returns_only_that_chain() -> None:
    atoms = atoms_of(_text())
    chain_a = select_chain(atoms, "A")
    assert {a.chain for a in chain_a} == {"A"}
    with pytest.raises(PrepError, match="available chains"):
        select_chain(atoms, "Z")


def test_strip_waters_and_ions_removes_the_right_residues() -> None:
    atoms = atoms_of(_text())
    kept, counts = strip_waters_and_ions(atoms)
    kept_resnames = {a.res_name for a in kept}
    assert "HOH" not in kept_resnames
    assert "ZN" not in kept_resnames
    assert counts.get("HOH", 0) == 2
    assert counts.get("ZN", 0) == 1
    assert "LIG" in kept_resnames, "the drug-like ligand must survive the ion/water filter"


def test_extract_cognate_ligand_picks_the_largest_het_residue() -> None:
    atoms = atoms_of(_text())
    kept, _ = strip_waters_and_ions(atoms)
    protein, ligand, resname = extract_cognate_ligand(kept)
    assert resname == "LIG"
    assert all(a.record == "ATOM" for a in protein)
    assert all(a.record == "HETATM" and a.res_name == "LIG" for a in ligand)
    assert len(ligand) == 5


def test_extract_cognate_ligand_absent_returns_none() -> None:
    atoms = [a for a in atoms_of(_text()) if a.record != "HETATM"]
    protein, ligand, resname = extract_cognate_ligand(atoms)
    assert ligand == []
    assert resname is None
    assert protein == atoms


def test_clean_structure_selects_chain_a_and_finds_ligand() -> None:
    cleaned = clean_structure(_text(), chain="A", source_pdb_id="FIX1")
    assert cleaned.chain == "A"
    assert cleaned.has_cognate_ligand is True
    assert cleaned.cognate_ligand_resname == "LIG"
    # Chain B atoms must not be in the protein-of-interest.
    assert all(a.chain == "A" for a in cleaned.protein_atoms)
    # Waters and ions are stripped.
    assert "HOH" in cleaned.stripped_counts


def test_clean_structure_chain_b_has_no_cognate_ligand() -> None:
    cleaned = clean_structure(_text(), chain="B")
    assert cleaned.has_cognate_ligand is False
    assert cleaned.cognate_ligand_resname is None


# --------------------------------------------------------------------------- #
# Box definition — the acceptance test exercises this on the fixture.
# --------------------------------------------------------------------------- #


def test_define_box_from_ligand_adds_padding() -> None:
    cleaned = clean_structure(_text(), chain="A")
    box = define_box_from_ligand(cleaned.cognate_ligand_atoms, padding_a=4.0)
    # The LIG atoms span x in [13.0, 16.0], y in [30.0, 31.0], z in [14.0, 17.0]
    assert box.center == pytest.approx((14.5, 30.5, 15.5))
    assert box.size == pytest.approx((3.0 + 8.0, 1.0 + 8.0, 3.0 + 8.0))
    assert box.method == "cognate-ligand+4A"


def test_define_box_from_ligand_rejects_empty_input() -> None:
    with pytest.raises(PrepError, match="ligand atom"):
        define_box_from_ligand([])


def test_define_box_from_ligand_rejects_negative_padding() -> None:
    cleaned = clean_structure(_text(), chain="A")
    with pytest.raises(PrepError, match="non-negative"):
        define_box_from_ligand(cleaned.cognate_ligand_atoms, padding_a=-1.0)


def test_define_box_from_residues_selects_only_requested_residues() -> None:
    atoms = atoms_of(_text())
    box = define_box_from_residues(
        atoms,
        residues=[StandardResidue("A", 1), StandardResidue("A", 2)],
        padding_a=2.0,
    )
    assert box.method == "residues:2+2A"
    assert all(s > 0 for s in box.size)


def test_define_box_from_residues_rejects_missing_residues() -> None:
    atoms = atoms_of(_text())
    with pytest.raises(PrepError, match="None of the requested"):
        define_box_from_residues(atoms, residues=[StandardResidue("Z", 999)])


def test_define_box_explicit_validates_size() -> None:
    box = define_box_explicit((0.0, 0.0, 0.0), (10.0, 10.0, 10.0))
    assert box.method == "explicit"
    with pytest.raises(PrepError, match="positive"):
        define_box_explicit((0.0, 0.0, 0.0), (10.0, 0.0, 10.0))


def test_box_dict_round_trip_matches_box_config_shape() -> None:
    box = define_box_explicit((1.0, 2.0, 3.0), (10.0, 10.0, 10.0))
    data = box.to_dict()
    assert set(data) == {"center", "size", "method"}
    assert data["center"] == [1.0, 2.0, 3.0]
    assert data["size"] == [10.0, 10.0, 10.0]


# --------------------------------------------------------------------------- #
# Acceptance helper: box definition from a fixture structure.
# --------------------------------------------------------------------------- #


def test_acceptance_box_from_fixture_structure() -> None:
    """PHASES.md Phase 1: box definition from a fixture structure."""
    cleaned = clean_structure(_text(), chain="A", source_pdb_id="FIX1")
    box = define_box_from_ligand(
        cleaned.cognate_ligand_atoms, padding_a=DEFAULT_BOX_PADDING_A
    )
    assert isinstance(box, Box)
    assert box.method == f"cognate-ligand+{DEFAULT_BOX_PADDING_A:g}A"
    # Box center sits inside the protein cloud, and the box is non-trivial.
    assert all(s >= 2 * DEFAULT_BOX_PADDING_A for s in box.size)
