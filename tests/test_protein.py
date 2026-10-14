"""Tests for :mod:`app.chemistry.protein` (read-only structure analysis)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.chemistry.protein import (
    StructureParseError,
    read_atoms,
    summarize_structure,
)
from tests.factories import pdb_atom_line


def test_summary_of_mini_pdb(mini_pdb: Path) -> None:
    summary = summarize_structure(mini_pdb)
    assert summary.atom_count == 14            # 3 x 4 backbone + water + Zn
    assert summary.chains == ["A"]
    assert summary.residue_count == 5          # 3 ALA + HOH + ZN
    assert summary.water_count == 1
    assert summary.metals == ["ZN"]
    # water is reported separately, so it is not repeated as a HETATM residue
    assert summary.het_residues == ["ZN"]
    assert summary.has_hydrogens is False


def test_summary_of_mmcif_matches_pdb(mini_pdb: Path, mini_cif: Path) -> None:
    pdb = summarize_structure(mini_pdb)
    cif = summarize_structure(mini_cif)
    assert cif.atom_count == pdb.atom_count
    assert cif.residue_count == pdb.residue_count
    assert cif.chains == pdb.chains
    assert cif.het_residues == pdb.het_residues
    assert cif.water_count == pdb.water_count


def test_bounding_box_and_center(mini_pdb: Path) -> None:
    summary = summarize_structure(mini_pdb)
    assert summary.bounding_box is not None
    assert summary.center is not None
    assert summary.size is not None
    cx, cy, cz = summary.center
    assert 0.0 <= cx <= 12.0
    assert all(edge > 0 for edge in summary.size)


def test_multi_chain_detection(multi_chain_pdb: Path) -> None:
    summary = summarize_structure(multi_chain_pdb)
    assert summary.chains == ["A", "B"]
    assert summary.chain_count == 2


def test_rows_for_gui(mini_pdb: Path) -> None:
    rows = dict(summarize_structure(mini_pdb).to_rows())
    assert rows["File"] == mini_pdb.name
    assert rows["Chains"] == "A"
    assert rows["Atoms"] == "14"
    assert "ZN" in rows["Metals / ions"]


# --------------------------------------------------------------- error handling
def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(StructureParseError) as excinfo:
        summarize_structure(tmp_path / "nope.pdb")
    assert "does not exist" in str(excinfo.value)


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdb"
    path.write_text("", encoding="utf-8")
    with pytest.raises(StructureParseError) as excinfo:
        summarize_structure(path)
    assert "empty" in str(excinfo.value).lower()


def test_unsupported_suffix(tmp_path: Path) -> None:
    path = tmp_path / "structure.xyz"
    path.write_text("ATOM\n", encoding="utf-8")
    with pytest.raises(StructureParseError) as excinfo:
        summarize_structure(path)
    assert "not supported" in str(excinfo.value)


def test_file_without_atoms(tmp_path: Path) -> None:
    path = tmp_path / "header_only.pdb"
    path.write_text("HEADER    NOTHING HERE\nEND\n", encoding="utf-8")
    with pytest.raises(StructureParseError) as excinfo:
        summarize_structure(path)
    assert "No ATOM or HETATM" in str(excinfo.value)


def test_malformed_coordinates_are_skipped(tmp_path: Path) -> None:
    good = pdb_atom_line(2, "CA", "ALA", "A", 1, 1.458, 0.0, 0.0, "C")
    bad = good.replace("   1.458", "     abc")     # x column is not a number
    path = tmp_path / "broken.pdb"
    path.write_text(f"{bad}\n{good}\nEND\n", encoding="utf-8")

    atoms = read_atoms(path)
    assert len(atoms) == 1                     # the broken line is ignored
    assert atoms[0].name.strip() == "CA"


def test_element_is_read_from_the_element_column(mini_pdb: Path) -> None:
    elements = {atom.name.strip(): atom.element for atom in read_atoms(mini_pdb)}
    assert elements["CA"] == "C"
    assert elements["N"] == "N"
    assert elements["ZN"] == "ZN"
