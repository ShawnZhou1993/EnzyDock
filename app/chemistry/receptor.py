"""Receptor (protein) preparation: structure file -> clean, protonated PDB -> PDBQT.

This module is the Step 4 counterpart of :mod:`app.chemistry.preparation`.
It implements the workflow from the project brief::

    .pdb / .cif / .mmcif
        -> read atoms                 (app.chemistry.protein, read-only)
        -> drop waters                (optional)
        -> drop HETATM hetero atoms   (optional)
        -> keep selected chains       (optional)
        -> resolve alternate locations
        -> add hydrogens              (optional, via a pluggable protonator)
        -> receptor_clean.pdb / receptor_protonated.pdb
        -> AutoDock typing + charges  (pluggable receptor converter)
        -> prepared/receptor.pdbqt

Two things are deliberately *adapter based*, exactly like the ligand side:

``protonation``
    Adding hydrogens to a macromolecule needs chemistry that neither RDKit
    nor meeko provides on its own.  :class:`RDKitProtonator` reproduces the
    hydrogen count of OpenBabel's ``obabel -h`` without needing any external
    tool, and :class:`OpenBabelProtonator` is used when it is installed.

``PDBQT conversion``
    :class:`MeekoReceptorConverter` is the primary backend (the AutoDock
    authors' own polymer machinery, which types backbone donors/acceptors
    correctly); :class:`OpenBabelReceptorConverter` is the fallback.

The user's original file is never modified: everything is written into the
run workspace.
"""

from __future__ import annotations

import abc
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Sequence

from ..utils.logger import get_logger
from ..utils.paths import ensure_dir
from .conversion import ProgressCallback
from .protein import (
    METAL_ELEMENTS,
    WATER_RESIDUES,
    AtomRecord,
    StructureParseError,
    read_atoms,
)

log = get_logger(__name__)

#: Altloc preference: blank wins, then "A", then whatever comes first.
_ALTLOC_RANK = {"": 0, " ": 0, "A": 1}

#: Elements for which a missing hydrogen is chemically meaningful.
POLAR_ELEMENTS = frozenset({"N", "O", "S"})


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class ReceptorError(ValueError):
    """Base class for receptor problems shown through the standard dialog.

    ``message`` is written for a bench scientist, ``details`` keeps the
    technical cause for *Show Technical Details*.
    """

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ReceptorPreparationError(ReceptorError):
    """Raised when a structure cannot be turned into a docking receptor."""


class ReceptorConversionError(ReceptorError):
    """Raised when a cleaned structure cannot be written as receptor PDBQT."""


# ---------------------------------------------------------------------------
# options / results
# ---------------------------------------------------------------------------
@dataclass
class ReceptorOptions:
    """Cleaning and protonation choices for receptor preparation."""

    remove_waters: bool = True
    #: Drop HETATM records (ligands, cofactors, ions).  Metals are reported
    #: but are *not* valid AutoDock types, so they have to go for Vina.
    remove_hetero: bool = True
    add_hydrogens: bool = True
    #: ``None`` keeps every chain; otherwise only the listed ones are kept.
    keep_chains: list[str] | None = None
    #: Protonation backend: ``None`` picks the best installed one.
    protonator: str | None = None
    #: PDBQT backend: ``None`` picks the best installed one.
    converter: str | None = None
    #: Keep going when a residue cannot be recognised by the converter.
    allow_bad_residues: bool = True

    def __post_init__(self) -> None:
        if self.keep_chains is not None:
            self.keep_chains = [chain.strip() for chain in self.keep_chains
                                if chain.strip()] or None


