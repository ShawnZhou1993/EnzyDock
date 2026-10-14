"""Unit tests for :mod:`app.visualization.interactions` (Step 7).

The interaction analysis is pure geometry, so it can be pinned down exactly
with hand-placed atoms.  Two things are worth testing here beyond "it finds
something":

* that the *classification* follows the documented priority (a charged pair
  is a salt bridge, not "just" a hydrogen bond);
* that the ring detection accepts a flat benzene ring and rejects a
  cyclohexane chair - the whole pi-stacking feature rests on that.
"""

from __future__ import annotations

import math

import pytest

from app.chemistry.protein import AtomRecord
from app.visualization.interactions import (
    CONTACT_MAX_DISTANCE,
    HBOND_MAX_DISTANCE,
    KINDS,
    KIND_HALOGEN,
    KIND_HBOND,
    KIND_HYDROPHOBIC,
    KIND_LABELS,
    KIND_PI_STACKING,
    KIND_SALT_BRIDGE,
    Interaction,
    Ring,
    atom_label,
    describe,
    element_of,
    find_aromatic_side_chain_rings,
    find_hydrogen_bonds,
    find_interactions,
    find_planar_rings,
    group_counts,
    is_hydrogen,
    is_polar,
)


def atom(name: str, element: str, x: float, y: float, z: float,
         residue: str = "ALA", chain: str = "A", resseq: int = 1,
         record: str = "ATOM") -> AtomRecord:
    """Build one :class:`AtomRecord` at an explicit position."""
    return AtomRecord(record=record, name=name, residue=residue, chain=chain,
                      resseq=resseq, element=element, x=x, y=y, z=z)


def hexagon(residue: str, prefix: str, radius: float, z: float,
            names: tuple[str, ...], chain: str = "A",
            resseq: int = 1) -> list[AtomRecord]:
    """A regular planar hexagon in the ``z`` plane (bond length = ``radius``)."""
    atoms = []
    for index, name in enumerate(names):
        angle = math.radians(index * 60.0)
        atoms.append(atom(name, "C", radius * math.cos(angle),
                          radius * math.sin(angle), z,
                          residue=residue, chain=chain, resseq=resseq))
    return atoms


def chair(radius: float, height: float) -> list[AtomRecord]:
    """An idealised cyclohexane chair with 1.54 A ring bonds.

    ``radius`` is the in-plane radius (2.51 A / sqrt(3) gives the real 1,3
    distance) and ``height`` half the distance between the two planes.
    """
    up = [0.0, 120.0, 240.0]
    down = [60.0, 180.0, 300.0]
    atoms: list[AtomRecord] = []
    for index, angle in enumerate(up):
        radians = math.radians(angle)
        atoms.append(atom(f"U{index}", "C", radius * math.cos(radians),
                          radius * math.sin(radians), height,
                          residue="CHX", resseq=1))
    for index, angle in enumerate(down):
        radians = math.radians(angle)
        atoms.append(atom(f"D{index}", "C", radius * math.cos(radians),
                          radius * math.sin(radians), -height,
                          residue="CHX", resseq=1))
    return atoms


# ------------------------------------------------------------------ elements
@pytest.mark.parametrize("name,element,expected", [
    ("OD1", "O", "O"),
    ("CL1", "CL", "CL"),
    ("CA", " C ", "C"),
    ("ND1", "", "N"),            # element column empty -> fall back to the name
    ("1H", "", "H"),
])
def test_element_of_falls_back_to_the_atom_name(name: str, element: str,
                                                expected: str) -> None:
    assert element_of(atom(name, element, 0.0, 0.0, 0.0)) == expected


def test_hydrogen_detection_uses_the_element_then_the_name() -> None:
    assert is_hydrogen(atom("H1", "H", 0.0, 0.0, 0.0)) is True
    assert is_hydrogen(atom("HD1", "", 0.0, 0.0, 0.0)) is True
    assert is_hydrogen(atom("1HG1", "", 0.0, 0.0, 0.0)) is True
    assert is_hydrogen(atom("CA", "C", 0.0, 0.0, 0.0)) is False


def test_a_protein_carbon_alpha_is_not_read_as_calcium() -> None:
    assert element_of(atom("CA", "", 0.0, 0.0, 0.0)) == "C"
    assert element_of(atom("CD1", "", 0.0, 0.0, 0.0)) == "C"


def test_a_hydrogen_bond_survives_a_missing_element_column() -> None:
    """Regression: ``ND1`` used to be read as the element ``ND``.

    Structure files written by other tools often leave columns 77-78 empty,
    which silently killed every hydrogen bond.
    """
    receptor = [atom("OG", "", 0.0, 0.0, 0.0, "SER", resseq=5)]
    ligand = [atom("N1", "", 3.0, 0.0, 0.0, "LIG")]
    items = find_interactions(receptor, ligand)
    assert [item.kind for item in items] == [KIND_HBOND]


