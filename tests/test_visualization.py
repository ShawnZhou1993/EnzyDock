"""Unit tests for :mod:`app.visualization.viewer` (Step 7).

The 3D page itself cannot be tested headlessly, so every geometric decision
made for it - pocket definition, label anchors, hydrogen bonds, payload
serialisation - is asserted here instead.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.chemistry.protein import AtomRecord, read_atoms
from app.docking.docking_config import BoxConfig
from app.visualization.interactions import KIND_HBOND, KIND_HYDROPHOBIC
from app.visualization.viewer import (
    HBOND_MAX_DISTANCE,
    LABEL_OFFSET,
    MAX_RESIDUE_LABELS,
    POCKET_CUTOFF,
    PocketResidue,
    ViewerScene,
    build_scene,
    find_hydrogen_bonds,
    find_pocket_residues,
    find_pocket_residues_in_box,
    is_hydrogen,
    is_polar,
    pocket_atoms,
    residue_centroids,
)


def atom(name: str, element: str, x: float, y: float, z: float,
         residue: str = "ALA", chain: str = "A", resseq: int = 1,
         record: str = "ATOM") -> AtomRecord:
    """Build one :class:`AtomRecord` at an explicit position."""
    return AtomRecord(record=record, name=name, residue=residue, chain=chain,
                      resseq=resseq, element=element, x=x, y=y, z=z)


# ------------------------------------------------------------------ atoms
@pytest.mark.parametrize("element,expected", [
    ("N", True), ("O", True), ("S", True), ("F", True), ("C", False), ("", False),
])
def test_is_polar_checks_the_element(element: str, expected: bool) -> None:
    assert is_polar(atom("X", element, 0.0, 0.0, 0.0)) is expected


def test_is_polar_falls_back_to_the_atom_name() -> None:
    assert is_polar(atom("ND1", "", 0.0, 0.0, 0.0)) is True
    assert is_polar(atom("CB", "", 0.0, 0.0, 0.0)) is False


def test_is_hydrogen_checks_the_element_then_the_name() -> None:
    assert is_hydrogen(atom("H", "H", 0.0, 0.0, 0.0)) is True
    assert is_hydrogen(atom("HD1", "", 0.0, 0.0, 0.0)) is True
    assert is_hydrogen(atom("CA", "C", 0.0, 0.0, 0.0)) is False


# ----------------------------------------------------------------- pocket
def test_pocket_contains_residues_touching_the_ligand() -> None:
    receptor = [
        atom("CA", "C", 0.0, 0.0, 0.0, resseq=1),        # touching
        atom("CA", "C", 10.0, 0.0, 0.0, resseq=2),       # far away
        atom("N", "N", 1.0, 0.0, 0.0, resseq=1),
    ]
    ligand = [atom("C1", "C", 0.5, 0.0, 0.0, residue="LIG", resseq=1,
                   record="HETATM")]
    residues = find_pocket_residues(receptor, ligand, POCKET_CUTOFF)
    assert residues == [PocketResidue(chain="A", resseq=1, resname="ALA")]


def test_pocket_boundary_is_inclusive() -> None:
    receptor = [atom("CA", "C", 4.5, 0.0, 0.0, resseq=7)]
    ligand = [atom("C1", "C", 0.0, 0.0, 0.0, residue="LIG")]
    assert find_pocket_residues(receptor, ligand, 4.5)
    assert not find_pocket_residues(receptor, ligand, 4.49)


def test_pocket_ignores_hydrogens_on_both_sides() -> None:
    receptor = [atom("HG", "H", 0.2, 0.0, 0.0, resseq=1)]
    ligand = [atom("H1", "H", 0.0, 0.0, 0.0, residue="LIG")]
    assert find_pocket_residues(receptor, ligand) == []


def test_pocket_is_empty_without_a_ligand() -> None:
    assert find_pocket_residues([atom("CA", "C", 0, 0, 0)], []) == []


def test_pocket_residues_are_sorted_and_unique() -> None:
    receptor = [
        atom("CA", "C", 0.0, 0.0, 0.0, resseq=9),
        atom("CB", "C", 0.1, 0.0, 0.0, resseq=9),      # same residue again
        atom("CA", "C", 0.2, 0.0, 0.0, resseq=3),
    ]
    ligand = [atom("C1", "C", 0.0, 0.0, 0.0, residue="LIG")]
    residues = find_pocket_residues(receptor, ligand)
    assert [r.resseq for r in residues] == [3, 9]


def test_bounding_box_prefilter_does_not_change_the_result() -> None:
    """The optimisation must be transparent (same answer, fewer comparisons)."""
    receptor = [atom("CA", "C", x * 1.0, 0.0, 0.0, resseq=x + 1)
                for x in range(20)]
    ligand = [atom("C1", "C", 3.0, 0.0, 0.0, residue="LIG")]
    residues = find_pocket_residues(receptor, ligand, 4.5)
    assert [r.resseq for r in residues] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_pocket_from_the_docking_box() -> None:
    box = BoxConfig(center=(0.0, 0.0, 0.0), size=(10.0, 10.0, 10.0))
    receptor = [
        atom("CA", "C", 1.0, 0.0, 0.0, resseq=1),       # inside
        atom("CA", "C", 9.0, 0.0, 0.0, resseq=2),       # outside (+/-5)
    ]
    assert [r.resseq for r in find_pocket_residues_in_box(receptor, box)] == [1]


# ------------------------------------------------------------- hydrogen bonds
def test_hydrogen_bond_needs_two_polar_atoms_close_together() -> None:
    receptor = [atom("NE2", "N", 0.0, 0.0, 0.0, residue="HIS", resseq=10)]
    ligand = [atom("O1", "O", 2.8, 0.0, 0.0, residue="LIG")]
    bonds = find_hydrogen_bonds(receptor, ligand, HBOND_MAX_DISTANCE)
    assert len(bonds) == 1
    assert bonds[0].distance == pytest.approx(2.8)
    assert "HIS10" in bonds[0].protein_atom


def test_carbon_contacts_are_not_hydrogen_bonds() -> None:
    receptor = [atom("CD2", "C", 0.0, 0.0, 0.0)]
    ligand = [atom("C1", "C", 3.0, 0.0, 0.0, residue="LIG")]
    assert find_hydrogen_bonds(receptor, ligand) == []


def test_distant_polar_pairs_are_rejected() -> None:
    receptor = [atom("OG", "O", 0.0, 0.0, 0.0, residue="SER", resseq=5)]
    ligand = [atom("N1", "N", 3.6, 0.0, 0.0, residue="LIG")]
    assert find_hydrogen_bonds(receptor, ligand) == []


def test_hydrogen_bonds_are_sorted_by_distance() -> None:
    receptor = [
        atom("OG", "O", 0.0, 0.0, 0.0, residue="SER", resseq=5),
        atom("NE2", "N", 5.0, 0.0, 0.0, residue="HIS", resseq=9),
    ]
    ligand = [atom("N1", "N", 3.0, 0.0, 0.0, residue="LIG")]
    bonds = find_hydrogen_bonds(receptor, ligand)
    assert [round(b.distance, 2) for b in bonds] == [2.0, 3.0]


def test_hydrogen_bond_label_is_readable() -> None:
    receptor = [atom("OG", "O", 0.0, 0.0, 0.0, residue="SER", resseq=5)]
    ligand = [atom("N1", "N", 3.0, 0.0, 0.0, residue="LIG")]
    bond = find_hydrogen_bonds(receptor, ligand)[0]
    assert bond.label == "SER5:OG - LIG1:N1 (3.00 A)"


# ------------------------------------------------------------------ helpers
def test_pocket_atoms_returns_the_whole_residue() -> None:
    receptor = [
        atom("CA", "C", 0.0, 0.0, 0.0, resseq=1),
        atom("CB", "C", 2.0, 0.0, 0.0, resseq=1),       # not near the ligand
        atom("CA", "C", 0.0, 0.0, 0.0, resseq=2),
    ]
    wanted = [PocketResidue(chain="A", resseq=1, resname="ALA")]
    assert len(pocket_atoms(receptor, wanted)) == 2


def test_residue_centroids_fall_between_the_atoms() -> None:
    receptor = [
        atom("CA", "C", 0.0, 0.0, 0.0, resseq=1),
        atom("CB", "C", 2.0, 0.0, 0.0, resseq=1),
    ]
    labels = residue_centroids(receptor, [PocketResidue("A", 1, "ALA")])
    assert len(labels) == 1
    assert labels[0]["text"] == "ALA1:A"
    assert labels[0]["x"] == pytest.approx(1.0)


def test_label_includes_the_chain_when_present() -> None:
    assert PocketResidue("A", 28, "ALA").label == "ALA28:A"
    assert PocketResidue("", 28, "ALA").label == "ALA28"


# ----------------------------------------------------------- label placement
def _label_anchor(label: dict[str, object]) -> tuple[float, float, float]:
    return (label["ax"], label["ay"], label["az"])       # type: ignore[return-value]


def _centroid(label: dict[str, object]) -> tuple[float, float, float]:
    return (label["x"], label["y"], label["z"])          # type: ignore[return-value]


def test_labels_keep_the_residue_centroid_untouched() -> None:
    """``x``/``y``/``z`` still point at the residue - only the text moves."""
    receptor = [
        atom("CA", "C", 10.0, 0.0, 0.0, resseq=1),
        atom("CA", "C", -10.0, 0.0, 0.0, resseq=2),
        atom("CA", "C", 0.0, 10.0, 0.0, resseq=3),
    ]
    residues = [PocketResidue("A", 1, "ALA"), PocketResidue("A", 2, "ALA"),
                PocketResidue("A", 3, "ALA")]
    labels = residue_centroids(receptor, residues)
    assert _centroid(labels[0]) == (10.0, 0.0, 0.0)


def test_labels_are_pushed_outwards_from_the_pocket_centre() -> None:
    receptor = [
        atom("CA", "C", 10.0, 0.0, 0.0, resseq=1),
        atom("CA", "C", -10.0, 0.0, 0.0, resseq=2),
        atom("CA", "C", 0.0, 10.0, 0.0, resseq=3),
    ]
    residues = [PocketResidue("A", 1, "ALA"), PocketResidue("A", 2, "ALA"),
                PocketResidue("A", 3, "ALA")]
    labels = residue_centroids(receptor, residues)
    centre = (
        sum(_centroid(label)[0] for label in labels) / len(labels),
        sum(_centroid(label)[1] for label in labels) / len(labels),
        sum(_centroid(label)[2] for label in labels) / len(labels),
    )
    for label in labels:
        before = math.dist(_centroid(label), centre)
        after = math.dist(_label_anchor(label), centre)
        assert after == pytest.approx(before + LABEL_OFFSET, abs=1e-3)


def test_spreading_labels_actually_separates_them() -> None:
    """The whole point: the names must stop printing on top of each other.

    A real pocket is a shell of residues *around* the ligand, so the outward
    push moves them apart instead of sliding them along one line.
    """
    third = 2.0 / math.sqrt(3.0)
    positions = [(2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 2.0),
                 (-third, -third, -third)]
    receptor = [
        atom("CA", "C", x, y, z, resseq=index + 1)
        for index, (x, y, z) in enumerate(positions)
    ]
    residues = [PocketResidue("A", index + 1, "ALA") for index in range(4)]
    labels = residue_centroids(receptor, residues)
    raw = [_centroid(label) for label in labels]
    anchors = [_label_anchor(label) for label in labels]
    closest_raw = min(math.dist(a, b) for i, a in enumerate(raw) for b in raw[i + 1:])
    closest_anchor = min(
        math.dist(a, b) for i, a in enumerate(anchors) for b in anchors[i + 1:]
    )
    assert closest_anchor > 3 * closest_raw


def test_a_lone_residue_has_no_direction_to_move_in() -> None:
    receptor = [atom("CA", "C", 1.0, 0.0, 0.0, resseq=1)]
    label = residue_centroids(receptor, [PocketResidue("A", 1, "ALA")])[0]
    assert _label_anchor(label) == _centroid(label) == (1.0, 0.0, 0.0)


def test_the_offset_can_be_switched_off() -> None:
    receptor = [
        atom("CA", "C", 0.0, 0.0, 0.0, resseq=1),
        atom("CA", "C", 4.0, 0.0, 0.0, resseq=2),
    ]
    residues = [PocketResidue("A", 1, "ALA"), PocketResidue("A", 2, "ALA")]
    labels = residue_centroids(receptor, residues, offset=0.0)
    assert all(_centroid(label) == _label_anchor(label) for label in labels)


def test_a_huge_pocket_cannot_flood_the_view_with_labels() -> None:
    receptor = []
    residues = []
    for index in range(MAX_RESIDUE_LABELS + 15):
        receptor.append(atom("CA", "C", float(index) * 2, 0.0, 0.0,
                             resseq=index + 1))
        residues.append(PocketResidue("A", index + 1, "ALA"))
    assert len(residue_centroids(receptor, residues)) == MAX_RESIDUE_LABELS


def test_residue_centroids_of_nothing_is_nothing() -> None:
    assert residue_centroids([], []) == []
    assert residue_centroids([atom("CA", "C", 0.0, 0.0, 0.0)], []) == []


# ------------------------------------------------------------------- scene
def test_an_empty_scene_reports_itself_as_empty() -> None:
    scene = ViewerScene()
    assert scene.is_empty is True
    assert scene.has_protein is False


def test_payload_hides_labels_until_the_toggle_is_on() -> None:
    scene = ViewerScene(protein_text="ATOM\n", labels=[{"text": "ALA1"}])
    assert scene.payload({"residues": False})["pocket"]["labels"] == []
    assert scene.payload({"residues": True})["pocket"]["labels"]


def test_payload_marks_the_visible_parts() -> None:
    scene = ViewerScene(protein_text="ATOM\n", ligand_text="HETATM\n")
    payload = scene.payload({"protein": False, "ligand": True})
    assert payload["protein"]["visible"] is False
    assert payload["ligand"]["visible"] is True


def test_payload_defaults_to_protein_and_ligand_only() -> None:
    payload = ViewerScene(protein_text="A", ligand_text="B").payload()
    assert payload["pocket"]["visible"] is False
    assert payload["hbonds"]["visible"] is False


def test_payload_serialises_hydrogen_bonds_as_flat_segments() -> None:
    receptor = [atom("OG", "O", 0.0, 0.0, 0.0, residue="SER", resseq=5)]
    ligand = [atom("N1", "N", 3.0, 0.0, 0.0, residue="LIG")]
    scene = ViewerScene(hbonds=find_hydrogen_bonds(receptor, ligand))
    segment = scene.payload({"hbonds": True})["hbonds"]["segments"][0]
    assert len(segment) == 7                       # x1 y1 z1 x2 y2 z2 distance
    assert segment[6] == pytest.approx(3.0)


# ------------------------------------------------------------------ factory
def test_build_scene_reads_both_structures(tmp_path: Path) -> None:
    receptor = tmp_path / "receptor_clean.pdb"
    receptor.write_text(
        "ATOM      1  CA  ALA A   1     -10.000   0.000   0.000  1.00 0.00           C\n"
        "ATOM      2  OG  SER A   2       2.000   0.000   0.000  1.00 0.00           O\n"
        "ATOM      3  CA  GLY A  50      40.000   0.000   0.000  1.00 0.00           C\n"
        "END\n",
        encoding="utf-8",
    )
    ligand = tmp_path / "pose_1.pdb"
    ligand.write_text(
        "HETATM    1  N1  LIG B   1       3.000   0.000   0.000  1.00 0.00           N\n"
        "END\n",
        encoding="utf-8",
    )
    scene = build_scene(receptor, ligand)
    assert scene.has_protein and scene.has_ligand
    assert [r.resseq for r in scene.pocket_residues] == [2]
    assert scene.pocket_text.startswith("REMARK")
    assert scene.labels[0]["text"] == "SER2:A"
    assert len(scene.hbonds) == 1
    assert scene.pocket_source.endswith("around the ligand")


def test_build_scene_without_a_ligand_uses_the_box(tmp_path: Path) -> None:
    receptor = tmp_path / "receptor_clean.pdb"
    receptor.write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 0.00           C\n"
        "ATOM      2  CA  GLY A   2      30.000   0.000   0.000  1.00 0.00           C\n"
        "END\n",
        encoding="utf-8",
    )
    box = BoxConfig(center=(0.0, 0.0, 0.0), size=(10.0, 10.0, 10.0))
    scene = build_scene(receptor, None, box)
    assert [r.resseq for r in scene.pocket_residues] == [1]
    assert scene.pocket_source == "inside the docking box"
    assert scene.hbonds == []


def test_build_scene_without_a_box_or_ligand_has_no_pocket(tmp_path: Path) -> None:
    receptor = tmp_path / "r.pdb"
    receptor.write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 0.00           C\n",
        encoding="utf-8",
    )
    scene = build_scene(receptor)
    assert scene.pocket_residues == []
    assert scene.pocket_text == ""


def test_build_scene_survives_a_missing_file(tmp_path: Path) -> None:
    scene = build_scene(tmp_path / "nope.pdb", tmp_path / "nope2.pdb")
    assert scene.is_empty is True


def test_build_scene_caps_a_very_large_pocket(tmp_path: Path) -> None:
    lines = []
    for index in range(90):
        x = index * 0.5
        lines.append(
            f"ATOM  {index + 1:5d}  CA  ALA A{index + 1:4d}    "
            f"{x:8.3f}   0.000   0.000  1.00 0.00           C"
        )
    path = tmp_path / "receptor_clean.pdb"
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")
    ligand = tmp_path / "l.pdb"
    ligand.write_text(
        "HETATM    1  C1  LIG B   1       0.000   0.000   0.000  1.00 0.00           C\n",
        encoding="utf-8",
    )
    scene = build_scene(path, ligand, pocket_cutoff=100.0)
    assert len(scene.pocket_residues) <= 60


def test_build_scene_works_on_a_real_receptor(project_root: Path) -> None:
    """Sanity check on ubiquitin: a 22 A box is never an empty pocket."""
    ubq = project_root / "examples" / "protein" / "1UBQ.pdb"
    if not ubq.is_file():                              # pragma: no cover
        pytest.skip("the 1UBQ example is not available")
    atoms = read_atoms(ubq)
    assert atoms
    scene = build_scene(ubq, None, BoxConfig(center=(30.0, 29.0, 18.0),
                                             size=(22.0, 22.0, 22.0)))
    assert scene.has_protein
    assert scene.pocket_residues                       # a 22 A box is not empty
    assert all(r.resname for r in scene.pocket_residues)


# ----------------------------------------------- scene: what is in the scene
def test_the_scene_records_which_receptor_was_drawn(tmp_path: Path) -> None:
    receptor = tmp_path / "receptor_clean.pdb"
    receptor.write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 0.00           C\n",
        encoding="utf-8",
    )
    scene = build_scene(receptor)
    assert scene.protein_name == "receptor_clean.pdb"
    assert scene.payload()["protein"]["name"] == "receptor_clean.pdb"


def test_the_scene_lists_interactions_and_the_hbond_subset(tmp_path: Path) -> None:
    receptor = tmp_path / "receptor_clean.pdb"
    receptor.write_text(
        "ATOM      1  OG  SER A   2       0.000   0.000   0.000  1.00 0.00           O\n"
        "ATOM      2  CD1 LEU A   8       0.000   6.000   0.000  1.00 0.00           C\n",
        encoding="utf-8",
    )
    ligand = tmp_path / "pose_1.pdb"
    ligand.write_text(
        "HETATM    1  N1  LIG B   1       2.900   0.000   0.000  1.00 0.00           N\n"
        "HETATM    2  C1  LIG B   1       0.000   9.000   0.000  1.00 0.00           C\n",
        encoding="utf-8",
    )
    scene = build_scene(receptor, ligand)
    kinds = {item.kind for item in scene.interactions}
    assert KIND_HBOND in kinds
    assert KIND_HYDROPHOBIC in kinds
    # hbonds is the subset the "H-bonds" toggle draws.
    assert scene.hbonds == [i for i in scene.interactions if i.kind == KIND_HBOND]
    assert scene.interaction_counts[KIND_HBOND] == 1


def test_the_payload_carries_the_contacts_the_page_draws(tmp_path: Path) -> None:
    receptor = tmp_path / "receptor_clean.pdb"
    receptor.write_text(
        "ATOM      1  OG  SER A   2       0.000   0.000   0.000  1.00 0.00           O\n",
        encoding="utf-8",
    )
    ligand = tmp_path / "pose_1.pdb"
    ligand.write_text(
        "HETATM    1  N1  LIG B   1       3.000   0.000   0.000  1.00 0.00           N\n",
        encoding="utf-8",
    )
    scene = build_scene(receptor, ligand)

    hidden = scene.payload({"contacts": False})["contacts"]
    assert hidden["visible"] is False

    shown = scene.payload({"contacts": True})["contacts"]
    assert shown["visible"] is True
    item = shown["items"][0]
    assert item["kind"] == KIND_HBOND
    assert item["kind_label"] == "H-bond"
    assert item["start"] == [0.0, 0.0, 0.0]
    assert item["end"] == [3.0, 0.0, 0.0]
    assert item["dashed"] is True
    assert item["color"].startswith("#")
    assert scene.payload()["info"]["contact_summary"] == "1 H-bond"


def test_a_missing_receptor_is_reported_instead_of_being_ignored(
    tmp_path: Path,
) -> None:
    scene = build_scene(tmp_path / "gone.pdb")
    assert scene.has_protein is False
    assert any("missing" in note.lower() for note in scene.notes)
    assert scene.payload()["info"]["notes"]


def test_a_structure_we_cannot_parse_is_still_drawn(tmp_path: Path) -> None:
    """A PDBQT receptor has no ATOM records our reader understands.

    Showing nothing is the one outcome the user cannot act on, so the text is
    still handed to the page and the reason is written down.
    """
    receptor = tmp_path / "receptor.pdbqt"
    receptor.write_text(
        "REMARK  AutoDock receptor\n"
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00     0.000 C \n",
        encoding="utf-8",
    )
    scene = build_scene(receptor)
    assert scene.has_protein is True                  # it is drawn ...
    assert scene.pocket_residues == []                # ... but not analysed
    assert any("could not be analysed" in note for note in scene.notes)


def test_the_payload_reports_no_protein_rather_than_lying() -> None:
    scene = ViewerScene(ligand_text="HETATM\n")
    payload = scene.payload()
    assert payload["protein"]["text"] == ""
    assert payload["protein"]["visible"] is True      # the flag is not the data
