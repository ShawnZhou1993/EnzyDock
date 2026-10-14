"""Scene building for the embedded 3Dmol.js viewer (Step 7).

The 3D page itself is dumb on purpose: it renders whatever this module hands
it.  Every geometric decision - which residues form the binding pocket, where
the hydrogen bonds are - is made here in Python, where it can be unit tested
without a browser.

    receptor.pdb + pose_3.pdb + docking box
        -> pocket residues        (within POCKET_CUTOFF of the ligand)
        -> pocket atom subset     (PDB text for the transparent surface)
        -> residue labels         (centroid of each pocket residue)
        -> interactions           (H-bonds, salt bridges, pi-stacking ...)
        -> :meth:`ViewerScene.payload`  (JSON for ``enzydockViewer.setScene``)

The interaction chemistry itself lives in
:mod:`app.visualization.interactions`; this module only assembles the scene.

Keeping the geometry in Python also keeps the JavaScript small: the page only
needs ``addModel``, ``setStyle``, ``addSurface``, ``addCylinder`` and
``addLabel``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from ..chemistry.protein import AtomRecord, read_atoms
from ..chemistry.receptor import atoms_to_pdb_text
from ..docking.docking_config import BoxConfig
from ..utils.logger import get_logger
from .interactions import (
    CONTACT_MAX_DISTANCE,
    HBOND_MAX_DISTANCE,
    KIND_HALOGEN,
    KIND_HBOND,
    KIND_HYDROPHOBIC,
    KIND_LABELS,
    KIND_PI_STACKING,
    KIND_SALT_BRIDGE,
    KINDS,
    POLAR_ELEMENTS,
    HydrogenBond,
    Interaction,
    atom_label,
    describe,
    find_aromatic_side_chain_rings,      # noqa: F401  (re-exported for callers)
    find_hydrogen_bonds,
    find_interactions,
    find_planar_rings,                   # noqa: F401  (re-exported for callers)
    group_counts,
    is_hydrogen,
    is_polar,
)

log = get_logger(__name__)

#: A residue belongs to the pocket when any of its atoms is this close to
#: any ligand atom.
POCKET_CUTOFF = 4.5

#: Hard cap so that a huge box cannot produce a 500-residue "pocket".
MAX_POCKET_RESIDUES = 60

#: Pocket residue labels are pushed this far outwards from the centre of the
#: pocket.  The residues lining a site sit within a few angstroms of each
#: other, so labels drawn at the raw centroids print on top of one another and
#: become unreadable; the offset spreads them around the pocket.  A grey leader
#: line connects every label back to the residue it names.
LABEL_OFFSET = 5.0

#: Hard cap on residue labels - a 60-residue pocket would otherwise bury the
#: structure under its own annotation.
MAX_RESIDUE_LABELS = 24


@dataclass(frozen=True)
class PocketResidue:
    """One residue that lines the binding site."""

    chain: str
    resseq: int
    resname: str

    @property
    def label(self) -> str:
        """``ALA28`` or ``ALA28:A`` when the structure has several chains."""
        base = f"{self.resname}{self.resseq}"
        return f"{base}:{self.chain}" if self.chain.strip() else base


@dataclass(frozen=True)
class HydrogenBond:
    """A possible hydrogen bond between receptor and ligand."""

    protein_atom: str
    ligand_atom: str
    distance: float
    start: tuple[float, float, float]
    end: tuple[float, float, float]

    @property
    def label(self) -> str:
        return f"{self.protein_atom} - {self.ligand_atom} ({self.distance:.2f} A)"


@dataclass
class ViewerScene:
    """Everything the 3D page needs to draw the current structure."""

    protein_text: str = ""
    ligand_text: str = ""
    pocket_text: str = ""
    pocket_residues: list[PocketResidue] = field(default_factory=list)
    labels: list[dict[str, object]] = field(default_factory=list)
    #: Possible hydrogen bonds (kept as its own field for the toolbar toggle).
    hbonds: list[Interaction] = field(default_factory=list)
    #: Every contact - H-bonds, salt bridges, halogen bonds, pi-stacking and
    #: hydrophobic contacts - in display order.
    interactions: list[Interaction] = field(default_factory=list)
    pocket_source: str = ""          # how the pocket was defined, for the UI
    #: ``(label, path)`` of the receptor actually drawn, for the status line.
    protein_name: str = ""
    #: Human readable problems to surface in the UI (missing receptor ...).
    notes: list[str] = field(default_factory=list)

    # -- queries -----------------------------------------------------------
    @property
    def has_protein(self) -> bool:
        return bool(self.protein_text.strip())

    @property
    def has_ligand(self) -> bool:
        return bool(self.ligand_text.strip())

    @property
    def is_empty(self) -> bool:
        return not (self.has_protein or self.has_ligand)

    @property
    def pocket_labels(self) -> list[str]:
        return [residue.label for residue in self.pocket_residues]

    @property
    def interaction_counts(self) -> dict[str, int]:
        """``{kind: count}`` for the interactions in this scene."""
        return group_counts(self.interactions)

    # -- serialisation -----------------------------------------------------
    def payload(self, display: dict[str, bool] | None = None) -> dict[str, object]:
        """Build the JSON-serialisable payload sent to the web page.

        ``display`` maps the toolbar toggle names (``protein``, ``ligand``,
        ``pocket``, ``surface``, ``residues``, ``hbonds``, ``contacts``) to
        their enabled state.
        """
        flags = {"protein": True, "ligand": True, "pocket": False,
                 "surface": False, "residues": False, "hbonds": False,
                 "contacts": False}
        if display:
            flags.update({key: bool(value) for key, value in display.items()})
        return {
            "protein": {
                "text": self.protein_text,
                "visible": flags["protein"],
                "name": self.protein_name,
                "surface": flags["surface"],
            },
            "ligand": {"text": self.ligand_text, "visible": flags["ligand"]},
            "pocket": {
                "text": self.pocket_text,
                "visible": flags["pocket"] and bool(self.pocket_text),
                "labels": self.labels if flags["residues"] else [],
            },
            "hbonds": {
                "segments": [
                    [*bond.start, *bond.end, round(bond.distance, 2)]
                    for bond in self.hbonds
                ],
                "visible": flags["hbonds"] and bool(self.hbonds),
            },
            "contacts": {
                "items": [_contact_item(item) for item in self.interactions],
                "visible": flags["contacts"] and bool(self.interactions),
            },
            "info": {
                "pocket_source": self.pocket_source,
                "pocket_residues": [r.label for r in self.pocket_residues],
                "hbond_count": len(self.hbonds),
                "protein_name": self.protein_name,
                "contact_counts": self.interaction_counts,
                "contact_summary": describe(self.interactions),
                "notes": list(self.notes),
            },
        }


#: Colours and line widths per interaction kind (shared with the 3D page).
#: ``show_label`` marks the directional contacts: their distance is written
#: in the scene, while the (numerous, directionless) hydrophobic contacts are
#: only drawn.
INTERACTION_STYLE: dict[str, dict[str, object]] = {
    KIND_SALT_BRIDGE: {"color": "#ff7ab6", "radius": 0.10, "dashed": True,
                       "show_label": True},
    KIND_HBOND: {"color": "#ffd479", "radius": 0.09, "dashed": True,
                 "show_label": True},
    KIND_HALOGEN: {"color": "#7ee0ff", "radius": 0.09, "dashed": True,
                   "show_label": True},
    KIND_PI_STACKING: {"color": "#b18cff", "radius": 0.07, "dashed": False,
                       "show_label": True},
    KIND_HYDROPHOBIC: {"color": "#8f9aa6", "radius": 0.05, "dashed": False,
                       "show_label": False},
}


def _contact_item(item: Interaction) -> dict[str, object]:
    """One interaction in the shape the 3D page draws."""
    style = INTERACTION_STYLE.get(item.kind, INTERACTION_STYLE[KIND_HBOND])
    return {
        "kind": item.kind,
        "kind_label": item.kind_label,
        "label": item.label,
        "start": [round(value, 3) for value in item.start],
        "end": [round(value, 3) for value in item.end],
        "distance": round(item.distance, 2),
        "contacts": item.contacts,
        **style,
    }


# ------------------------------------------------------------------ geometry
def _distance(a: AtomRecord, b: AtomRecord) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))


def _residue_key(atom: AtomRecord) -> tuple[str, int, str]:
    """Identity of the residue an atom belongs to."""
    return (atom.chain.strip(), atom.resseq, atom.residue.strip())


def find_pocket_residues(
    receptor: Sequence[AtomRecord],
    ligand: Sequence[AtomRecord],
    cutoff: float = POCKET_CUTOFF,
) -> list[PocketResidue]:
    """Residues with at least one atom within ``cutoff`` of any ligand atom.

    This is the definition a structural biologist expects: the pocket follows
    the docked pose instead of being a fixed cube.
    """
    if not receptor or not ligand:
        return []
    cutoff_sq = cutoff * cutoff
    heavy_ligand = [atom for atom in ligand if not is_hydrogen(atom)]
    if not heavy_ligand:
        return []

    # Cheap pre-filter: only receptor atoms inside the ligand's bounding box
    # (grown by the cutoff) can possibly be in contact.  This keeps large
    # complexes responsive without needing a spatial index.
    xs = [atom.x for atom in heavy_ligand]
    ys = [atom.y for atom in heavy_ligand]
    zs = [atom.z for atom in heavy_ligand]
    low = (min(xs) - cutoff, min(ys) - cutoff, min(zs) - cutoff)
    high = (max(xs) + cutoff, max(ys) + cutoff, max(zs) + cutoff)

    near = [
        atom for atom in receptor
        if not is_hydrogen(atom)
        and low[0] <= atom.x <= high[0]
        and low[1] <= atom.y <= high[1]
        and low[2] <= atom.z <= high[2]
        and any(
            (atom.x - other.x) ** 2 + (atom.y - other.y) ** 2
            + (atom.z - other.z) ** 2 <= cutoff_sq
            for other in heavy_ligand
        )
    ]
    return _unique_residues(near)


def find_pocket_residues_in_box(
    receptor: Sequence[AtomRecord],
    box: BoxConfig,
) -> list[PocketResidue]:
    """Residues with at least one atom inside ``box``.

    Used before a pose exists (directly after receptor preparation), where
    there is no ligand to measure against.
    """
    if not receptor:
        return []
    half = tuple(size / 2.0 for size in box.size)
    inside = [
        atom for atom in receptor
        if abs(atom.x - box.center[0]) <= half[0]
        and abs(atom.y - box.center[1]) <= half[1]
        and abs(atom.z - box.center[2]) <= half[2]
    ]
    return _unique_residues(inside)


def _unique_residues(atoms: Iterable[AtomRecord]) -> list[PocketResidue]:
    seen: dict[tuple[str, int, str], PocketResidue] = {}
    for atom in atoms:
        key = _residue_key(atom)
        if key not in seen:
            seen[key] = PocketResidue(chain=key[0], resseq=key[1], resname=key[2])
    return sorted(seen.values(), key=lambda residue: (residue.chain, residue.resseq))


def pocket_atoms(
    receptor: Sequence[AtomRecord],
    residues: Sequence[PocketResidue],
) -> list[AtomRecord]:
    """Atoms of ``receptor`` that belong to one of ``residues``."""
    wanted = {(r.chain, r.resseq, r.resname) for r in residues}
    return [atom for atom in receptor if _residue_key(atom) in wanted]


def residue_centroids(
    receptor: Sequence[AtomRecord],
    residues: Sequence[PocketResidue],
    *,
    offset: float = LABEL_OFFSET,
    limit: int = MAX_RESIDUE_LABELS,
) -> list[dict[str, object]]:
    """One label per pocket residue, spread out around the binding site.

    ``x``/``y``/``z`` is the geometric centre of the residue - where the label
    *points*.  ``ax``/``ay``/``az`` is where the text is actually drawn: the
    same point pushed ``offset`` angstroms away from the centre of the whole
    pocket.  Without that the neighbouring names overlap into an unreadable
    smear; the page draws a leader line between the two so the association
    stays obvious.
    """
    wanted = {(r.chain, r.resseq, r.resname): r for r in residues}
    sums: dict[tuple[str, int, str], list[float]] = {}
    for atom in receptor:
        key = _residue_key(atom)
        if key not in wanted:
            continue
        bucket = sums.setdefault(key, [0.0, 0.0, 0.0, 0.0])
        bucket[0] += atom.x
        bucket[1] += atom.y
        bucket[2] += atom.z
        bucket[3] += 1.0

    centres: list[tuple[tuple[str, int, str], tuple[float, float, float]]] = []
    for key, (sx, sy, sz, count) in sums.items():
        if not count:                                  # pragma: no cover
            continue
        centres.append((key, (sx / count, sy / count, sz / count)))
    if not centres:
        return []
    if limit > 0 and len(centres) > limit:
        log.debug("residue labels truncated from %d to %d",
                  len(centres), limit)
        centres = centres[:limit]

    pocket_centre = tuple(
        sum(centre[axis] for _key, centre in centres) / len(centres)
        for axis in range(3)
    )

    labels: list[dict[str, object]] = []
    for key, centre in centres:
        anchor = _push_outwards(centre, pocket_centre, offset)
        labels.append({
            "text": wanted[key].label,
            "x": round(centre[0], 3),
            "y": round(centre[1], 3),
            "z": round(centre[2], 3),
            "ax": round(anchor[0], 3),
            "ay": round(anchor[1], 3),
            "az": round(anchor[2], 3),
        })
    return labels


def _push_outwards(
    point: tuple[float, float, float],
    centre: tuple[float, float, float],
    distance: float,
) -> tuple[float, float, float]:
    """Move ``point`` away from ``centre`` by ``distance`` (unchanged if equal).

    A residue sitting exactly on the pocket centre has no outward direction -
    moving it would be arbitrary, so it stays where it is.
    """
    vector = tuple(point[axis] - centre[axis] for axis in range(3))
    length = math.dist(point, centre)
    if distance <= 0 or length < 1e-6:
        return point
    return tuple(
        point[axis] + vector[axis] / length * distance for axis in range(3)
    )


# ------------------------------------------------------------------- factory
def _read_structure_text(path: Path, scene: ViewerScene, what: str) -> str:
    """Read a structure file as text, recording why it failed.

    This is deliberately independent of :func:`read_atoms`: a file our own
    parser refuses (for example a ``.pdbqt`` receptor) can still be drawn by
    3Dmol.js, and *showing nothing* is the one outcome a user cannot act on.
    """
    if not path.is_file():
        scene.notes.append(f"The {what} file '{path.name}' is missing.")
        log.warning("%s file missing: %s", what, path)
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:                             # pragma: no cover
        scene.notes.append(f"The {what} file '{path.name}' could not be read.")
        log.warning("could not read %s %s: %s", what, path, exc)
        return ""


def _read_atoms_quietly(
    path: Path,
    scene: ViewerScene,
    what: str,
) -> list[AtomRecord]:
    """Parse a structure, degrading to "drawn but not analysed"."""
    try:
        return read_atoms(path)
    except Exception as exc:
        log.warning("could not parse %s %s: %s", what, path, exc)
        scene.notes.append(
            f"The {what} structure could not be analysed ({exc}) - it is "
            "still displayed, but the pocket and the interactions are not "
            "available."
        )
        return []


def build_scene(
    receptor_path: Path | str | None = None,
    ligand_path: Path | str | None = None,
    box: BoxConfig | None = None,
    *,
    pocket_cutoff: float = POCKET_CUTOFF,
    hbond_distance: float = HBOND_MAX_DISTANCE,
    contact_distance: float = CONTACT_MAX_DISTANCE,
) -> ViewerScene:
    """Read both structures and derive pocket, labels and interactions.

    Missing files are tolerated: the scene simply contains fewer pieces, so
    the viewer can show a receptor before docking has been run.  Whatever
    could not be loaded is listed in :attr:`ViewerScene.notes` instead of
    being dropped silently.
    """
    scene = ViewerScene()
    receptor_atoms: list[AtomRecord] = []
    ligand_atoms: list[AtomRecord] = []

    if receptor_path is not None:
        path = Path(receptor_path)
        scene.protein_text = _read_structure_text(path, scene, "receptor")
        if scene.protein_text:
            scene.protein_name = path.name
            receptor_atoms = _read_atoms_quietly(path, scene, "receptor")

    if ligand_path is not None:
        path = Path(ligand_path)
        scene.ligand_text = _read_structure_text(path, scene, "ligand")
        if scene.ligand_text:
            ligand_atoms = _read_atoms_quietly(path, scene, "ligand")

    if not receptor_atoms:
        return scene

    if ligand_atoms:
        residues = find_pocket_residues(receptor_atoms, ligand_atoms, pocket_cutoff)
        scene.pocket_source = f"{pocket_cutoff:g} A around the ligand"
    elif box is not None:
        residues = find_pocket_residues_in_box(receptor_atoms, box)
        scene.pocket_source = "inside the docking box"
    else:
        residues = []

    if len(residues) > MAX_POCKET_RESIDUES:
        log.debug("pocket truncated from %d to %d residues",
                  len(residues), MAX_POCKET_RESIDUES)
        residues = residues[:MAX_POCKET_RESIDUES]

    scene.pocket_residues = residues
    if residues:
        subset = pocket_atoms(receptor_atoms, residues)
        scene.pocket_text = atoms_to_pdb_text(subset, "EnzyDock binding pocket")
        scene.labels = residue_centroids(receptor_atoms, residues)

    if ligand_atoms:
        scene.interactions = find_interactions(
            receptor_atoms,
            ligand_atoms,
            hbond_distance=hbond_distance,
            contact_distance=contact_distance,
        )
        scene.hbonds = [
            item for item in scene.interactions if item.kind == KIND_HBOND
        ]
        if scene.interactions:
            log.info("viewer scene: %s", describe(scene.interactions))
    return scene


__all__ = [
    "CONTACT_MAX_DISTANCE",
    "HBOND_MAX_DISTANCE",
    "INTERACTION_STYLE",
    "Interaction",
    "KIND_HALOGEN",
    "KIND_HBOND",
    "KIND_HYDROPHOBIC",
    "KIND_LABELS",
    "KIND_PI_STACKING",
    "KIND_SALT_BRIDGE",
    "KINDS",
    "LABEL_OFFSET",
    "MAX_POCKET_RESIDUES",
    "MAX_RESIDUE_LABELS",
    "POCKET_CUTOFF",
    "POLAR_ELEMENTS",
    "HydrogenBond",
    "PocketResidue",
    "ViewerScene",
    "atom_label",
    "build_scene",
    "describe",
    "find_aromatic_side_chain_rings",
    "find_hydrogen_bonds",
    "find_interactions",
    "find_planar_rings",
    "find_pocket_residues",
    "find_pocket_residues_in_box",
    "group_counts",
    "is_hydrogen",
    "is_polar",
    "pocket_atoms",
    "residue_centroids",
]