def test_a_hydrophobic_contact_survives_a_missing_element_column() -> None:
    receptor = [atom("CD1", "", 0.0, 0.0, 0.0, "LEU", resseq=8)]
    ligand = [atom("C1", "", 3.0, 0.0, 0.0, "LIG")]
    items = find_interactions(receptor, ligand)
    assert [item.kind for item in items] == [KIND_HYDROPHOBIC]


def test_polar_detection_covers_the_usual_donors_and_acceptors() -> None:
    assert is_polar(atom("OG", "O", 0.0, 0.0, 0.0)) is True
    assert is_polar(atom("SD", "S", 0.0, 0.0, 0.0)) is True
    assert is_polar(atom("CL1", "CL", 0.0, 0.0, 0.0)) is False


def test_atom_label_names_the_residue_and_the_atom() -> None:
    assert atom_label(atom("OG", "O", 0.0, 0.0, 0.0, "SER", resseq=5)) == "SER5:OG"
    assert atom_label(atom("C1", "C", 0.0, 0.0, 0.0, "", resseq=1)) == "LIG1:C1"


# ------------------------------------------------------------------- record
def test_interaction_reports_a_readable_label() -> None:
    item = Interaction("SER5:OG", "LIG1:N1", 3.0, (0, 0, 0), (3, 0, 0))
    assert item.label == "SER5:OG - LIG1:N1 (3.00 A)"
    assert item.kind_label == KIND_LABELS[KIND_HBOND]


def test_only_directional_contacts_are_polar() -> None:
    polar = Interaction("A", "B", 3.0, (0, 0, 0), (3, 0, 0), KIND_HBOND)
    apolar = Interaction("A", "B", 3.9, (0, 0, 0), (3, 0, 0), KIND_HYDROPHOBIC)
    assert polar.is_polar is True
    assert apolar.is_polar is False


def test_the_table_row_starts_with_the_readable_kind() -> None:
    item = Interaction("SER5:OG", "LIG1:N1", 3.0, (0, 0, 0), (3, 0, 0),
                       KIND_SALT_BRIDGE)
    assert item.to_row() == ("Salt bridge", "SER5:OG", "LIG1:N1", "3.00")


# ---------------------------------------------------------------- distances
def test_distance_thresholds_are_ordered_sensibly() -> None:
    assert HBOND_MAX_DISTANCE < CONTACT_MAX_DISTANCE
    assert 0 < HBOND_MAX_DISTANCE <= 4.0


# ----------------------------------------------------------- hydrogen bonds
def test_hydrogen_bond_needs_two_polar_atoms_close_together() -> None:
    receptor = [atom("OG", "O", 0.0, 0.0, 0.0, "SER", resseq=5)]
    ligand = [atom("N1", "N", 3.0, 0.0, 0.0, "LIG")]
    bonds = find_hydrogen_bonds(receptor, ligand)
    assert len(bonds) == 1
    assert bonds[0].kind == KIND_HBOND
    assert bonds[0].distance == pytest.approx(3.0)


def test_a_carbon_contact_is_not_a_hydrogen_bond() -> None:
    receptor = [atom("CB", "C", 0.0, 0.0, 0.0, "ALA", resseq=5)]
    ligand = [atom("C1", "C", 3.0, 0.0, 0.0, "LIG")]
    assert find_hydrogen_bonds(receptor, ligand) == []


def test_hydrogen_bonds_are_sorted_by_distance() -> None:
    receptor = [atom("OG", "O", 0.0, 0.0, 0.0, "SER", resseq=5)]
    ligand = [
        atom("N1", "N", 3.4, 0.0, 0.0, "LIG"),
        atom("O1", "O", 2.8, 0.0, 0.0, "LIG"),
    ]
    bonds = find_hydrogen_bonds(receptor, ligand)
    assert [round(bond.distance, 2) for bond in bonds] == [2.8, 3.4]


# ------------------------------------------------------------------ classify
def test_a_charged_pair_is_reported_as_a_salt_bridge() -> None:
    """LYS NZ against a ligand oxygen: specific beats generic."""
    receptor = [atom("NZ", "N", 0.0, 0.0, 0.0, "LYS", resseq=42)]
    ligand = [atom("O1", "O", 3.2, 0.0, 0.0, "LIG")]
    items = find_interactions(receptor, ligand)
    assert [item.kind for item in items] == [KIND_SALT_BRIDGE]