@dataclass
class PreparedReceptor:
    """Everything downstream (docking, report, viewer) needs about a receptor."""

    name: str = "receptor"
    source_path: Path | None = None
    #: Cleaned structure (waters / HETATM / chains removed).
    clean_path: Path | None = None
    pdbqt_path: Path | None = None
    chains: list[str] = field(default_factory=list)
    residue_count: int = 0
    atom_count: int = 0
    heavy_atom_count: int = 0
    hydrogen_count: int = 0
    #: ``"RDKit"``, ``"OpenBabel"``, ``"input"`` or ``"none"``.
    protonation: str = "none"
    converter: str = ""
    #: Residues the converter could not type and therefore skipped.
    ignored_residues: int = 0
    removed_waters: int = 0
    removed_hetero: int = 0
    warnings: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def is_ready(self) -> bool:
        """``True`` when a PDBQT file was produced and exists on disk."""
        return self.pdbqt_path is not None and Path(self.pdbqt_path).is_file()

    @property
    def center(self) -> tuple[float, float, float] | None:
        """Geometric centre of the prepared receptor (box starting point)."""
        return self._bounds()[0] if self.pdbqt_path else None

    def to_rows(self) -> list[tuple[str, str]]:
        """``(label, value)`` rows for the protein panel / report."""
        rows = [
            ("Docking receptor",
             self.pdbqt_path.name if self.pdbqt_path else "not prepared"),
            ("Chains", ", ".join(self.chains) if self.chains else "-"),
            ("Residues", f"{self.residue_count:,}"),
            ("Atoms", f"{self.atom_count:,}"),
            ("Hydrogens", f"{self.hydrogen_count:,} ({self.protonation})"),
            ("Removed", f"{self.removed_waters} waters, "
                        f"{self.removed_hetero} hetero atoms"),
            ("PDBQT writer", self.converter or "-"),
        ]
        if self.ignored_residues:
            rows.append(("Skipped residues", str(self.ignored_residues)))
        return rows

    def _bounds(self) -> tuple[tuple[float, float, float] | None,
                               tuple[float, float, float] | None]:
        """Read the receptor PDBQT and return ``(centre, size)``."""
        if self.pdbqt_path is None or not Path(self.pdbqt_path).is_file():
            return None, None
        xs: list[float] = []
        ys: list[float] = []
        zs: list[float] = []
        for line in Path(self.pdbqt_path).read_text(
                encoding="utf-8", errors="replace").splitlines():
            if not line.startswith(("ATOM", "HETATM")):
                continue
            try:
                xs.append(float(line[30:38]))
                ys.append(float(line[38:46]))
                zs.append(float(line[46:54]))
            except ValueError:                            # pragma: no cover
                continue
        if not xs:
            return None, None
        low = (min(xs), min(ys), min(zs))
        high = (max(xs), max(ys), max(zs))
        centre = tuple((a + b) / 2.0 for a, b in zip(low, high))
        size = tuple(b - a for a, b in zip(low, high))
        return centre, size


# ---------------------------------------------------------------------------
# cleaning
# ---------------------------------------------------------------------------
def is_water(atom: AtomRecord) -> bool:
    """``True`` for solvent residues."""
    return atom.residue.upper().strip() in WATER_RESIDUES


def is_hydrogen(atom: AtomRecord) -> bool:
    """``True`` when ``atom`` is a hydrogen (or deuterium)."""
    return (atom.element or "").upper() in ("H", "D")


def filter_atoms(
    atoms: Sequence[AtomRecord],
    options: ReceptorOptions,
) -> tuple[list[AtomRecord], dict[str, int]]:
    """Apply the cleaning options to ``atoms``.

    Returns ``(kept_atoms, statistics)`` where statistics records how many
    waters, hetero atoms, hydrogens and off-selection chains were dropped, so
    the GUI can tell the user exactly what happened to their file.
    """
    keep_chains = set(options.keep_chains) if options.keep_chains else None
    stats = {"waters": 0, "hetero": 0, "chains": 0, "hydrogens": 0,
             "altloc": 0, "input": len(atoms)}

    kept: list[AtomRecord] = []
    for atom in atoms:
        if is_water(atom) and options.remove_waters:
            stats["waters"] += 1
            continue
        if atom.record == "HETATM" and options.remove_hetero:
            stats["hetero"] += 1
            continue
        if keep_chains is not None and atom.chain not in keep_chains:
            stats["chains"] += 1
            continue
        kept.append(atom)

    deduped, dropped_altloc = resolve_altloc(kept)
    stats["altloc"] = dropped_altloc

    if options.add_hydrogens:
        # Hydrogens that came with the file are kept (and protonation is
        # skipped later); hydrogens are only removed when we cannot trust them.
        stats["hydrogens"] = sum(1 for atom in deduped if is_hydrogen(atom))

    return deduped, stats


def resolve_altloc(
    atoms: Sequence[AtomRecord],
) -> tuple[list[AtomRecord], int]:
    """Collapse alternate locations into a single conformation.

    For every ``(chain, resseq, icode, residue, atom name)`` the atom with the
    best altloc is kept: blank first, then ``A``, then the first one seen.
    Returns ``(kept_atoms, dropped_count)``.
    """
    chosen: dict[tuple[str, int, str, str, str], AtomRecord] = {}
    order: list[tuple[str, int, str, str, str]] = []
    dropped = 0

    for atom in atoms:
        key = (atom.chain, atom.resseq, atom.icode, atom.residue,
               atom.name.strip())
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = atom
            order.append(key)
            continue
        dropped += 1
        if _altloc_rank(atom) < _altloc_rank(existing):
            chosen[key] = atom

    return [chosen[key] for key in order], dropped


def _altloc_rank(atom: AtomRecord) -> int:
    """Lower is better: blank > A > anything else."""
    return _ALTLOC_RANK.get(atom.altloc.strip().upper(), 2)


def write_pdb(atoms: Sequence[AtomRecord], destination: str | Path,
              title: str = "EnzyDock prepared receptor") -> Path:
    """Write ``atoms`` as a PDB file at ``destination``.

    Columns follow the RCSB specification so that downstream tools (meeko,
    OpenBabel, 3Dmol.js) can read the result without surprises.
    """
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(atoms_to_pdb_text(atoms, title), encoding="utf-8")
    log.debug("wrote %d atoms to %s", len(atoms), path.name)
    return path


