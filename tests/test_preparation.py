"""Tests for the ligand preparation pipeline (:mod:`app.chemistry.preparation`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from app.chemistry.conversion import ConversionError
from app.chemistry.ligand import LigandError
from app.chemistry.preparation import (
    FORCE_FIELDS,
    LigandPreparationError,
    PreparationOptions,
    add_hydrogens,
    build_3d,
    embed_conformer,
    has_3d,
    optimize_conformer,
    prepare_from_file,
    prepare_from_smiles,
    prepare_ligand,
    select_fragment,
)

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
ETHANOL = "CCO"


@pytest.fixture()
def aspirin_3d_sdf(tmp_path: Path) -> Path:
    """An SDF file that already carries 3D coordinates."""
    mol = Chem.AddHs(Chem.MolFromSmiles(ASPIRIN))
    assert AllChem.EmbedMolecule(mol, randomSeed=7) == 0
    AllChem.MMFFOptimizeMolecule(mol)
    path = tmp_path / "aspirin3d.sdf"
    writer = Chem.SDWriter(str(path))
    writer.write(mol)
    writer.close()
    return path


# ------------------------------------------------------------- end to end
def test_prepare_aspirin_writes_a_docking_input(tmp_path: Path) -> None:
    pytest.importorskip("meeko")
    prepared = prepare_from_smiles(ASPIRIN, output_dir=tmp_path / "prepared")

    assert prepared.is_ready
    assert prepared.pdbqt_path is not None and prepared.pdbqt_path.is_file()
    assert prepared.pdbqt_path.name == "ligand.pdbqt"
    assert prepared.mol_path is not None and prepared.mol_path.is_file()
    assert prepared.rotatable_bonds >= 1
    assert prepared.converter == "meeko"
    assert prepared.smiles.startswith("CC(=O)Oc1ccccc1C")


def test_prepared_ligand_exposes_display_rows(tmp_path: Path) -> None:
    pytest.importorskip("meeko")
    prepared = prepare_from_smiles(ETHANOL, output_dir=tmp_path / "prepared")
    rows = dict(prepared.to_rows())
    assert "Molecular formula" in rows
    assert "PDBQT writer" in rows
    # exactly one rotatable-bond figure is shown, and it is the one Vina will use
    assert rows["Rotatable bonds (docking)"] == str(prepared.rotatable_bonds)
    assert "Rotatable bonds" not in rows


def test_progress_callback_is_driven_to_completion(tmp_path: Path) -> None:
    pytest.importorskip("meeko")
    seen: list[tuple[int, str]] = []
    prepare_from_smiles(ETHANOL, output_dir=tmp_path / "prepared",
                        progress=lambda percent, message: seen.append((percent, message)))
    assert seen, "the pipeline must report progress"
    assert seen[-1][0] == 100
    assert [percent for percent, _ in seen] == sorted(percent for percent, _ in seen)


# ------------------------------------------------------------- geometry
def test_hydrogens_are_added() -> None:
    mol = Chem.MolFromSmiles(ETHANOL)
    assert mol.GetNumAtoms() == 3
    assert add_hydrogens(mol).GetNumAtoms() == 9


def test_build_3d_produces_a_minimised_conformer() -> None:
    built = build_3d(Chem.MolFromSmiles(ASPIRIN))
    assert has_3d(built.mol)
    assert built.force_field == "MMFF94"
    assert built.energy is not None
    assert built.reused_input_coordinates is False


def test_force_field_can_be_disabled() -> None:
    built = build_3d(Chem.MolFromSmiles(ETHANOL),
                     PreparationOptions(force_field="none"))
    assert built.force_field == "none"
    assert built.energy is None
    assert has_3d(built.mol)                       # embedding still happened


def test_uff_is_used_when_mmff_parameters_are_missing(monkeypatch) -> None:
    monkeypatch.setattr("app.chemistry.preparation.AllChem.MMFFGetMoleculeProperties",
                        lambda *args, **kwargs: None)
    built = build_3d(Chem.MolFromSmiles(ETHANOL))
    assert built.force_field == "UFF"
    assert built.energy is not None


def test_explicit_uff_selection() -> None:
    built = build_3d(Chem.MolFromSmiles(ETHANOL), PreparationOptions(force_field="UFF"))
    assert built.force_field == "UFF"


def test_embedding_failure_is_explained_to_the_user(monkeypatch) -> None:
    monkeypatch.setattr("app.chemistry.preparation.AllChem.EmbedMolecule",
                        lambda *args, **kwargs: -1)
    with pytest.raises(LigandPreparationError) as info:
        build_3d(Chem.MolFromSmiles(ETHANOL))
    assert info.value.message.startswith("Unable to generate a 3D structure")
    assert "ligand" in info.value.message


def test_optimize_conformer_reports_the_force_field() -> None:
    mol = Chem.AddHs(Chem.MolFromSmiles(ETHANOL))
    assert AllChem.EmbedMolecule(mol, randomSeed=1) == 0
    _mol, used, energy, converged = optimize_conformer(mol, PreparationOptions())
    assert used == "MMFF94"
    assert energy is not None
    assert isinstance(converged, bool)


def test_embed_conformer_accepts_a_seed() -> None:
    mol = Chem.AddHs(Chem.MolFromSmiles(ETHANOL))
    embed_conformer(mol, PreparationOptions(random_seed=1234))
    assert mol.GetNumConformers() == 1


# ------------------------------------------------------------ input files
def test_coordinates_from_a_file_are_kept(aspirin_3d_sdf: Path,
                                          tmp_path: Path) -> None:
    pytest.importorskip("meeko")
    original = Chem.SDMolSupplier(str(aspirin_3d_sdf), removeHs=False)[0]
    prepared = prepare_from_file(aspirin_3d_sdf, output_dir=tmp_path / "prepared")

    assert prepared.force_field_used == "none"     # nothing was minimised
    assert prepared.mol is not None
    before = original.GetConformer().GetPositions()
    after = prepared.mol.GetConformer().GetPositions()
    assert before.shape == after.shape
    assert (before == after).all(), "input coordinates must not be moved"


def test_a_file_can_be_re_minimised_on_request(aspirin_3d_sdf: Path,
                                               tmp_path: Path) -> None:
    pytest.importorskip("meeko")
    prepared = prepare_from_file(
        aspirin_3d_sdf, output_dir=tmp_path / "prepared",
        options=PreparationOptions(optimize_input_3d=True),
    )
    assert prepared.force_field_used == "MMFF94"
    assert prepared.energy is not None


def test_the_original_input_file_is_never_touched(aspirin_3d_sdf: Path,
                                                  tmp_path: Path) -> None:
    pytest.importorskip("meeko")
    before = aspirin_3d_sdf.read_bytes()
    prepare_from_file(aspirin_3d_sdf, output_dir=tmp_path / "prepared")
    assert aspirin_3d_sdf.read_bytes() == before


def test_prepare_ligand_does_not_mutate_the_molecule_it_is_given() -> None:
    pytest.importorskip("meeko")
    mol = Chem.MolFromSmiles(ETHANOL)
    assert mol.GetNumConformers() == 0
    prepare_ligand(mol)
    assert mol.GetNumConformers() == 0, "the caller's molecule must stay untouched"


# --------------------------------------------------------------- fragments
def test_a_salt_is_flagged_but_kept_whole() -> None:
    mol = Chem.MolFromSmiles("CC(=O)[O-].[Na+]")
    _kept, was_split = select_fragment(mol, keep_largest=False)
    assert was_split is True

    built = build_3d(mol)
    assert any("more than one molecule" in warning for warning in built.warnings)


def test_the_largest_fragment_can_be_isolated() -> None:
    mol = Chem.MolFromSmiles("CC(=O)[O-].[Na+]")
    kept, was_split = select_fragment(mol, keep_largest=True)
    assert was_split is True
    assert kept.GetNumHeavyAtoms() < mol.GetNumHeavyAtoms()


def test_single_fragment_molecules_are_untouched() -> None:
    mol = Chem.MolFromSmiles(ASPIRIN)
    kept, was_split = select_fragment(mol, keep_largest=True)
    assert was_split is False
    assert kept.GetNumAtoms() == mol.GetNumAtoms()


# ----------------------------------------------------------------- errors
def test_invalid_smiles_still_gives_a_readable_error() -> None:
    with pytest.raises(LigandError) as info:
        prepare_from_smiles("C(C(C(")
    assert "could not be interpreted" in info.value.message


def test_a_missing_pdbqt_writer_gives_install_advice(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise ConversionError("No PDBQT writer is available on this computer.",
                              "none importable")

    monkeypatch.setattr("app.chemistry.preparation.get_converter", boom)
    with pytest.raises(LigandPreparationError) as info:
        prepare_from_smiles(ETHANOL)
    assert "No PDBQT writer" in info.value.message


def test_preparing_nothing_is_reported() -> None:
    with pytest.raises(LigandPreparationError) as info:
        prepare_ligand(None)                            # type: ignore[arg-type]
    assert "no ligand" in info.value.message


def test_preparation_errors_are_ligand_errors() -> None:
    """The GUI handles every ligand problem through one dialog path."""
    assert issubclass(LigandPreparationError, LigandError)


# ---------------------------------------------------------------- options
def test_force_field_names_are_validated() -> None:
    with pytest.raises(ValueError):
        PreparationOptions(force_field="AMBER")         # type: ignore[arg-type]
    assert set(FORCE_FIELDS) == {"MMFF94", "UFF", "none"}


def test_option_ranges_are_validated() -> None:
    with pytest.raises(ValueError):
        PreparationOptions(num_conformers=0)
    with pytest.raises(ValueError):
        PreparationOptions(max_iterations=-1)


def test_several_conformers_can_be_scored(tmp_path: Path) -> None:
    pytest.importorskip("meeko")
    prepared = prepare_from_smiles(
        "CCCCCC", output_dir=tmp_path / "prepared",
        options=PreparationOptions(num_conformers=3),
    )
    assert prepared.is_ready
    assert prepared.mol is not None and prepared.mol.GetNumConformers() == 1
