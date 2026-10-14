"""Tests for the PDBQT conversion adapters (:mod:`app.chemistry.conversion`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from app.chemistry.conversion import (
    ConversionError,
    MeekoConverter,
    OpenBabelConverter,
    available_converters,
    count_branch_records,
    count_torsions,
    get_converter,
    write_pdbqt,
)

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


@pytest.fixture()
def aspirin_3d() -> Chem.Mol:
    """Aspirin with hydrogens, embedded and minimised."""
    mol = Chem.AddHs(Chem.MolFromSmiles(ASPIRIN))
    assert AllChem.EmbedMolecule(mol, randomSeed=1) == 0
    AllChem.MMFFOptimizeMolecule(mol)
    return mol


@pytest.fixture()
def aspirin_2d() -> Chem.Mol:
    """The same molecule *without* 3D coordinates."""
    return Chem.MolFromSmiles(ASPIRIN)


# ------------------------------------------------------------------- registry
def test_converter_registry_returns_instances() -> None:
    usable = available_converters()
    assert isinstance(usable, list)
    for converter in usable:
        assert isinstance(converter.name, str) and converter.name


def test_meeko_is_the_preferred_converter() -> None:
    pytest.importorskip("meeko")
    names = [converter.name for converter in available_converters()]
    assert "meeko" in names
    assert get_converter().name == "meeko"


def test_requesting_a_missing_converter_explains_itself() -> None:
    with pytest.raises(ConversionError) as info:
        get_converter("definitely-not-a-converter")
    assert "not available" in info.value.message


def test_no_converter_at_all_advises_installing_meeko(monkeypatch) -> None:
    monkeypatch.setattr("app.chemistry.conversion.available_converters", lambda: [])
    with pytest.raises(ConversionError) as info:
        get_converter()
    assert "No PDBQT writer" in info.value.message
    assert "meeko" in info.value.message


# ------------------------------------------------------------------- meeko
def test_meeko_writes_a_complete_pdbqt(aspirin_3d: Chem.Mol) -> None:
    pytest.importorskip("meeko")
    text = MeekoConverter().to_pdbqt_string(aspirin_3d)

    assert "ROOT" in text
    assert "TORSDOF" in text
    assert count_torsions(text) > 0
    assert count_branch_records(text) > 0
    # every ATOM line must carry coordinates, a charge and an AutoDock type
    atom_lines = [line for line in text.splitlines() if line.startswith("ATOM")]
    assert len(atom_lines) > 10
    for line in atom_lines[:5]:
        assert len(line) >= 78
        assert line[77:79].strip(), line


def test_write_pdbqt_creates_the_file(aspirin_3d: Chem.Mol, tmp_path: Path) -> None:
    pytest.importorskip("meeko")
    target = tmp_path / "prepared" / "ligand.pdbqt"     # parent does not exist yet
    result = write_pdbqt(aspirin_3d, target)
    assert result == target
    assert target.is_file()
    assert "TORSDOF" in target.read_text(encoding="utf-8")


def test_conversion_requires_3d_coordinates(aspirin_2d: Chem.Mol) -> None:
    pytest.importorskip("meeko")
    with pytest.raises(ConversionError) as info:
        MeekoConverter().to_pdbqt_string(aspirin_2d)
    assert "3D" in info.value.message
    assert "no 3D conformer" in info.value.details


def test_conversion_of_none_is_reported_clearly() -> None:
    pytest.importorskip("meeko")
    with pytest.raises(ConversionError) as info:
        MeekoConverter().to_pdbqt_string(None)          # type: ignore[arg-type]
    assert "no molecule" in info.value.message


# --------------------------------------------------------------- openbabel
def test_openbabel_availability_check_is_safe() -> None:
    """The probe must never raise, whatever the machine looks like."""
    assert isinstance(OpenBabelConverter().is_available(), bool)


def test_openbabel_and_meeko_agree_on_the_number_of_torsions(
    aspirin_3d: Chem.Mol,
) -> None:
    """Cross-check the fallback writer against the preferred one."""
    openbabel = OpenBabelConverter()
    if not openbabel.is_available():
        pytest.skip("OpenBabel is not installed on this machine")
    pytest.importorskip("meeko")

    theirs = count_torsions(openbabel.to_pdbqt_string(aspirin_3d))
    ours = count_torsions(MeekoConverter().to_pdbqt_string(aspirin_3d))
    assert theirs == ours > 0


def test_openbabel_without_a_backend_explains_how_to_fix_it(
    aspirin_3d: Chem.Mol, monkeypatch
) -> None:
    converter = OpenBabelConverter()
    monkeypatch.setattr(converter, "_module_route", lambda: None)
    monkeypatch.setattr(converter, "_cli_route", lambda: None)

    assert converter.is_available() is False
    with pytest.raises(ConversionError) as info:
        converter.to_pdbqt_string(aspirin_3d)
    assert "No PDBQT writer" in info.value.message


# ---------------------------------------------------------------- parsing
def test_count_torsions_reads_the_torsdof_record() -> None:
    assert count_torsions("ROOT\nENDROOT\nTORSDOF 7\n") == 7
    assert count_torsions("ROOT\nENDROOT\n") == 0
    assert count_torsions("") == 0


def test_count_branch_records() -> None:
    text = "ROOT\nBRANCH 1 2\nENDBRANCH 1 2\nBRANCH 2 3\nENDBRANCH 2 3\n"
    assert count_branch_records(text) == 2