def atoms_to_pdb_text(atoms: Sequence[AtomRecord],
                      title: str = "EnzyDock prepared receptor") -> str:
    """Render ``atoms`` as PDB text (Step 7: feed a subset to the 3D viewer)."""
    lines = [f"REMARK    {title}",
             "REMARK    generated by EnzyDock - do not edit"]
    for serial, atom in enumerate(atoms, start=1):
        lines.append(_pdb_atom_line(serial, atom))
    lines.extend(("TER", "END"))
    return "\n".join(lines) + "\n"


def _pdb_atom_line(serial: int, atom: AtomRecord) -> str:
    """Format one PDB coordinate record.

    Column layout (1-based): ``1-6 record | 7-11 serial | 13-16 name |
    17 altLoc | 18-20 resName | 22 chain | 23-26 resSeq | 27 iCode |
    31-38 x | 39-46 y | 47-54 z | 55-60 occupancy | 61-66 tempFactor |
    77-78 element``.
    """
    element = (atom.element or "").strip().upper() or _element_from_name(atom.name)
    residue = atom.residue.strip()[:3].rjust(3)
    name = _format_atom_name(atom.name, element)
    return (
        f"{atom.record:<6s}"
        f"{_clamp(serial, 99999):5d} "
        f"{name:<4s}"
        f"{(atom.altloc or ' ')[:1]:1s}"
        f"{residue:>3s}"
        f" "
        f"{(atom.chain or ' ')[:1]:1s}"
        f"{_clamp(atom.resseq, 9999):4d}"
        f"{(atom.icode or ' ')[:1]:1s}"
        f"   "
        f"{atom.x:8.3f}{atom.y:8.3f}{atom.z:8.3f}"
        f"{atom.occupancy:6.2f}{atom.temp_factor:6.2f}"
        f"          "
        f"{element[:2]:>2s}"
    )


def _clamp(value: int, maximum: int) -> int:
    """Keep ``value`` inside ``0..maximum`` so PDB columns never overflow."""
    return max(0, min(int(value), maximum))


def _format_atom_name(name: str, element: str) -> str:
    """Return a 4-character PDB atom name with the element right-justified.

    ``"CA"`` becomes ``" CA "`` and ``"H1"`` becomes ``" H1 "``; names that
    already carry padding are passed through unchanged.
    """
    stripped = (name or "").strip()
    if not stripped:
        stripped = element
    if len(stripped) >= 4:
        return stripped[:4]
    if len(element) == 1 and not stripped[0].isdigit():
        return f" {stripped:<3s}"
    return f"{stripped:<4s}"


def _element_from_name(name: str) -> str:
    """Best-effort element symbol from an atom name (``" CA "`` -> ``"C"``)."""
    stripped = (name or "").strip()
    if not stripped:
        return ""
    if stripped[0].isdigit():
        stripped = stripped[1:]
    element = ""
    for char in stripped:
        if char.isalpha():
            element += char
        else:
            break
    return element[:2].upper()


