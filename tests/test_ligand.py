"""Tests for :mod:`app.chemistry.ligand`."""

from __future__ import annotations

from pathlib import Path

import pytest
from rdkit import Chem

from app.chemistry.ligand import (
    LigandError,
    ligand_from_file,
    ligand_from_smiles,
    molecule_from_file,
    molecule_from_smiles,
    summarize_molecule,
)

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
CAFFEINE = "Cn1cnc2c1c(=O)n(C)c(=O)n2C"


# ------------------------------------------------------------------- SMILES
def test_aspirin_summary() -> None:
    mol, summary = ligand_from_smiles(ASPIRIN)
    assert mol.GetNumHeavyAtoms() == 13
    assert summary.formula == "C9H8O4"
    assert summary.molecular_weight == pytest.approx(180.16, abs=0.05)
    assert summary.rotatable_bonds >= 2
    assert summary.ring_count == 1
    assert summary.has_3d is False            # SMILES input is 2D


def test_caffeine_summary() -> None:
    _, summary = ligand_from_smiles(CAFFEINE)
    assert summary.formula == "C8H10N4O2"
    assert summary.molecular_weight == pytest.approx(194.19, abs=0.05)
    assert summary.rotatable_bonds == 0


def test_empty_smiles_raises_friendly_error() -> None:
    with pytest.raises(LigandError) as excinfo:
        molecule_from_smiles("   ")
    assert "SMILES" in excinfo.value.message


@pytest.mark.parametrize("raw", [
    "CC(=O)Oc1ccccc1C(=O)O\taspirin",      # .smi file / pasted with a name
    "CC(=O)Oc1ccccc1C(=O)O aspirin",       # space separated
    "  CC(=O)Oc1ccccc1C(=O)O  ",           # stray whitespace
])
def test_smiles_with_a_trailing_label_is_accepted(raw: str) -> None:
    mol = molecule_from_smiles(raw)
    assert mol.GetNumAtoms() == 13          # aspirin without hydrogens


@pytest.mark.parametrize("smiles", ["C(C", "Xx1cc", "not-a-smiles"])
def test_invalid_smiles_raises_friendly_error(smiles: str) -> None:
    with pytest.raises(LigandError) as excinfo:
        molecule_from_smiles(smiles)
    message = excinfo.value.message
    assert "could not be interpreted" in message
    assert excinfo.value.details                 # technical cause available


# --------------------------------------------------------------------- files
def test_read_molfile(aspirin_mol: Path) -> None:
    mol, summary = ligand_from_file(aspirin_mol)
    assert mol.GetNumHeavyAtoms() == 13
    assert summary.source == aspirin_mol.name
    assert summary.name == "aspirin"
    assert summary.formula == "C9H8O4"


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(LigandError) as excinfo:
        molecule_from_file(tmp_path / "ghost.sdf")
    assert "does not exist" in excinfo.value.message


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.sdf"
    path.write_text("", encoding="utf-8")
    with pytest.raises(LigandError) as excinfo:
        molecule_from_file(path)
    assert "empty" in excinfo.value.message.lower()


def test_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "ligand.xyz"
    path.write_text("C 0 0 0\n", encoding="utf-8")
    with pytest.raises(LigandError) as excinfo:
        molecule_from_file(path)
    assert "not supported" in excinfo.value.message


def test_corrupt_sdf_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "broken.sdf"
    path.write_text("this is not an sdf file\n", encoding="utf-8")
    with pytest.raises(LigandError) as excinfo:
        molecule_from_file(path)
    assert "valid molecule" in excinfo.value.message


# ------------------------------------------------------------------ summaries
def test_summary_rows_for_gui() -> None:
    _, summary = ligand_from_smiles(ASPIRIN)
    rows = dict(summary.to_rows())
    assert rows["Molecular formula"] == "C9H8O4"
    assert "g/mol" in rows["Molecular weight"]
    assert rows["3D coordinates"] == "no (2D only)"


def test_molecule_without_heavy_atoms_is_rejected() -> None:
    mol = Chem.MolFromSmiles("[H][H]")
    assert mol is not None
    with pytest.raises(LigandError) as excinfo:
        summarize_molecule(mol)
    assert "heavy atoms" in excinfo.value.message


def test_very_flexible_molecule_warns() -> None:
    mol = Chem.MolFromSmiles("C" * 30)
    assert mol is not None
    summary = summarize_molecule(mol)
    assert any("rotatable bonds" in warning for warning in summary.warnings)
