"""Receptor-ligand interaction detection for the 3D viewer (Step 7).

A docking score alone does not tell an enzyme engineer *why* a pose is good.
This module turns a receptor/pose pair into the contacts a structural
biologist looks at first, so the viewer can draw them and the GUI can list
them:

=================  =====================================================
kind               geometric screen
=================  =====================================================
``salt bridge``    a charged side-chain nitrogen (LYS NZ, ARG NE/NH1/NH2,
                   HIS ND1/NE2) against a ligand oxygen, or an ASP/GLU
                   carboxylate oxygen against a ligand nitrogen, <= 4.0 A
``hbond``          two polar heavy atoms (N/O/S/F) <= 3.5 A
``halogen``        a polar receptor atom against a ligand Cl/Br/I <= 3.6 A
``pi-stacking``    an aromatic side chain against a *planar* 5- or 6-ring of
                   the ligand, ring-centre distance <= 5.5 A
``hydrophobic``    receptor carbon/sulfur against a ligand carbon <= 4.0 A,
                   summarised per residue
=================  =====================================================

Everything here is **pure Python + numpy** - no Qt, no browser, no RDKit.
That keeps it unit-testable and keeps the JavaScript page dumb: it renders
what this module decided.

Honest labelling
----------------
These are *geometric screens*, not a full interaction assignment.  A pose
file carries no bond orders and at most the polar hydrogens, so a
donor-H...acceptor angle test would be guesswork (see
:data:`app.visualization.viewer.HBOND_MAX_DISTANCE`).  Aromaticity is
approximated by ring size + planarity instead of an aromaticity model.
Every hit is therefore reported as *possible*, and the GUI says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from ..chemistry.protein import AtomRecord
from ..utils.logger import get_logger

log = get_logger(__name__)

# ------------------------------------------------------------------ elements
#: Elements that can donate or accept a hydrogen bond.
POLAR_ELEMENTS = frozenset({"N", "O", "S", "F"})

#: Elements that can form a halogen bond.
HALOGEN_ELEMENTS = frozenset({"CL", "BR", "I"})

#: Elements treated as hydrophobic (carbon and sulfur are the ones that
#: actually bury surface area in a protein-ligand complex).
HYDROPHOBIC_ELEMENTS = frozenset({"C", "S"})

#: Two-letter element symbols that really turn up in docking inputs.  Used
#: only when a file has no element column: a name like ``CL1`` then has to be
#: read as chlorine.  ``CA`` is deliberately absent - in a protein that is the
#: carbon alpha, not calcium, and the element column is what tells them apart.
TWO_LETTER_ELEMENTS = frozenset({
    "CL", "BR", "NA", "MG", "ZN", "FE", "MN", "SE", "SI", "NI", "CU",
})

# ---------------------------------------------------------------- distances
#: Maximum donor-acceptor heavy-atom distance counted as a hydrogen bond.
#: 3.5 A is the upper end of the usual 2.5-3.5 A range.
HBOND_MAX_DISTANCE = 3.5

#: Charged-group contact distance for a possible salt bridge.
SALT_BRIDGE_MAX_DISTANCE = 4.0

#: Polar-atom to halogen distance.
HALOGEN_MAX_DISTANCE = 3.6

#: Ring-centre distance for a possible pi-stacking contact.
PI_STACKING_MAX_DISTANCE = 5.5

#: Carbon-carbon (or carbon-sulfur) distance counted as a hydrophobic contact.
HYDROPHOBIC_MAX_DISTANCE = 4.0

#: Anything further away is not a contact at all.
CONTACT_MAX_DISTANCE = 4.5

#: Hard cap so that a badly placed pose cannot produce hundreds of rows.
MAX_INTERACTIONS = 80

# ------------------------------------------------------------------- labels
KIND_SALT_BRIDGE = "salt bridge"
KIND_HBOND = "hbond"
KIND_HALOGEN = "halogen"
KIND_PI_STACKING = "pi-stacking"
KIND_HYDROPHOBIC = "hydrophobic"

#: Display order - the strongest / most specific interaction first.
KINDS: tuple[str, ...] = (
    KIND_SALT_BRIDGE,
    KIND_HBOND,
    KIND_HALOGEN,
    KIND_PI_STACKING,
    KIND_HYDROPHOBIC,
)

#: One-letter/short description used in tables and the report.
KIND_LABELS: dict[str, str] = {
    KIND_SALT_BRIDGE: "Salt bridge",
    KIND_HBOND: "H-bond",
    KIND_HALOGEN: "Halogen bond",
    KIND_PI_STACKING: "pi-stacking",
    KIND_HYDROPHOBIC: "Hydrophobic",
}

# --------------------------------------------------- residue knowledge tables
#: Side-chain nitrogens of residues that carry a positive charge.
BASIC_NITROGENS: dict[str, tuple[str, ...]] = {
    "LYS": ("NZ",),
    "ARG": ("NE", "NH1", "NH2"),
    "HIS": ("ND1", "NE2"),
    "HID": ("ND1", "NE2"),
    "HIE": ("ND1", "NE2"),
    "HIP": ("ND1", "NE2"),
    "HSD": ("ND1", "NE2"),
    "HSE": ("ND1", "NE2"),
    "HSP": ("ND1", "NE2"),
}

#: Side-chain carboxylate oxygens of residues that carry a negative charge.
ACIDIC_OXYGENS: dict[str, tuple[str, ...]] = {
    "ASP": ("OD1", "OD2"),
    "GLU": ("OE1", "OE2"),
}

#: Aromatic rings of the standard residues, as atom-name tuples.  Using the
#: residue name is far more reliable than re-deriving aromaticity from
#: coordinates, and a receptor file always has residue names.
AROMATIC_RINGS: dict[str, tuple[tuple[str, ...], ...]] = {
    "PHE": (("CG", "CD1", "CD2", "CE1", "CE2", "CZ"),),
    "TYR": (("CG", "CD1", "CD2", "CE1", "CE2", "CZ"),),
    "HIS": (("CG", "ND1", "CD2", "CE1", "NE2"),),
    "HID": (("CG", "ND1", "CD2", "CE1", "NE2"),),
    "HIE": (("CG", "ND1", "CD2", "CE1", "NE2"),),
    "HIP": (("CG", "ND1", "CD2", "CE1", "NE2"),),
    "TRP": (
        ("CG", "CD1", "NE1", "CE2", "CD2"),
        ("CD2", "CE2", "CE3", "CZ2", "CZ3", "CH2"),
    ),
}

# ------------------------------------------------------- ligand ring finding
#: Covalent radii (A) used to decide whether two ligand atoms are bonded.
COVALENT_RADII: dict[str, float] = {
    "H": 0.31, "C": 0.76, "N": 0.71, "O": 0.66, "S": 1.05, "P": 1.07,
    "F": 0.57, "CL": 1.02, "BR": 1.20, "I": 1.39,
}

#: A bond is assumed when the distance is below this multiple of the sum of
#: the covalent radii.  1.25 x (0.76 + 0.76) = 1.90 A for C-C, which is well
#: above a normal single bond (1.54 A) and well below any non-bonded 1,3
#: contact (~2.5 A).
BOND_SCALE = 1.25

#: Ring sizes considered aromatic.  Larger rings (macrocycles, the outline of
#: a fused system) are ignored on purpose.
RING_SIZES = (5, 6)

#: RMS out-of-plane deviation (A) allowed for a ring to count as planar.
#: A benzene ring sits at ~0.005 A; a cyclohexane chair is ~0.25 A.
RING_PLANARITY = 0.15


# --------------------------------------------------------------------------
# atoms
# --------------------------------------------------------------------------
def name_letters(atom: AtomRecord) -> str:
    """Atom name without leading digits, uppercased (``1HG1`` -> ``HG1``).

    Older PDB files number hydrogens (``1HB``, ``2HG1``); stripping the digits
    is what makes the name usable as a fallback for the element.
    """
    return atom.name.strip().lstrip("0123456789").upper()


def element_of(atom: AtomRecord) -> str:
    """Uppercase element symbol, falling back to the atom name.

    AutoDock PDBQT files and some PDB files leave the element column empty.
    Taking two characters would then read ``ND1`` as the non-element ``ND`` and
    silently lose every hydrogen bond, so the fallback is one letter unless the
    first two spell a real two-letter element (see
    :data:`TWO_LETTER_ELEMENTS`).
    """
    element = (atom.element or "").strip().upper()
    if element:
        return element
    name = name_letters(atom)
    if name[:2] in TWO_LETTER_ELEMENTS:
        return name[:2]
    return name[:1]


def is_hydrogen(atom: AtomRecord) -> bool:
    """``True`` for hydrogen atoms."""
    element = (atom.element or "").strip().upper()
    if element:
        return element == "H"
    return name_letters(atom)[:1] == "H"


def is_polar(atom: AtomRecord) -> bool:
    """``True`` for atoms that can take part in a hydrogen bond."""
    element = (atom.element or "").strip().upper()
    if element:
        return element in POLAR_ELEMENTS
    return name_letters(atom)[:1] in POLAR_ELEMENTS


def _is_hydrophobic_atom(atom: AtomRecord) -> bool:
    return element_of(atom) in HYDROPHOBIC_ELEMENTS


def _distance(a: AtomRecord, b: AtomRecord) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))


def atom_label(atom: AtomRecord) -> str:
    """``SER5:OG`` - residue, number and atom name."""
    residue = atom.residue.strip() or "LIG"
    return f"{residue}{atom.resseq}:{atom.name.strip()}"


def _residue_key(atom: AtomRecord) -> tuple[str, int, str]:
    return (atom.chain.strip(), atom.resseq, atom.residue.strip())


# --------------------------------------------------------------------------
# the record itself
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Interaction:
    """One contact between the receptor and the ligand.

    The first five fields match the hydrogen-bond record the viewer has
    always produced, so ``HydrogenBond`` is still an alias for this class.
    """

    protein_atom: str
    ligand_atom: str
    distance: float
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    kind: str = KIND_HBOND
    #: Number of atom pairs this entry stands for (hydrophobic contacts are
    #: summarised per residue, so this is usually > 1 for them).
    contacts: int = 1

    @property
    def label(self) -> str:
        """``SER5:OG - LIG1:N1 (3.00 A)``."""
        return f"{self.protein_atom} - {self.ligand_atom} ({self.distance:.2f} A)"

    @property
    def kind_label(self) -> str:
        """Short, human readable form of :attr:`kind` for tables."""
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def is_polar(self) -> bool:
        """``True`` for directional contacts worth drawing as a dashed line."""
        return self.kind in (KIND_SALT_BRIDGE, KIND_HBOND, KIND_HALOGEN)

    def to_row(self) -> tuple[str, str, str, str]:
        """``(kind, receptor, ligand, distance)`` for the GUI table."""
        return (
            self.kind_label,
            self.protein_atom,
            self.ligand_atom,
            f"{self.distance:.2f}",
        )


#: Backwards-compatible name used by the viewer and its tests.
HydrogenBond = Interaction


# --------------------------------------------------------------------------
# hydrogen bonds (unchanged behaviour, now expressed as interactions)
# --------------------------------------------------------------------------
def find_hydrogen_bonds(
    receptor: Sequence[AtomRecord],
    ligand: Sequence[AtomRecord],
    max_distance: float = HBOND_MAX_DISTANCE,
) -> list[Interaction]:
    """Possible hydrogen bonds between receptor and ligand.

    Criterion: both atoms are polar heavy atoms (N/O/S/F) and their
    heavy-atom distance is at most ``max_distance``.  Results are sorted by
    distance.
    """
    if not receptor or not ligand:
        return []
    donors = [atom for atom in receptor if is_polar(atom)]
    acceptors = [atom for atom in ligand if is_polar(atom)]
    bonds: list[Interaction] = []
    for protein_atom in donors:
        for ligand_atom in acceptors:
            distance = _distance(protein_atom, ligand_atom)
            if distance <= max_distance:
                bonds.append(Interaction(
                    protein_atom=atom_label(protein_atom),
                    ligand_atom=atom_label(ligand_atom),
                    distance=distance,
                    start=(protein_atom.x, protein_atom.y, protein_atom.z),
                    end=(ligand_atom.x, ligand_atom.y, ligand_atom.z),
                    kind=KIND_HBOND,
                ))
    bonds.sort(key=lambda bond: bond.distance)
    return bonds


# --------------------------------------------------------------------------
# rings
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Ring:
    """A planar, ring-shaped group of atoms."""

    #: Indices into the atom sequence the ring was built from.
    atoms: tuple[int, ...]
    center: tuple[float, float, float]
    #: Unit normal of the ring plane.
    normal: tuple[float, float, float]
    #: RMS out-of-plane deviation (A) - how flat the ring really is.
    flatness: float

    @property
    def size(self) -> int:
        return len(self.atoms)


def _adjacency(atoms: Sequence[AtomRecord]) -> dict[int, set[int]]:
    """Distance-based bond graph of the heavy atoms."""
    neighbours: dict[int, set[int]] = {index: set() for index in range(len(atoms))}
    for i in range(len(atoms)):
        first = atoms[i]
        radius_i = COVALENT_RADII.get(element_of(first), 0.77)
        for j in range(i + 1, len(atoms)):
            second = atoms[j]
            radius_j = COVALENT_RADII.get(element_of(second), 0.77)
            limit = BOND_SCALE * (radius_i + radius_j)
            dx = first.x - second.x
            dy = first.y - second.y
            dz = first.z - second.z
            if dx * dx + dy * dy + dz * dz <= limit * limit:
                neighbours[i].add(j)
                neighbours[j].add(i)
    return neighbours


def _cycles(neighbours: dict[int, set[int]], largest: int) -> list[tuple[int, ...]]:
    """Every simple cycle up to ``largest`` atoms, each returned once.

    Small graphs only (a ligand has tens of atoms), so the depth-limited
    search is plenty - and it avoids pulling in a graph library.
    """
    found: set[tuple[int, ...]] = set()
    for start in sorted(neighbours):
        # Paths are only allowed to grow through atoms with a higher index
        # than the start, so each cycle is discovered exactly once.
        stack: list[tuple[int, list[int]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            if len(path) > largest:
                continue
            for neighbour in neighbours[node]:
                if neighbour == start and len(path) >= 3:
                    found.add(tuple(sorted(path)))
                elif neighbour > start and neighbour not in path:
                    stack.append((neighbour, path + [neighbour]))
    return sorted(found)


def find_planar_rings(
    atoms: Sequence[AtomRecord],
    *,
    sizes: Sequence[int] = RING_SIZES,
    planarity: float = RING_PLANARITY,
) -> list[Ring]:
    """Planar 5- and 6-membered rings in a small molecule.

    Aromaticity is not available from a pose file (no bond orders), so a ring
    counts as *aromatic enough for a pi-stacking contact* when it is flat:
    benzene deviations are ~0.005 A while a cyclohexane chair is ~0.25 A.
    """
    heavy = [atom for atom in atoms if not is_hydrogen(atom)]
    if len(heavy) < 5:
        return []
    neighbours = _adjacency(heavy)
    rings: list[Ring] = []
    for cycle in _cycles(neighbours, max(sizes)):
        if len(cycle) not in sizes:
            continue
        points = np.array([[heavy[i].x, heavy[i].y, heavy[i].z] for i in cycle])
        centered = points - points.mean(axis=0)
        try:
            _left, _singular, right = np.linalg.svd(centered)
        except np.linalg.LinAlgError:                      # pragma: no cover
            continue
        normal = right[-1]
        deviation = float(np.sqrt(((centered @ normal) ** 2).mean()))
        if deviation > planarity:
            continue
        rings.append(Ring(
            atoms=tuple(i for i in cycle),
            center=tuple(float(value) for value in points.mean(axis=0)),
            normal=tuple(float(value) for value in normal),
            flatness=deviation,
        ))
    return rings


def find_aromatic_side_chain_rings(
    receptor: Sequence[AtomRecord],
) -> list[tuple[str, Ring]]:
    """Aromatic rings of PHE/TYR/TRP/HIS, labelled as ``PHE168 ring``."""
    buckets: dict[tuple[str, int, str], dict[str, AtomRecord]] = {}
    for atom in receptor:
        key = _residue_key(atom)
        if key[2] in AROMATIC_RINGS:
            buckets.setdefault(key, {})[atom.name.strip().upper()] = atom

    rings: list[tuple[str, Ring]] = []
    for key, names in buckets.items():
        resname, resseq, chain = key[2], key[1], key[0]
        label = f"{resname}{resseq}" + (f":{chain}" if chain else "")
        for ring_names in AROMATIC_RINGS[resname]:
            if not all(name in names for name in ring_names):
                continue
            points = np.array([
                [names[name].x, names[name].y, names[name].z] for name in ring_names
            ])
            centered = points - points.mean(axis=0)
            try:
                _left, _singular, right = np.linalg.svd(centered)
            except np.linalg.LinAlgError:                  # pragma: no cover
                continue
            normal = right[-1]
            rings.append((f"{label} ring", Ring(
                atoms=tuple(range(len(ring_names))),
                center=tuple(float(value) for value in points.mean(axis=0)),
                normal=tuple(float(value) for value in normal),
                flatness=float(np.sqrt(((centered @ normal) ** 2).mean())),
            )))
    return rings


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------
def _is_basic_nitrogen(atom: AtomRecord) -> bool:
    """A lysine/arginine/histidine side-chain nitrogen."""
    names = BASIC_NITROGENS.get(atom.residue.strip().upper(), ())
    return atom.name.strip().upper() in names


def _is_acidic_oxygen(atom: AtomRecord) -> bool:
    """An aspartate/glutamate carboxylate oxygen."""
    names = ACIDIC_OXYGENS.get(atom.residue.strip().upper(), ())
    return atom.name.strip().upper() in names


def _classify(
    receptor_atom: AtomRecord,
    ligand_atom: AtomRecord,
    distance: float,
    hbond_distance: float,
) -> str | None:
    """Directional interaction for one atom pair, or ``None``.

    Priority runs from the most specific to the least, so a charged pair is
    reported as a salt bridge rather than as "just" a hydrogen bond.

    Hydrophobic contacts are *not* handled here: they carry no direction, so
    they are summarised per residue in :func:`find_interactions` and would
    otherwise be listed twice.
    """
    receptor_element = element_of(receptor_atom)
    ligand_element = element_of(ligand_atom)

    if distance <= SALT_BRIDGE_MAX_DISTANCE:
        charged_pair = (
            (_is_basic_nitrogen(receptor_atom) and ligand_element == "O")
            or (_is_acidic_oxygen(receptor_atom) and ligand_element == "N")
        )
        if charged_pair:
            return KIND_SALT_BRIDGE

    if distance <= hbond_distance:
        if receptor_element in POLAR_ELEMENTS and ligand_element in POLAR_ELEMENTS:
            return KIND_HBOND

    if distance <= HALOGEN_MAX_DISTANCE:
        if receptor_element in POLAR_ELEMENTS and ligand_element in HALOGEN_ELEMENTS:
            return KIND_HALOGEN

    return None


def _hydrophobic_pairs(
    receptor: Sequence[AtomRecord],
    ligand: Sequence[AtomRecord],
    max_distance: float,
) -> dict[tuple[str, int, str], list[tuple[AtomRecord, AtomRecord, float]]]:
    """Carbon/sulfur pairs within ``max_distance``, grouped by residue."""
    grouped: dict[tuple[str, int, str], list[tuple[AtomRecord, AtomRecord, float]]] = {}
    apolar_ligand = [atom for atom in ligand if _is_hydrophobic_atom(atom)]
    if not apolar_ligand:
        return grouped

    # Same bounding-box pre-filter as the main scan: a whole protein would
    # otherwise be compared against every ligand carbon.
    low = [min(atom.x for atom in apolar_ligand) - max_distance,
           min(atom.y for atom in apolar_ligand) - max_distance,
           min(atom.z for atom in apolar_ligand) - max_distance]
    high = [max(atom.x for atom in apolar_ligand) + max_distance,
            max(atom.y for atom in apolar_ligand) + max_distance,
            max(atom.z for atom in apolar_ligand) + max_distance]
    apolar_receptor = [
        atom for atom in receptor
        if _is_hydrophobic_atom(atom)
        and low[0] <= atom.x <= high[0]
        and low[1] <= atom.y <= high[1]
        and low[2] <= atom.z <= high[2]
    ]

    for protein_atom in apolar_receptor:
        for ligand_atom in apolar_ligand:
            distance = _distance(protein_atom, ligand_atom)
            if distance <= max_distance:
                grouped.setdefault(_residue_key(protein_atom), []).append(
                    (protein_atom, ligand_atom, distance)
                )
    return grouped


# --------------------------------------------------------------------------
# the entry point
# --------------------------------------------------------------------------
def find_interactions(
    receptor: Sequence[AtomRecord],
    ligand: Sequence[AtomRecord],
    *,
    hbond_distance: float = HBOND_MAX_DISTANCE,
    contact_distance: float = CONTACT_MAX_DISTANCE,
    max_items: int = MAX_INTERACTIONS,
) -> list[Interaction]:
    """Every contact this module can find, strongest first.

    For each polar receptor atom the ligand atom giving the most specific
    interaction wins, which keeps the list to one line per protein atom and
    free of near-duplicates.  Hydrophobic contacts carry no direction, so
    they are summarised per residue instead.
    """
    if not receptor or not ligand:
        return []

    heavy_receptor = [atom for atom in receptor if not is_hydrogen(atom)]
    heavy_ligand = [atom for atom in ligand if not is_hydrogen(atom)]
    if not heavy_receptor or not heavy_ligand:
        return []

    # Cheap pre-filter: only receptor atoms inside the ligand's bounding box
    # (grown by the contact distance) can possibly be in contact.
    low = [min(atom.x for atom in heavy_ligand) - contact_distance,
           min(atom.y for atom in heavy_ligand) - contact_distance,
           min(atom.z for atom in heavy_ligand) - contact_distance]
    high = [max(atom.x for atom in heavy_ligand) + contact_distance,
            max(atom.y for atom in heavy_ligand) + contact_distance,
            max(atom.z for atom in heavy_ligand) + contact_distance]
    near = [
        atom for atom in heavy_receptor
        if is_polar(atom)
        and low[0] <= atom.x <= high[0]
        and low[1] <= atom.y <= high[1]
        and low[2] <= atom.z <= high[2]
    ]

    items: list[Interaction] = []
    for protein_atom in near:
        best: tuple[int, float, AtomRecord] | None = None
        for ligand_atom in heavy_ligand:
            distance = _distance(protein_atom, ligand_atom)
            if distance > contact_distance:
                continue
            kind = _classify(protein_atom, ligand_atom, distance, hbond_distance)
            if kind is None:
                continue
            candidate = (KINDS.index(kind), distance, ligand_atom)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            continue
        rank, distance, ligand_atom = best
        items.append(Interaction(
            protein_atom=atom_label(protein_atom),
            ligand_atom=atom_label(ligand_atom),
            distance=distance,
            start=(protein_atom.x, protein_atom.y, protein_atom.z),
            end=(ligand_atom.x, ligand_atom.y, ligand_atom.z),
            kind=KINDS[rank],
        ))

    # --- hydrophobic contacts, one line per residue -----------------------
    for _key, pairs in _hydrophobic_pairs(
        heavy_receptor, heavy_ligand, HYDROPHOBIC_MAX_DISTANCE
    ).items():
        protein_atom, ligand_atom, distance = min(pairs, key=lambda item: item[2])
        items.append(Interaction(
            protein_atom=atom_label(protein_atom),
            ligand_atom=atom_label(ligand_atom),
            distance=distance,
            start=(protein_atom.x, protein_atom.y, protein_atom.z),
            end=(ligand_atom.x, ligand_atom.y, ligand_atom.z),
            kind=KIND_HYDROPHOBIC,
            contacts=len(pairs),
        ))

    # --- pi-stacking -------------------------------------------------------
    ligand_rings = find_planar_rings(heavy_ligand)
    if ligand_rings:
        for label, ring in find_aromatic_side_chain_rings(heavy_receptor):
            for other in ligand_rings:
                distance = math.dist(ring.center, other.center)
                if distance <= PI_STACKING_MAX_DISTANCE:
                    items.append(Interaction(
                        protein_atom=label,
                        ligand_atom="ligand ring",
                        distance=distance,
                        start=ring.center,
                        end=other.center,
                        kind=KIND_PI_STACKING,
                    ))

    items.sort(key=lambda item: (KINDS.index(item.kind), item.distance))
    if len(items) > max_items:
        log.debug("truncated %d interactions to %d", len(items), max_items)
        items = items[:max_items]
    return items


def group_counts(items: Iterable[Interaction]) -> dict[str, int]:
    """``{kind: count}`` for the interactions in ``items`` (display order)."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return {kind: counts[kind] for kind in KINDS if kind in counts}


