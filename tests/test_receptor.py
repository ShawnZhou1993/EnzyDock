"""Tests for receptor preparation (Step 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.chemistry.protein import AtomRecord, read_atoms
from app.chemistry.receptor import (
    MeekoReceptorConverter,
    OpenBabelProtonator,
    PreparedReceptor,
    RDKitProtonator,
    ReceptorConversionError,
    ReceptorOptions,
    ReceptorPreparationError,
    available_converters,
    available_protonators,
    filter_atoms,
    is_hydrogen,
    is_water,
    merge_hydrogens,
    prepare_receptor,
    resolve_altloc,
    strip_hydrogens,
    summarize_pdbqt,
    write_pdb,
)

# ------------------------------------------------------------------ helpers
def _backbone(resseq: int, chain: str = "A", residue: str = "ALA") -> list[AtomRecord]:
    """A complete alanine residue (N, CA, C, O, CB) - meeko can type these."""
    offsets = {
        "N": (0.000, 0.000, 0.000),
        "CA": (1.458, 0.000, 0.000),
        "C": (2.009, 1.420, 0.000),
        "O": (3.228, 1.620, 0.000),
        "CB": (1.900, -0.700, 1.200),
    }
    x0 = resseq * 5.0
    return [
        AtomRecord("ATOM", name, residue, chain, resseq, name[0],
                   x0 + dx, dy, dz)
        for name, (dx, dy, dz) in offsets.items()
    ]


def _water(resseq: int = 900) -> AtomRecord:
    return AtomRecord("HETATM", "O", "HOH", "A", resseq, "O", 50.0, 50.0, 50.0)


def _metal(resseq: int = 901) -> AtomRecord:
    return AtomRecord("HETATM", "ZN", "ZN", "A", resseq, "ZN", 60.0, 60.0, 60.0)


@pytest.fixture()
def protein_atoms() -> list[AtomRecord]:
    return [*_backbone(1), *_backbone(2), _water(), _metal()]


# ------------------------------------------------------------------ cleaning
def test_is_water_recognises_solvent_names() -> None:
    assert is_water(AtomRecord("HETATM", "O", "HOH", "A", 1, "O", 0, 0, 0))
    assert is_water(AtomRecord("HETATM", "O", "WAT", "A", 1, "O", 0, 0, 0))
    assert not is_water(AtomRecord("ATOM", "N", "ALA", "A", 1, "N", 0, 0, 0))


def test_is_hydrogen_accepts_h_and_d() -> None:
    assert is_hydrogen(AtomRecord("ATOM", "H", "ALA", "A", 1, "H", 0, 0, 0))
    assert is_hydrogen(AtomRecord("ATOM", "D", "ALA", "A", 1, "D", 0, 0, 0))
    assert not is_hydrogen(AtomRecord("ATOM", "CA", "ALA", "A", 1, "C", 0, 0, 0))


def test_filter_atoms_drops_waters_and_hetero(protein_atoms) -> None:
    kept, stats = filter_atoms(protein_atoms, ReceptorOptions())
    assert len(kept) == 10                       # two alanine residues
    assert stats["waters"] == 1
    assert stats["hetero"] == 1                  # the zinc
    assert stats["input"] == 12


def test_filter_atoms_can_keep_hetero(protein_atoms) -> None:
    kept, stats = filter_atoms(protein_atoms, ReceptorOptions(remove_hetero=False))
    assert stats["hetero"] == 0
    assert any(atom.residue == "ZN" for atom in kept)


def test_filter_atoms_can_keep_waters(protein_atoms) -> None:
    options = ReceptorOptions(remove_waters=False, remove_hetero=False)
    kept, stats = filter_atoms(protein_atoms, options)
    assert stats["waters"] == 0
    assert any(atom.residue == "HOH" for atom in kept)


def test_waters_are_also_hetero_atoms(protein_atoms) -> None:
    """Waters are HETATM records, so 'remove hetero' takes them too."""
    kept, stats = filter_atoms(protein_atoms, ReceptorOptions(remove_waters=False))
    assert stats["waters"] == 0
    assert not any(atom.residue == "HOH" for atom in kept)


def test_filter_atoms_selects_chains() -> None:
    atoms = [*_backbone(1, "A"), *_backbone(1, "B")]
    kept, stats = filter_atoms(atoms, ReceptorOptions(keep_chains=["B"]))
    assert {atom.chain for atom in kept} == {"B"}
    assert stats["chains"] == 5


def test_filter_atoms_with_an_empty_chain_list_keeps_everything() -> None:
    atoms = _backbone(1, "A")
    kept, _ = filter_atoms(atoms, ReceptorOptions(keep_chains=[]))
    assert len(kept) == 5                        # empty selection => no filter


def test_resolve_altloc_prefers_the_blank_location() -> None:
    atoms = [
        AtomRecord("ATOM", "CB", "ALA", "A", 1, "C", 1.0, 0.0, 0.0, altloc="B"),
        AtomRecord("ATOM", "CB", "ALA", "A", 1, "C", 2.0, 0.0, 0.0, altloc=""),
        AtomRecord("ATOM", "CG", "ALA", "A", 1, "C", 3.0, 0.0, 0.0, altloc="A"),
    ]
    kept, dropped = resolve_altloc(atoms)
    assert dropped == 1
    positions = {atom.name.strip(): atom.x for atom in kept}
    assert positions["CB"] == 2.0                # blank wins over "B"
    assert positions["CG"] == 3.0


def test_resolve_altloc_keeps_a_side_chain_that_only_has_a_and_b() -> None:
    atoms = [
        AtomRecord("ATOM", "CB", "ALA", "A", 1, "C", 1.0, 0.0, 0.0, altloc="B"),
        AtomRecord("ATOM", "CB", "ALA", "A", 1, "C", 2.0, 0.0, 0.0, altloc="A"),
    ]
    kept, dropped = resolve_altloc(atoms)
    assert dropped == 1
    assert kept[0].x == 2.0                      # "A" wins over "B"


def test_resolve_altloc_leaves_normal_structures_untouched() -> None:
    atoms = _backbone(1)
    kept, dropped = resolve_altloc(atoms)
    assert dropped == 0
    assert len(kept) == 5


# --------------------------------------------------------------- PDB writing
def test_write_pdb_uses_spec_columns(tmp_path: Path, protein_atoms) -> None:
    kept, _ = filter_atoms(protein_atoms, ReceptorOptions())
    path = write_pdb(kept, tmp_path / "clean.pdb")

    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if line.startswith("ATOM")]
    assert len(lines) == len(kept)
    first = lines[0]
    assert len(first) == 78
    assert first[0:6] == "ATOM  "
    assert first[12:16] == " N  "                # element right-justified
    assert first[17:20] == "ALA"
    assert first[20] == " "                      # column 21 is blank
    assert first[21] == "A"                      # column 22 is the chain
    assert first[26] == " "                      # column 27 is the iCode
    assert first[76:78].strip() == "N"           # element column


def test_write_pdb_output_round_trips_through_the_parser(
    tmp_path: Path, protein_atoms
) -> None:
    kept, _ = filter_atoms(protein_atoms, ReceptorOptions())
    path = write_pdb(kept, tmp_path / "clean.pdb")
    reread = read_atoms(path)
    assert len(reread) == len(kept)
    assert [atom.name.strip() for atom in reread] == [a.name.strip() for a in kept]


def test_write_pdb_clamps_extreme_serial_and_resseq(tmp_path: Path) -> None:
    atom = AtomRecord("ATOM", "CA", "ALA", "A", 123456, "C", 1.0, 2.0, 3.0)
    path = write_pdb([atom], tmp_path / "x.pdb")
    line = [line for line in path.read_text().splitlines()
            if line.startswith("ATOM")][0]
    assert len(line) == 78
    assert float(line[30:38]) == pytest.approx(1.0)


def test_format_atom_name_pads_two_letter_elements() -> None:
    from app.chemistry.receptor import _format_atom_name

    assert _format_atom_name("CA", "C") == " CA "
    assert _format_atom_name("H1", "H") == " H1 "
    assert _format_atom_name("ZN", "ZN") == "ZN  "


# ------------------------------------------------------------- hydrogen merge
def test_merge_hydrogens_inserts_them_into_their_residue() -> None:
    atoms = [*_backbone(1), *_backbone(2)]
    hydrogens = [
        AtomRecord("ATOM", "H1", "ALA", "A", 1, "H", 0.0, 0.0, 0.0),
        AtomRecord("ATOM", "H1", "ALA", "A", 2, "H", 5.0, 0.0, 0.0),
    ]
    merged = merge_hydrogens(atoms, hydrogens)
    assert len(merged) == 12
    # every residue stays a contiguous block
    order = [(atom.resseq, is_hydrogen(atom)) for atom in merged]
    residue1 = order[:6]
    residue2 = order[6:]
    assert {resseq for resseq, _ in residue1} == {1}
    assert {resseq for resseq, _ in residue2} == {2}
    assert residue1[-1][1] is True               # the H closes the block


def test_merge_hydrogens_without_hydrogens_returns_a_copy() -> None:
    atoms = _backbone(1)
    assert merge_hydrogens(atoms, []) == atoms


def test_merge_hydrogens_appends_unknown_residues_at_the_end() -> None:
    atoms = _backbone(1)
    orphan = AtomRecord("ATOM", "H1", "ALA", "A", 99, "H", 0.0, 0.0, 0.0)
    merged = merge_hydrogens(atoms, [orphan])
    assert merged[-1] is orphan


# ----------------------------------------------------------------- PDBQT utils
def _pdbqt_line(name: str, atom_type: str, serial: int = 1) -> str:
    """Build a PDBQT record with the AutoDock type in columns 78-79."""
    head = (f"ATOM  {serial:5d} {name:<4s} ALA A   1      "
            f"0.000   0.000   0.000  1.00  0.00    +0.000")
    return head.ljust(77) + f"{atom_type:<2s}"


def test_strip_hydrogens_removes_h_and_hd_types() -> None:
    text = "\n".join([
        _pdbqt_line("N", "N", 1),
        _pdbqt_line("H", "HD", 2),
        _pdbqt_line("HA", "H", 3),
        _pdbqt_line("CA", "C", 4),
    ])
    kept = strip_hydrogens(text).splitlines()
    assert len(kept) == 2
    assert kept[0][77:79].strip() == "N"
    assert kept[1][77:79].strip() == "C"


def test_summarize_pdbqt_counts_atom_types(tmp_path: Path) -> None:
    path = tmp_path / "r.pdbqt"
    path.write_text("\n".join([_pdbqt_line("N", "N"), _pdbqt_line("O", "OA"),
                               _pdbqt_line("C", "C")]) + "\n", encoding="utf-8")
    assert summarize_pdbqt(path) == {"atoms": 3, "atom_types": 3}


def test_summarize_pdbqt_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert summarize_pdbqt(tmp_path / "nope.pdbqt") == {}


# ------------------------------------------------------------------- adapters
def test_a_receptor_converter_is_available() -> None:
    assert available_converters(), "meeko or OpenBabel must be installed"


def test_the_preferred_converter_is_the_lowest_priority_one() -> None:
    converters = available_converters()
    assert converters == sorted(converters, key=lambda c: c.priority)


def test_meeko_is_preferred_over_openbabel() -> None:
    names = [converter.name for converter in available_converters()]
    if "meeko" in names:
        assert names[0] == "meeko"
    assert MeekoReceptorConverter.protonates is True


def test_a_protonator_is_available() -> None:
    assert available_protonators()


def test_rdkit_and_openbabel_protonators_are_registered() -> None:
    names = {protonator.name for protonator in available_protonators()}
    assert "RDKit" in names
    assert isinstance(RDKitProtonator().is_available(), bool)
    assert isinstance(OpenBabelProtonator().is_available(), bool)


def test_converter_selection_honours_an_explicit_request() -> None:
    from app.chemistry.receptor import get_converter

    first = available_converters()[0]
    assert get_converter(first.name).name == first.name


def test_converter_selection_rejects_an_unknown_name() -> None:
    from app.chemistry.receptor import get_converter

    with pytest.raises(ReceptorConversionError):
        get_converter("definitely-not-a-converter")


# ------------------------------------------------------------------- pipeline
def test_prepare_receptor_needs_a_readable_file(tmp_path: Path) -> None:
    from app.chemistry.protein import StructureParseError

    with pytest.raises(StructureParseError):
        prepare_receptor(tmp_path / "missing.pdb")


def test_prepare_receptor_rejects_an_empty_selection(tmp_path: Path) -> None:
    path = tmp_path / "mini.pdb"
    write_pdb([_water()], path)
    with pytest.raises(ReceptorPreparationError) as info:
        prepare_receptor(path)
    assert "cleaning options" in str(info.value)


def test_prepare_receptor_rejects_a_structure_without_residues(tmp_path: Path) -> None:
    """A backbone-only model cannot be typed - the error must say so."""
    from tests.factories import build_pdb_lines

    path = tmp_path / "backbone_only.pdb"
    path.write_text("\n".join(build_pdb_lines(3)) + "\n", encoding="utf-8")
    with pytest.raises(ReceptorConversionError) as info:
        prepare_receptor(path)
    assert info.value.message
    assert info.value.details
    assert "residue" in info.value.message.lower()


def test_prepare_receptor_works_without_a_workspace(tmp_path: Path) -> None:
    path = tmp_path / "mini.pdb"
    write_pdb([*_backbone(1), *_backbone(2)], path)
    prepared = prepare_receptor(path)
    assert prepared.pdbqt_path is None           # nothing was written
    assert prepared.atom_count > 0
    assert prepared.residue_count == 2


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "examples" / "protein" / "1UBQ.pdb").is_file(),
    reason="examples/protein/1UBQ.pdb is not present",
)
class TestUbiquitin:
    """End-to-end preparation of a real structure (1UBQ)."""

    @pytest.fixture()
    def ubq(self) -> Path:
        return Path(__file__).resolve().parents[1] / "examples" / "protein" / "1UBQ.pdb"

    def test_prepare_writes_a_receptor_pdbqt(self, ubq: Path, tmp_path: Path) -> None:
        prepared = prepare_receptor(ubq, output_dir=tmp_path / "prepared")
        assert prepared.is_ready
        assert prepared.converter
        assert prepared.protonation != "none"
        assert prepared.hydrogen_count > 0
        assert prepared.heavy_atom_count == prepared.atom_count - prepared.hydrogen_count
        assert prepared.residue_count == 76
        assert prepared.chains == ["A"]
        assert prepared.removed_waters == 58
        assert prepared.ignored_residues == 0

    def test_prepare_writes_both_intermediate_files(self, ubq: Path,
                                                    tmp_path: Path) -> None:
        out = tmp_path / "prepared"
        prepare_receptor(ubq, output_dir=out)
        assert (out / "receptor_clean.pdb").is_file()
        assert (out / "receptor.pdbqt").is_file()

    def test_without_hydrogens_the_pdbqt_has_none(self, ubq: Path,
                                                  tmp_path: Path) -> None:
        prepared = prepare_receptor(ubq, output_dir=tmp_path / "prepared",
                                    options=ReceptorOptions(add_hydrogens=False))
        assert prepared.hydrogen_count == 0
        assert prepared.atom_count == 602          # heavy atoms only
        assert prepared.protonation == "none"

    def test_chain_selection_keeps_only_the_requested_chain(
        self, ubq: Path, tmp_path: Path
    ) -> None:
        prepared = prepare_receptor(ubq, output_dir=tmp_path / "prepared",
                                    options=ReceptorOptions(keep_chains=["A"]))
        assert prepared.chains == ["A"]
        with pytest.raises(ReceptorPreparationError):
            prepare_receptor(ubq, output_dir=tmp_path / "p2",
                             options=ReceptorOptions(keep_chains=["Z"]))

    def test_waters_are_reported_as_removed(self, ubq: Path, tmp_path: Path) -> None:
        prepared = prepare_receptor(ubq, output_dir=tmp_path / "prepared")
        assert any("water" in warning.lower() for warning in prepared.warnings)

    def test_mmCIF_input_gives_the_same_receptor(self, ubq: Path,
                                                 tmp_path: Path) -> None:
        """The bundled mmCIF copy of 1UBQ must prepare identically."""
        cif = Path(__file__).resolve().parents[1] / "examples" / "protein" / "1UBQ.cif"
        if not cif.is_file():                      # pragma: no cover
            pytest.skip("no mmCIF sample available")
        pdb_result = prepare_receptor(ubq, output_dir=tmp_path / "a")
        cif_result = prepare_receptor(cif, output_dir=tmp_path / "b")
        assert cif_result.residue_count == pdb_result.residue_count
        assert cif_result.atom_count == pdb_result.atom_count


# ------------------------------------------------------------------ result API
def test_prepared_receptor_rows_are_displayable() -> None:
    prepared = PreparedReceptor(name="x", residue_count=10, atom_count=80,
                                hydrogen_count=20, protonation="meeko (polar)",
                                converter="meeko", chains=["A"], ignored_residues=2)
    rows = prepared.to_rows()
    labels = [label for label, _ in rows]
    assert "Docking receptor" in labels
    assert "Hydrogens" in labels
    assert "PDBQT writer" in labels
    assert "Skipped residues" in labels


def test_prepared_receptor_is_not_ready_without_a_file() -> None:
    assert PreparedReceptor().is_ready is False
    assert PreparedReceptor().center is None


def test_receptor_options_normalise_the_chain_list() -> None:
    options = ReceptorOptions(keep_chains=[" A ", "", "B"])
    assert options.keep_chains == ["A", "B"]
    assert ReceptorOptions(keep_chains=["", "  "]).keep_chains is None