# ---------------------------------------------------------------------------
# protonation
# ---------------------------------------------------------------------------
class Protonator(abc.ABC):
    """Interface for "add hydrogens to a cleaned receptor"."""

    name: ClassVar[str] = "abstract"
    priority: ClassVar[int] = 100

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return ``True`` when this protonator can run on this machine."""

    @abc.abstractmethod
    def add_hydrogens(self, atoms: Sequence[AtomRecord],
                      clean_path: Path) -> tuple[list[AtomRecord], str]:
        """Return ``(hydrogen_records, method_name)``.

        The returned records carry the chain, residue and sequence number of
        the atom each hydrogen belongs to, so :func:`merge_hydrogens` can put
        them back next to their residue.

        Raises
        ------
        ReceptorPreparationError
            When hydrogens cannot be placed.
        """


class RDKitProtonator(Protonator):
    """Geometric protonation using RDKit's valence model.

    RDKit reads the cleaned PDB, infers bonds by proximity, sanitises the
    molecule (which fixes formal charges and therefore the correct implicit
    hydrogen counts) and then places explicit hydrogens.  On ubiquitin this
    produces exactly the same atom count as ``obabel -h --pH 7.4``, so the
    result is trustworthy without needing any external tool.

    The generated hydrogens are mapped back onto the residue they belong to:
    RDKit writes *new* atoms into a dummy ``UNL`` residue, which would break
    every downstream residue-based step.
    """

    name = "RDKit"
    priority = 10

    def is_available(self) -> bool:
        try:
            from rdkit import Chem                        # noqa: F401
        except Exception:                                 # pragma: no cover
            return False
        return True

    def add_hydrogens(self, atoms: Sequence[AtomRecord],
                      clean_path: Path) -> tuple[list[AtomRecord], str]:
        try:
            from rdkit import Chem, RDLogger
            RDLogger.DisableLog("rdApp.*")
        except Exception as exc:                          # pragma: no cover
            raise ReceptorPreparationError(
                "Hydrogens could not be added because RDKit is unavailable.",
                f"import rdkit failed: {exc}",
            ) from exc

        text = Path(clean_path).read_text(encoding="utf-8", errors="replace")
        try:
            mol = Chem.MolFromPDBBlock(text, removeHs=False, sanitize=True,
                                       proximityBonding=True)
        except Exception as exc:
            raise ReceptorPreparationError(
                "Hydrogens could not be added: this structure contains "
                "chemistry RDKit cannot interpret (for example an unusual "
                "residue or a metal centre). Try preparing it without "
                "hydrogens, or remove the offending residues.",
                f"MolFromPDBBlock raised {type(exc).__name__}: {exc}",
            ) from exc

        if mol is None:
            raise ReceptorPreparationError(
                "Hydrogens could not be added: the cleaned structure could "
                "not be interpreted as a molecule.",
                "MolFromPDBBlock returned None",
            )

        if mol.GetNumAtoms() != len(atoms):
            raise ReceptorPreparationError(
                "Hydrogens could not be added safely: some atoms of the "
                "cleaned structure were not recognised, so hydrogens could "
                "not be assigned to the right residues.",
                f"RDKit read {mol.GetNumAtoms()} of {len(atoms)} atoms",
            )

        try:
            protonated = Chem.AddHs(mol, addCoords=True)
        except Exception as exc:                          # pragma: no cover
            raise ReceptorPreparationError(
                "Hydrogens could not be added to this structure.",
                f"Chem.AddHs raised {type(exc).__name__}: {exc}",
            ) from exc

        return _collect_new_hydrogens(protonated, mol.GetNumAtoms(), atoms), self.name


class OpenBabelProtonator(Protonator):
    """Protonation through the ``obabel`` command line tool.

    Preferred when available because it applies a real pH model, but Windows
    installations rarely put ``obabel`` on ``PATH``, so the executable is
    looked up in the usual install directories too.
    """

    name = "OpenBabel"
    priority = 50

    def __init__(self, ph: float = 7.4, executable: str | None = None) -> None:
        self.ph = ph
        self._executable = executable

    def is_available(self) -> bool:
        return self._executable_path() is not None

    def _executable_path(self) -> str | None:
        if self._executable and Path(self._executable).is_file():
            return self._executable
        from ..utils.executables import find_openbabel

        return find_openbabel()

    def add_hydrogens(self, atoms: Sequence[AtomRecord],
                      clean_path: Path) -> tuple[list[AtomRecord], str]:
        executable = self._executable_path()
        if executable is None:                             # pragma: no cover
            raise ReceptorPreparationError(
                "OpenBabel is not installed, so hydrogens cannot be added "
                "with it.", "obabel executable not found")

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "protonated.pdb"
            command = [executable, "-ipdb", str(Path(clean_path)),
                       "-opdb", "-h", "--pH", f"{self.ph:g}",
                       "-O", str(out_path)]
            try:
                completed = subprocess.run(command, capture_output=True,
                                           text=True, timeout=300, check=False)
            except Exception as exc:
                raise ReceptorPreparationError(
                    "OpenBabel could not be started to add hydrogens.",
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            if completed.returncode != 0 or not out_path.is_file():
                raise ReceptorPreparationError(
                    "OpenBabel could not add hydrogens to this structure.",
                    f"exit={completed.returncode} "
                    f"stderr={completed.stderr[:500]}",
                )
            try:
                produced = read_atoms(out_path)
            except StructureParseError as exc:
                raise ReceptorPreparationError(
                    "OpenBabel produced a structure EnzyDock cannot read back.",
                    str(exc),
                ) from exc

        hydrogens = [atom for atom in produced if is_hydrogen(atom)]
        if not hydrogens:                                  # pragma: no cover
            raise ReceptorPreparationError(
                "OpenBabel did not add any hydrogens to this structure.",
                f"obabel produced {len(produced)} atoms, none of them hydrogen",
            )
        return hydrogens, self.name


#: Every protonator EnzyDock knows about, in registration order.
_PROTONATOR_TYPES: tuple[type[Protonator], ...] = (
    RDKitProtonator,
    OpenBabelProtonator,
)


def available_protonators() -> list[Protonator]:
    """Return usable protonator instances, best one first."""
    found: list[Protonator] = []
    for cls in _PROTONATOR_TYPES:
        try:
            instance = cls()
        except Exception as exc:                           # pragma: no cover
            log.debug("cannot build protonator %s: %s", cls.__name__, exc)
            continue
        try:
            if instance.is_available():
                found.append(instance)
        except Exception as exc:                           # pragma: no cover
            log.debug("availability check failed for %s: %s", cls.name, exc)
    found.sort(key=lambda p: p.priority)
    return found


def get_protonator(preferred: str | None = None) -> Protonator:
    """Return a usable protonator, or raise with a user facing message."""
    usable = available_protonators()
    if preferred:
        for protonator in usable:
            if protonator.name.lower() == preferred.lower():
                return protonator
        raise ReceptorPreparationError(
            f"The requested protonation method '{preferred}' is not available.",
            f"available: {[p.name for p in usable]}",
        )
    if not usable:
        raise ReceptorPreparationError(
            "Hydrogens cannot be added on this computer because no "
            "protonation tool is available.",
            "no protonator is usable",
        )
    return usable[0]


def _collect_new_hydrogens(
    protonated: object,
    original_count: int,
    atoms: Sequence[AtomRecord],
) -> list[AtomRecord]:
    """Extract the hydrogens RDKit added and attach them to their residue.

    Every new hydrogen is given the residue name, chain and sequence number
    of the heavy atom it is bonded to, plus a per-residue ``H1``/``H2`` ...
    name.
    """
    conformer = protonated.GetConformer()
    per_residue: dict[tuple[str, int, str], int] = {}
    records: list[AtomRecord] = []

    for index in range(original_count, protonated.GetNumAtoms()):
        atom = protonated.GetAtomWithIdx(index)
        parent_index = _parent_index(atom, original_count)
        if parent_index is None:                           # pragma: no cover
            continue
        parent = atoms[parent_index]
        key = (parent.chain, parent.resseq, parent.residue)
        per_residue[key] = per_residue.get(key, 0) + 1

        position = conformer.GetAtomPosition(index)
        records.append(AtomRecord(
            record=parent.record,
            name=f"H{per_residue[key]}",
            residue=parent.residue,
            chain=parent.chain,
            resseq=parent.resseq,
            element="H",
            x=float(position.x),
            y=float(position.y),
            z=float(position.z),
            icode=parent.icode,
        ))
    return records


def _parent_index(atom: object, original_count: int) -> int | None:
    """Index of the heavy atom ``atom`` is bonded to, or ``None``."""
    for neighbour in atom.GetNeighbors():
        index = neighbour.GetIdx()
        if index < original_count:
            return index
    return None


def merge_hydrogens(
    atoms: Sequence[AtomRecord],
    hydrogens: Sequence[AtomRecord],
) -> list[AtomRecord]:
    """Insert ``hydrogens`` directly after the residue they belong to.

    Receptor parsers (meeko in particular) require every residue to be a
    *contiguous* block of PDB lines; appending all hydrogens at the end of the
    file breaks that and is rejected.  Each hydrogen carries the chain,
    residue name and sequence number of its parent atom, so it can be placed
    back where it belongs.
    """
    if not hydrogens:
        return list(atoms)

    by_residue: dict[tuple[str, int, str], list[AtomRecord]] = {}
    for hydrogen in hydrogens:
        key = (hydrogen.chain, hydrogen.resseq, hydrogen.residue)
        by_residue.setdefault(key, []).append(hydrogen)

    merged: list[AtomRecord] = []
    previous: tuple[str, int, str] | None = None
    for atom in atoms:
        key = (atom.chain, atom.resseq, atom.residue)
        if previous is not None and key != previous:
            merged.extend(by_residue.pop(previous, []))
        merged.append(atom)
        previous = key
    if previous is not None:
        merged.extend(by_residue.pop(previous, []))
    for leftover in by_residue.values():                  # pragma: no cover
        merged.extend(leftover)
    return merged


def strip_hydrogens(pdbqt_text: str) -> str:
    """Remove hydrogen atoms (AutoDock types ``H`` and ``HD``) from a PDBQT.

    Used when the user explicitly asks for a receptor without hydrogens.
    """
    kept = [line for line in pdbqt_text.splitlines()
            if not (line.startswith(("ATOM", "HETATM"))
                    and line[77:79].strip().upper() in ("H", "HD"))]
    return "\n".join(kept) + ("\n" if kept else "")


# ---------------------------------------------------------------------------
# PDBQT conversion
# ---------------------------------------------------------------------------
class ReceptorConverter(abc.ABC):
    """Interface every receptor PDBQT backend must implement."""

    name: ClassVar[str] = "abstract"
    priority: ClassVar[int] = 100
    #: ``True`` when the backend fills in the polar hydrogens AutoDock needs
    #: by itself, so EnzyDock does not have to protonate first.
    protonates: ClassVar[bool] = False

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return ``True`` when this backend can run on the current machine."""

    @abc.abstractmethod
    def to_pdbqt_string(self, pdb_path: Path,
                        allow_bad_residues: bool = True) -> tuple[str, int]:
        """Convert a cleaned PDB file into receptor PDBQT text.

        Returns ``(pdbqt_text, ignored_residue_count)``.
        """