def test_a_ligand_nitrogen_against_a_carboxylate_is_a_salt_bridge() -> None:
    receptor = [atom("OD1", "O", 0.0, 0.0, 0.0, "ASP", resseq=52)]
    ligand = [atom("N1", "N", 3.2, 0.0, 0.0, "LIG")]
    items = find_interactions(receptor, ligand)
    assert [item.kind for item in items] == [KIND_SALT_BRIDGE]


def test_a_backbone_oxygen_is_not_a_salt_bridge() -> None:
    """Only ASP/GLU carboxylates and LYS/ARG/HIS nitrogens are charged."""
    receptor = [atom("O", "O", 0.0, 0.0, 0.0, "ALA", resseq=10)]
    ligand = [atom("N1", "N", 3.2, 0.0, 0.0, "LIG")]
    items = find_interactions(receptor, ligand)
    assert [item.kind for item in items] == [KIND_HBOND]


def test_a_ligand_halogen_gives_a_halogen_bond() -> None:
    receptor = [atom("OG", "O", 0.0, 0.0, 0.0, "SER", resseq=5)]
    ligand = [atom("CL1", "CL", 3.4, 0.0, 0.0, "LIG")]
    items = find_interactions(receptor, ligand)
    assert [item.kind for item in items] == [KIND_HALOGEN]


def test_a_distant_polar_pair_is_not_an_interaction() -> None:
    receptor = [atom("OG", "O", 0.0, 0.0, 0.0, "SER", resseq=5)]
    ligand = [atom("N1", "N", 8.0, 0.0, 0.0, "LIG")]
    assert find_interactions(receptor, ligand) == []


def test_the_most_specific_ligand_atom_wins_per_receptor_atom() -> None:
    """One line per receptor atom - the salt bridge beats the H-bond."""
    receptor = [atom("NZ", "N", 0.0, 0.0, 0.0, "LYS", resseq=42)]
    ligand = [
        atom("O1", "O", 3.0, 0.0, 0.0, "LIG"),      # salt bridge / H-bond
        atom("O2", "O", 3.4, 0.0, 0.0, "LIG"),      # H-bond only
    ]
    items = find_interactions(receptor, ligand)
    assert len(items) == 1
    assert items[0].kind == KIND_SALT_BRIDGE


# -------------------------------------------------------------- hydrophobic
def test_apolar_contacts_are_summarised_per_residue() -> None:
    receptor = [atom("CD1", "C", 0.0, 0.0, 0.0, "LEU", resseq=8)]
    ligand = [
        atom("C1", "C", 3.0, 0.0, 0.0, "LIG"),
        atom("C2", "C", 0.0, 3.0, 0.0, "LIG"),
    ]
    items = find_interactions(receptor, ligand)
    assert len(items) == 1
    assert items[0].kind == KIND_HYDROPHOBIC
    assert items[0].contacts == 2          # both ligand carbons counted
    assert items[0].distance == pytest.approx(3.0)


def test_a_distant_apolar_contact_is_ignored() -> None:
    receptor = [atom("CD1", "C", 0.0, 0.0, 0.0, "LEU", resseq=8)]
    ligand = [atom("C1", "C", 4.6, 0.0, 0.0, "LIG")]
    assert find_interactions(receptor, ligand) == []


def test_the_bounding_box_prefilter_hides_nothing() -> None:
    """A receptor atom far outside the ligand box must not be scanned in."""
    near = atom("CD1", "C", 0.0, 0.0, 0.0, "LEU", resseq=8)
    far = atom("CD1", "C", 500.0, 0.0, 0.0, "LEU", resseq=80)
    ligand = [atom("C1", "C", 3.0, 0.0, 0.0, "LIG")]
    items = find_interactions([near, far], ligand)
    assert [item.protein_atom for item in items] == ["LEU8:CD1"]


# --------------------------------------------------------------------- rings
BENZENE = ("C1", "C2", "C3", "C4", "C5", "C6")


def test_a_flat_six_ring_is_detected() -> None:
    rings = find_planar_rings(hexagon("BNZ", "C", 1.39, 0.0, BENZENE))
    assert len(rings) == 1
    assert rings[0].size == 6
    assert rings[0].flatness < 0.01
    assert rings[0].center == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)


def test_a_cyclohexane_chair_is_not_a_pi_system() -> None:
    """1.54 A ring bonds, real chair puckering: flat enough?  No."""
    rings = find_planar_rings(chair(radius=1.449, height=0.261))
    assert rings == []


def test_a_ring_needs_at_least_five_heavy_atoms() -> None:
    assert find_planar_rings(hexagon("BNZ", "C", 1.39, 0.0, BENZENE)[:4]) == []


def test_hydrogens_are_ignored_when_looking_for_rings() -> None:
    atoms = hexagon("BNZ", "C", 1.39, 0.0, BENZENE)
    atoms.extend(
        atom(f"H{index}", "H", 2.4 * math.cos(math.radians(index * 60)),
             2.4 * math.sin(math.radians(index * 60)), 0.0, residue="BNZ")
        for index in range(6)
    )
    rings = find_planar_rings(atoms)
    assert len(rings) == 1