def describe(items: Sequence[Interaction]) -> str:
    """One-line summary such as ``2 H-bonds, 1 salt bridge``."""
    counts = group_counts(items)
    if not counts:
        return "no interactions"
    plural = {
        KIND_SALT_BRIDGE: "salt bridges",
        KIND_HBOND: "H-bonds",
        KIND_HALOGEN: "halogen bonds",
        KIND_PI_STACKING: "pi-stacking contacts",
        KIND_HYDROPHOBIC: "hydrophobic contacts",
    }
    parts = []
    for kind, count in counts.items():
        name = plural.get(kind, kind)
        parts.append(f"{count} {name[:-1] if count == 1 else name}")
    return ", ".join(parts)


__all__ = [
    "ACIDIC_OXYGENS",
    "AROMATIC_RINGS",
    "BASIC_NITROGENS",
    "BOND_SCALE",
    "CONTACT_MAX_DISTANCE",
    "COVALENT_RADII",
    "HALOGEN_ELEMENTS",
    "HALOGEN_MAX_DISTANCE",
    "HBOND_MAX_DISTANCE",
    "HYDROPHOBIC_ELEMENTS",
    "HYDROPHOBIC_MAX_DISTANCE",
    "HydrogenBond",
    "Interaction",
    "KINDS",
    "KIND_HALOGEN",
    "KIND_HBOND",
    "KIND_HYDROPHOBIC",
    "KIND_LABELS",
    "KIND_PI_STACKING",
    "KIND_SALT_BRIDGE",
    "MAX_INTERACTIONS",
    "PI_STACKING_MAX_DISTANCE",
    "POLAR_ELEMENTS",
    "RING_PLANARITY",
    "RING_SIZES",
    "Ring",
    "SALT_BRIDGE_MAX_DISTANCE",
    "TWO_LETTER_ELEMENTS",
    "atom_label",
    "describe",
    "element_of",
    "find_aromatic_side_chain_rings",
    "find_hydrogen_bonds",
    "find_interactions",
    "find_planar_rings",
    "group_counts",
    "is_hydrogen",
    "is_polar",
    "name_letters",
]