class MeekoReceptorConverter(ReceptorConverter):
    """Receptor PDBQT writer based on meeko's polymer machinery.

    meeko perceives residues, assigns AutoDock types (``N`` for donors,
    ``OA`` for acceptors, ...) and writes the *rigid* part of the receptor,
    which is what AutoDock Vina expects.
    """

    name = "meeko"
    priority = 10
    #: meeko completes every residue from its templates, which adds exactly
    #: the polar hydrogens AutoDock Vina needs (typed ``HD``).
    protonates = True

    def is_available(self) -> bool:
        try:
            from meeko import Polymer                     # noqa: F401
        except Exception as exc:                           # pragma: no cover
            log.debug("meeko not importable: %s", exc)
            return False
        return True

    def to_pdbqt_string(self, pdb_path: Path,
                        allow_bad_residues: bool = True) -> tuple[str, int]:
        try:
            from meeko import (
                MoleculePreparation,
                PDBQTWriterLegacy,
                Polymer,
                PolymerCreationError,
                ResidueChemTemplates,
            )
        except Exception as exc:                           # pragma: no cover
            raise ReceptorConversionError(
                "The receptor writer (meeko) is not installed, so the protein "
                "cannot be prepared for docking.",
                f"import meeko failed: {exc}",
            ) from exc

        text = Path(pdb_path).read_text(encoding="utf-8", errors="replace")
        try:
            polymer = Polymer.from_pdb_string(
                text,
                ResidueChemTemplates.create_from_defaults(),
                MoleculePreparation(),
                allow_bad_res=allow_bad_residues,
            )
        except PolymerCreationError as exc:
            raise ReceptorConversionError(
                "The cleaned protein could not be turned into a docking "
                "receptor: none of its residues could be recognised. "
                "Please check that the file contains a protein chain rather "
                "than only water or hetero atoms.",
                f"PolymerCreationError: {exc}",
            ) from exc
        except Exception as exc:
            raise ReceptorConversionError(
                "The cleaned protein could not be turned into a docking "
                "receptor because its residues could not be interpreted.",
                f"{type(exc).__name__}: {exc}",
            ) from exc

        try:
            ignored_monomers = polymer.get_ignored_monomers()
        except Exception:                                  # pragma: no cover
            ignored_monomers = []
        ignored = len(ignored_monomers)
        if not getattr(polymer, "monomers", None):
            skipped = ", ".join(str(key) for key in list(ignored_monomers)[:20])
            raise ReceptorConversionError(
                "None of the residues in the cleaned structure could be "
                "recognised, so a docking receptor could not be built. This "
                "usually means the residues are incomplete (missing side "
                "chain atoms) or are not standard amino acids.",
                f"meeko produced an empty polymer; skipped: {skipped or 'unknown'}",
            )

        try:
            result = PDBQTWriterLegacy.write_from_polymer(polymer)
        except Exception as exc:                           # pragma: no cover
            raise ReceptorConversionError(
                "The docking receptor could not be written in PDBQT format.",
                f"PDBQTWriterLegacy failed: {type(exc).__name__}: {exc}",
            ) from exc

        pdbqt_text = result[0] if isinstance(result, tuple) else result
        if not (pdbqt_text or "").strip():
            raise ReceptorConversionError(
                "The docking receptor came out empty. Please check that the "
                "selected chains contain protein residues.",
                "meeko produced an empty receptor PDBQT",
            )
        return pdbqt_text, ignored