PHENYL = ("CG", "CD1", "CD2", "CE1", "CE2", "CZ")


def test_phenylalanine_side_chains_are_recognised() -> None:
    receptor = hexagon("PHE", "CG", 1.39, 0.0, PHENYL, resseq=168)
    rings = find_aromatic_side_chain_rings(receptor)
    assert len(rings) == 1
    label, ring = rings[0]
    assert label == "PHE168:A ring"
    assert ring.size == 6


def test_an_incomplete_side_chain_is_skipped() -> None:
    receptor = hexagon("PHE", "CG", 1.39, 0.0, PHENYL)[:4]
    assert find_aromatic_side_chain_rings(receptor) == []


def test_a_ligand_ring_over_a_phenylalanine_is_pi_stacking() -> None:
    receptor = hexagon("PHE", "CG", 1.39, 4.8, PHENYL, resseq=168)
    ligand = hexagon("BNZ", "C", 1.39, 0.0, BENZENE)
    items = find_interactions(receptor, ligand)
    kinds = {item.kind for item in items}
    assert KIND_PI_STACKING in kinds
    stacked = next(item for item in items if item.kind == KIND_PI_STACKING)
    assert stacked.distance == pytest.approx(4.8)
    assert stacked.protein_atom == "PHE168:A ring"


def test_a_distant_ring_is_not_stacked() -> None:
    receptor = hexagon("PHE", "CG", 1.39, 8.0, PHENYL, resseq=168)
    ligand = hexagon("BNZ", "C", 1.39, 0.0, BENZENE)
    items = find_interactions(receptor, ligand)
    assert KIND_PI_STACKING not in {item.kind for item in items}


# ------------------------------------------------------------------- summary
def test_interactions_are_ordered_from_specific_to_generic() -> None:
    receptor = [
        atom("NZ", "N", 0.0, 0.0, 0.0, "LYS", resseq=42),
        atom("OG", "O", 20.0, 0.0, 0.0, "SER", resseq=5),
        atom("CD1", "C", 0.0, 20.0, 0.0, "LEU", resseq=8),
    ]
    ligand = [
        atom("O1", "O", 3.0, 0.0, 0.0, "LIG"),
        atom("N1", "N", 23.0, 0.0, 0.0, "LIG"),
        atom("C1", "C", 0.0, 23.0, 0.0, "LIG"),
    ]
    items = find_interactions(receptor, ligand)
    ranks = [KINDS.index(item.kind) for item in items]
    assert ranks == sorted(ranks)
    assert {item.kind for item in items} == {
        KIND_SALT_BRIDGE, KIND_HBOND, KIND_HYDROPHOBIC,
    }


def test_a_badly_placed_pose_cannot_produce_endless_rows() -> None:
    receptor = [
        atom("CD1", "C", 0.0, 0.0, 0.0, "LEU", resseq=100 + index)
        for index in range(30)
    ]
    ligand = [atom("C1", "C", 0.0, 0.0, 3.0, "LIG")]
    assert len(find_interactions(receptor, ligand, max_items=5)) == 5


def test_group_counts_follows_the_display_order() -> None:
    items = [
        Interaction("A", "B", 4.0, (0, 0, 0), (0, 0, 0), KIND_HYDROPHOBIC),
        Interaction("A", "B", 3.0, (0, 0, 0), (0, 0, 0), KIND_HBOND),
        Interaction("A", "B", 3.1, (0, 0, 0), (0, 0, 0), KIND_HBOND),
    ]
    assert group_counts(items) == {KIND_HBOND: 2, KIND_HYDROPHOBIC: 1}


def test_describe_reads_like_a_sentence() -> None:
    assert describe([]) == "no interactions"
    one = [Interaction("A", "B", 3.0, (0, 0, 0), (0, 0, 0), KIND_HBOND)]
    assert describe(one) == "1 H-bond"
    two = one + [
        Interaction("A", "B", 3.9, (0, 0, 0), (0, 0, 0), KIND_HYDROPHOBIC)
    ]
    assert describe(two) == "1 H-bond, 1 hydrophobic contact"


def test_ring_records_its_normal_and_flatness() -> None:
    ring = Ring(atoms=(0, 1, 2, 3, 4, 5), center=(0.0, 0.0, 0.0),
                normal=(0.0, 0.0, 1.0), flatness=0.004)
    assert ring.size == 6


def test_no_inputs_means_no_interactions() -> None:
    assert find_interactions([], []) == []
    assert find_interactions([atom("CA", "C", 0.0, 0.0, 0.0)], []) == []