class OpenBabelReceptorConverter(ReceptorConverter):
    """Fallback receptor writer based on the ``obabel`` command line tool."""

    name = "OpenBabel"
    priority = 50
    protonates = False

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def is_available(self) -> bool:
        return self._executable_path() is not None

    def _executable_path(self) -> str | None:
        if self._executable and Path(self._executable).is_file():
            return self._executable
        from ..utils.executables import find_openbabel

        return find_openbabel()

    def to_pdbqt_string(self, pdb_path: Path,
                        allow_bad_residues: bool = True) -> tuple[str, int]:
        executable = self._executable_path()
        if executable is None:
            raise ReceptorConversionError(
                "No receptor writer is available on this computer. Please "
                "install meeko (pip install meeko) and restart EnzyDock.",
                "meeko and obabel are both unavailable",
            )

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "receptor.pdbqt"
            command = [executable, "-ipdb", str(Path(pdb_path)), "-opdbqt",
                       "-xr", "--partialcharge", "gasteiger",
                       "-O", str(out_path)]
            try:
                completed = subprocess.run(command, capture_output=True,
                                           text=True, timeout=300, check=False)
            except Exception as exc:
                raise ReceptorConversionError(
                    "OpenBabel could not be started on this computer.",
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            if completed.returncode != 0 or not out_path.is_file():
                raise ReceptorConversionError(
                    "OpenBabel could not convert this protein into a docking "
                    "receptor.",
                    f"exit={completed.returncode} "
                    f"stderr={completed.stderr[:500]}",
                )
            text = out_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():                               # pragma: no cover
            raise ReceptorConversionError(
                "OpenBabel produced an empty docking receptor.",
                f"stdout={completed.stdout[:500]}",
            )
        return text, 0


_CONVERTER_TYPES: tuple[type[ReceptorConverter], ...] = (
    MeekoReceptorConverter,
    OpenBabelReceptorConverter,
)


def available_converters() -> list[ReceptorConverter]:
    """Return usable receptor converter instances, best one first."""
    found: list[ReceptorConverter] = []
    for cls in _CONVERTER_TYPES:
        try:
            instance = cls()
        except Exception as exc:                           # pragma: no cover
            log.debug("cannot build receptor converter %s: %s", cls.__name__, exc)
            continue
        try:
            if instance.is_available():
                found.append(instance)
        except Exception as exc:                           # pragma: no cover
            log.debug("availability check failed for %s: %s", cls.name, exc)
    found.sort(key=lambda c: c.priority)
    return found


def get_converter(preferred: str | None = None) -> ReceptorConverter:
    """Return a usable receptor converter, or raise with a friendly message."""
    usable = available_converters()
    if preferred:
        for converter in usable:
            if converter.name.lower() == preferred.lower():
                return converter
        raise ReceptorConversionError(
            f"The requested receptor writer '{preferred}' is not available.",
            f"available: {[c.name for c in usable]}",
        )
    if not usable:
        raise ReceptorConversionError(
            "No receptor writer is available on this computer, so the protein "
            "cannot be prepared for docking. Please install meeko "
            "(pip install meeko) and restart EnzyDock.",
            "no receptor converter is usable",
        )
    return usable[0]


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
def prepare_receptor(
    path: str | Path,
    output_dir: str | Path | None = None,
    options: ReceptorOptions | None = None,
    progress: ProgressCallback | None = None,
) -> PreparedReceptor:
    """Turn a structure file into a docking-ready receptor PDBQT.

    Parameters
    ----------
    path:
        ``.pdb``, ``.ent``, ``.cif`` or ``.mmcif`` file.  It is only read.
    output_dir:
        Workspace directory (``<workspace>/prepared``).  When given, the
        cleaned PDB and ``receptor.pdbqt`` are written there.
    options:
        See :class:`ReceptorOptions`; defaults are used when omitted.
    progress:
        Optional ``(percent, message)`` callback for the GUI progress bar.

    Raises
    ------
    ReceptorError
        With a user-readable message for every failure mode.  Parse problems
        surface as :class:`~app.chemistry.protein.StructureParseError`, which
        the GUI already knows how to present.
    """
    options = options or ReceptorOptions()
    source = Path(path)
    started = time.perf_counter()

    def report(percent: int, message: str) -> None:
        log.debug("[receptor] %d%% %s", percent, message)
        if progress is not None:
            progress(percent, message)

    report(5, f"Reading {source.name} ...")
    atoms = read_atoms(source)                     # may raise StructureParseError

    report(20, "Cleaning the structure ...")
    cleaned, stats = filter_atoms(atoms, options)
    if not cleaned:
        raise ReceptorPreparationError(
            "Nothing is left of this structure after applying your cleaning "
            "options. Try keeping more chains, or switch off "
            "'Remove hetero atoms'.",
            f"all {stats['input']} atoms were filtered out",
        )

    warnings: list[str] = []
    if stats["waters"]:
        warnings.append(f"Removed {stats['waters']} water molecules.")
    if stats["hetero"]:
        warnings.append(
            f"Removed {stats['hetero']} hetero atoms (ligands, cofactors or "
            "ions). Metals are not valid AutoDock types, so docking ignores "
            "them unless you keep them as part of the ligand."
        )
    if stats["chains"]:
        warnings.append(
            f"Removed {stats['chains']} atoms from chains that were not selected."
        )
    if stats["altloc"]:
        warnings.append(
            f"Resolved {stats['altloc']} alternate side-chain locations "
            "(the first conformation of each was kept)."
        )

    directory = ensure_dir(output_dir) if output_dir is not None else None
    clean_path = directory / "receptor_clean.pdb" if directory else None
    if clean_path is not None:
        write_pdb(cleaned, clean_path, title=f"cleaned from {source.name}")

    # --- hydrogens ---------------------------------------------------------
    # Most receptors need the polar hydrogens AutoDock uses for its hydrogen
    # bond term.  meeko adds them from its residue templates; the OpenBabel
    # fallback cannot, so it is protonated here instead.
    protonation = "none"
    working_path = clean_path
    if not options.add_hydrogens:
        cleaned = [atom for atom in cleaned if not is_hydrogen(atom)]
        if clean_path is not None:
            write_pdb(cleaned, clean_path, title=f"cleaned from {source.name}")
    else:
        converter = get_converter(options.converter)
        existing_hydrogens = sum(1 for atom in cleaned if is_hydrogen(atom))
        if existing_hydrogens:
            protonation = "input"
            warnings.append(
                f"The structure already contains {existing_hydrogens} "
                "hydrogens; they were kept as they are."
            )
        elif converter.protonates:
            protonation = f"{converter.name} (polar)"
        else:
            report(40, "Adding hydrogens ...")
            try:
                protonator = get_protonator(options.protonator)
                hydrogens, method = protonator.add_hydrogens(cleaned, clean_path)
            except ReceptorError as exc:
                warnings.append(
                    f"Hydrogens could not be added ({exc.message}). Docking "
                    "will continue without them; AutoDock Vina still scores "
                    "hydrogen bonds from the donor/acceptor atom types, but "
                    "the geometry is slightly less precise."
                )
                log.warning("protonation skipped: %s", exc.message)
                hydrogens, method = [], "none"
            if hydrogens:
                cleaned = merge_hydrogens(cleaned, hydrogens)
                protonation = method
                if directory is not None:
                    working_path = directory / "receptor_protonated.pdb"
                    write_pdb(cleaned, working_path,
                              title=f"protonated with {method}")

    # --- PDBQT -------------------------------------------------------------
    report(60, "Assigning AutoDock atom types ...")
    if working_path is None or not Path(working_path).is_file():
        # No workspace: convert straight from an in-memory structure.
        with tempfile.TemporaryDirectory() as tmp:
            working_path = write_pdb(cleaned, Path(tmp) / "receptor_clean.pdb")
            pdbqt_text, ignored, converter_name = _convert(working_path, options)
    else:
        pdbqt_text, ignored, converter_name = _convert(working_path, options)

    if not options.add_hydrogens:
        pdbqt_text = strip_hydrogens(pdbqt_text)

    if ignored:
        warnings.append(
            f"{ignored} residue(s) could not be recognised and were skipped. "
            "This is normal for modified or incomplete residues."
        )

    report(90, "Writing the docking receptor ...")
    pdbqt_path: Path | None = None
    if directory is not None:
        pdbqt_path = directory / "receptor.pdbqt"
        pdbqt_path.write_text(pdbqt_text, encoding="utf-8")

    # Report what actually ended up in the docking receptor, not what was
    # in the intermediate files - that is the number Vina will see.
    atom_count, hydrogen_count = _count_pdbqt_atoms(pdbqt_text)
    residues = {(atom.chain, atom.resseq, atom.residue) for atom in cleaned}
    chains: list[str] = []
    for atom in cleaned:
        if atom.chain and atom.chain not in chains:
            chains.append(atom.chain)

    prepared = PreparedReceptor(
        name=source.stem,
        source_path=source,
        clean_path=clean_path,
        pdbqt_path=pdbqt_path,
        chains=sorted(chains),
        residue_count=len(residues),
        atom_count=atom_count,
        heavy_atom_count=atom_count - hydrogen_count,
        hydrogen_count=hydrogen_count,
        protonation=protonation,
        converter=converter_name,
        ignored_residues=ignored,
        removed_waters=stats["waters"],
        removed_hetero=stats["hetero"],
        warnings=warnings,
        elapsed_seconds=time.perf_counter() - started,
    )
    report(100, f"Receptor ready ({atom_count} atoms)")
    log.info("prepared receptor %s: %d residues, %d PDBQT atoms, %d H (%s), "
             "%.2fs, writer=%s", prepared.name, prepared.residue_count,
             atom_count, hydrogen_count, protonation,
             prepared.elapsed_seconds, prepared.converter)
    return prepared


def _count_pdbqt_atoms(pdbqt_text: str) -> tuple[int, int]:
    """Return ``(total_atoms, hydrogen_atoms)`` of a receptor PDBQT."""
    total = 0
    hydrogens = 0
    for line in pdbqt_text.splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        total += 1
        if line[77:79].strip().upper() in ("H", "HD"):
            hydrogens += 1
    return total, hydrogens


def _convert(pdb_path: Path, options: ReceptorOptions) -> tuple[str, int, str]:
    """Run the selected receptor converter.

    Returns ``(pdbqt_text, ignored_residue_count, converter_name)``.
    """
    converter = get_converter(options.converter)
    text, ignored = converter.to_pdbqt_string(Path(pdb_path),
                                              options.allow_bad_residues)
    return text, ignored, converter.name


def summarize_pdbqt(pdbqt_path: str | Path) -> dict[str, int]:
    """Quick statistics about a receptor PDBQT file (atoms / typed atoms)."""
    path = Path(pdbqt_path)
    if not path.is_file():                                 # pragma: no cover
        return {}
    atoms = 0
    types: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(("ATOM", "HETATM")):
            atoms += 1
            if len(line) >= 79:
                types.add(line[77:79].strip())
    return {"atoms": atoms, "atom_types": len(types)}


__all__ = [
    "POLAR_ELEMENTS",
    "MeekoReceptorConverter",
    "OpenBabelProtonator",
    "OpenBabelReceptorConverter",
    "PreparedReceptor",
    "Protonator",
    "RDKitProtonator",
    "ReceptorConversionError",
    "ReceptorConverter",
    "ReceptorError",
    "ReceptorOptions",
    "ReceptorPreparationError",
    "available_converters",
    "available_protonators",
    "filter_atoms",
    "get_converter",
    "get_protonator",
    "is_hydrogen",
    "is_water",
    "prepare_receptor",
    "resolve_altloc",
    "summarize_pdbqt",
    "write_pdb",
]
