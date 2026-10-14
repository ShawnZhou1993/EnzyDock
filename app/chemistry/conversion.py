"""Molecule -> PDBQT conversion, behind a pluggable adapter interface.

AutoDock Vina cannot read RDKit molecules directly: it needs ``.pdbqt`` files
that carry AutoDock atom types and Gasteiger charges.  Several tools can
produce them, and which one is installed varies wildly between machines
(especially on Windows, where OpenBabel's Python bindings rarely have wheels).

EnzyDock therefore hides the whole thing behind a tiny interface:

    PDBQTConverter          - abstract base, every backend implements this
    MeekoConverter          - preferred: pure Python, ships with RDKit-friendly API
    OpenBabelConverter      - fallback: ``obabel`` CLI or the ``openbabel`` module

Callers never name a backend; they call :func:`write_pdbqt` and get the best
available converter.  Adding Smina/GNINA-specific writers later only means
registering one more class.

Typical use::

    from app.chemistry.conversion import write_pdbqt
    path = write_pdbqt(mol, workspace / "prepared" / "ligand.pdbqt")
"""

from __future__ import annotations

import abc
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, ClassVar

from rdkit import Chem

from ..utils.logger import get_logger

log = get_logger(__name__)

#: Marks a problem that the user can act on (bad molecule, missing backend).
class ConversionError(RuntimeError):
    """Raised when a molecule cannot be written as PDBQT.

    ``message`` is written for a bench scientist; ``details`` keeps the
    technical cause for the *Show Technical Details* dialog.
    """

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class PDBQTConverter(abc.ABC):
    """Interface every PDBQT backend must implement.

    Subclasses are stateless and cheap to construct: the registry builds one
    per call so that availability is re-checked (a user may install OpenBabel
    while EnzyDock is running).
    """

    #: Short human readable name, shown in logs and the status bar.
    name: ClassVar[str] = "abstract"

    #: Lower numbers are preferred when picking a default converter.
    priority: ClassVar[int] = 100

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return ``True`` when this backend can run on the current machine."""

    @abc.abstractmethod
    def to_pdbqt_string(self, mol: Chem.Mol) -> str:
        """Convert an RDKit molecule (with 3D coordinates) into PDBQT text.

        Raises
        ------
        ConversionError
            With a user facing message when the molecule cannot be typed.
        """

    # -- shared helpers ----------------------------------------------------
    def _require_3d(self, mol: Chem.Mol) -> None:
        """Reject molecules without a conformer before handing them to a backend."""
        if mol is None:
            raise ConversionError(
                "There is no molecule to convert.", "to_pdbqt_string received None"
            )
        if mol.GetNumConformers() == 0 or not mol.GetConformer().Is3D():
            raise ConversionError(
                "This ligand has no 3D coordinates yet, so it cannot be converted "
                "for docking. Please generate a 3D structure first.",
                "molecule has no 3D conformer",
            )

    def write(self, mol: Chem.Mol, destination: Path | str) -> Path:
        """Convert ``mol`` and write the result to ``destination``."""
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = self.to_pdbqt_string(mol)
        path.write_text(text, encoding="utf-8")
        log.info("wrote %s using the %s converter (%d lines)",
                 path.name, self.name, len(text.splitlines()))
        return path


class MeekoConverter(PDBQTConverter):
    """PDBQT writer based on ``meeko`` (the AutoDock/Vina authors' own library).

    meeko handles AutoDock atom typing, Gasteiger charges, rotatable bond
    detection and merging of non-polar hydrogens, which is exactly what Vina
    expects.  It is pure Python, so it works on every platform.
    """

    name = "meeko"
    priority = 10

    def __init__(self, charge_model: str = "gasteiger",
                 merge_hydrogens: bool = True) -> None:
        self._charge_model = charge_model
        self._merge_hydrogens = merge_hydrogens

    def is_available(self) -> bool:
        try:
            import meeko                                  # noqa: F401
        except Exception as exc:                          # pragma: no cover
            log.debug("meeko not importable: %s", exc)
            return False
        return True

    def to_pdbqt_string(self, mol: Chem.Mol) -> str:
        self._require_3d(mol)
        try:
            from meeko import MoleculePreparation
            from meeko import PDBQTWriterLegacy
        except Exception as exc:                          # pragma: no cover
            raise ConversionError(
                "The PDBQT writer (meeko) is not installed, so this ligand "
                "cannot be prepared for docking.",
                f"import meeko failed: {exc}",
            ) from exc

        try:
            preparator = MoleculePreparation(
                charge_model=self._charge_model,
                merge_these_atom_types=("H",) if self._merge_hydrogens else (),
            )
            setups = preparator.prepare(mol)
        except Exception as exc:
            raise ConversionError(
                "This ligand could not be prepared for docking: its atoms could "
                "not be assigned AutoDock types. Unusual elements or an exotic "
                "chemistry are the usual cause.",
                f"meeko MoleculePreparation.prepare failed: {type(exc).__name__}: {exc}",
            ) from exc

        if not setups:
            raise ConversionError(
                "This ligand could not be prepared for docking: no usable "
                "molecule model was produced.",
                "meeko returned an empty setup list",
            )

        try:
            result = PDBQTWriterLegacy.write_string(setups[0])
        except Exception as exc:                          # pragma: no cover
            raise ConversionError(
                "This ligand could not be written in PDBQT format.",
                f"PDBQTWriterLegacy failed: {type(exc).__name__}: {exc}",
            ) from exc

        # meeko >= 0.5 returns (text, ok, error_message).
        if isinstance(result, tuple):
            text, ok, error = (list(result) + [""])[:3]
            if not ok or not (text or "").strip():
                raise ConversionError(
                    "This ligand could not be written in PDBQT format.",
                    f"meeko reported: {error or 'empty output'}",
                )
        else:                                             # pragma: no cover
            text = result

        return text


class OpenBabelConverter(PDBQTConverter):
    """Fallback PDBQT writer based on OpenBabel.

    Two routes are supported, in this order:

    1. the ``openbabel``/``pybel`` Python module,
    2. the ``obabel`` command line tool (what most Windows installs provide).

    OpenBabel is only used when meeko is unavailable, because its PDBQT
    torsion model differs slightly from meeko's.
    """

    name = "OpenBabel"
    priority = 50

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def is_available(self) -> bool:
        return self._module_route() is not None or self._cli_route() is not None

    # -- discovery ---------------------------------------------------------
    def _module_route(self) -> bool | None:
        """``True`` when the Python bindings import cleanly, else ``None``."""
        try:
            import openbabel                              # noqa: F401
        except Exception:
            return None
        return True

    def _cli_route(self) -> str | None:
        """Path to ``obabel`` when it is on ``PATH`` or in a default install dir."""
        if self._executable and Path(self._executable).is_file():
            return self._executable
        from ..utils.executables import find_openbabel

        # Windows installers do not add OpenBabel to PATH and Homebrew does
        # not link the keg either, so a directory search is needed on both.
        return find_openbabel()

    # -- conversion --------------------------------------------------------
    def to_pdbqt_string(self, mol: Chem.Mol) -> str:
        self._require_3d(mol)
        if self._module_route():
            return self._via_module(mol)
        executable = self._cli_route()
        if executable is None:
            raise ConversionError(
                "No PDBQT writer is available on this computer. Please install "
                "meeko (pip install meeko) or OpenBabel, then try again.",
                "neither meeko nor obabel could be found",
            )
        return self._via_cli(mol, executable)

    def _via_module(self, mol: Chem.Mol) -> str:          # pragma: no cover
        """Convert through the ``pybel`` bindings."""
        try:
            from openbabel import pybel
        except Exception as exc:
            raise ConversionError(
                "OpenBabel is installed but its Python interface could not be used.",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        with tempfile.TemporaryDirectory() as tmp:
            mol_path = Path(tmp) / "ligand.mol"
            Chem.MolToMolFile(mol, str(mol_path))
            try:
                molecule = next(iter(pybel.readfile("mol", str(mol_path))))
                molecule.addh()
                return molecule.write("pdbqt")
            except Exception as exc:
                raise ConversionError(
                    "OpenBabel could not convert this ligand into PDBQT format.",
                    f"{type(exc).__name__}: {exc}",
                ) from exc

    def _via_cli(self, mol: Chem.Mol, executable: str) -> str:
        """Convert by calling ``obabel`` on a temporary MDL file."""
        with tempfile.TemporaryDirectory() as tmp:
            mol_path = Path(tmp) / "ligand.mol"
            out_path = Path(tmp) / "ligand.pdbqt"
            try:
                Chem.MolToMolFile(mol, str(mol_path))
            except Exception as exc:                      # pragma: no cover
                raise ConversionError(
                    "This ligand could not be handed to OpenBabel.",
                    f"{type(exc).__name__}: {exc}",
                ) from exc

            command = [executable, "-imol", str(mol_path), "-opdbqt",
                       "-h", "--partialcharge", "gasteiger", "-O", str(out_path)]
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True, timeout=120,
                    check=False,
                )
            except Exception as exc:
                raise ConversionError(
                    "OpenBabel could not be started on this computer.",
                    f"{type(exc).__name__}: {exc}",
                ) from exc

            if completed.returncode != 0 or not out_path.is_file():
                raise ConversionError(
                    "OpenBabel could not convert this ligand into PDBQT format.",
                    f"exit={completed.returncode} stderr={completed.stderr[:500]}",
                )
            text = out_path.read_text(encoding="utf-8", errors="replace")
            if not text.strip():                          # pragma: no cover
                raise ConversionError(
                    "OpenBabel produced an empty PDBQT file for this ligand.",
                    f"stdout={completed.stdout[:500]}",
                )
            return text


#: Every converter EnzyDock knows about, in registration order.
_CONVERTER_TYPES: tuple[type[PDBQTConverter], ...] = (
    MeekoConverter,
    OpenBabelConverter,
)


def available_converters() -> list[PDBQTConverter]:
    """Return usable converter instances, best one first."""
    found: list[PDBQTConverter] = []
    for cls in _CONVERTER_TYPES:
        try:
            instance = cls()
        except Exception as exc:                          # pragma: no cover
            log.debug("cannot build converter %s: %s", cls.__name__, exc)
            continue
        try:
            if instance.is_available():
                found.append(instance)
        except Exception as exc:                          # pragma: no cover
            log.debug("availability check failed for %s: %s", cls.name, exc)
    found.sort(key=lambda c: c.priority)
    return found


def get_converter(preferred: str | None = None) -> PDBQTConverter:
    """Return a usable converter.

    Parameters
    ----------
    preferred:
        Optional backend name (``"meeko"``, ``"OpenBabel"``).  When it is not
        installed an informative :class:`ConversionError` is raised instead of
        silently falling back.

    Raises
    ------
    ConversionError
        When no backend is available (or the requested one is missing).
    """
    usable = available_converters()
    if preferred:
        for converter in usable:
            if converter.name.lower() == preferred.lower():
                return converter
        raise ConversionError(
            f"The requested PDBQT writer '{preferred}' is not available. "
            "Please install it, or leave the writer on 'auto'.",
            f"available: {[c.name for c in usable]}",
        )
    if not usable:
        raise ConversionError(
            "No PDBQT writer is available on this computer, so ligands cannot "
            "be prepared for docking. Please install meeko "
            "(pip install meeko) and restart EnzyDock.",
            "no PDBQT converter is importable",
        )
    return usable[0]


def write_pdbqt(
    mol: Chem.Mol,
    destination: str | Path,
    converter: PDBQTConverter | str | None = None,
) -> Path:
    """Write ``mol`` as PDBQT to ``destination`` and return the path.

    Parameters
    ----------
    mol:
        RDKit molecule **with a 3D conformer**.
    destination:
        Target file.  Parent directories are created as needed.
    converter:
        ``None`` for automatic selection, a name, or a ready-made instance.
    """
    if not isinstance(converter, PDBQTConverter):
        converter = get_converter(converter)
    return converter.write(mol, destination)


def count_torsions(pdbqt_text: str) -> int:
    """Number of rotatable bonds declared in a PDBQT string (``TORSDOF``).

    Returns ``0`` when the record is missing, which is correct for rigid
    molecules.
    """
    for line in pdbqt_text.splitlines():
        if line.startswith("TORSDOF"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):              # pragma: no cover
                return 0
    return 0


def count_branch_records(pdbqt_text: str) -> int:
    """Number of ``BRANCH`` records - sanity check against ``TORSDOF``."""
    return sum(1 for line in pdbqt_text.splitlines() if line.startswith("BRANCH"))


#: Type of the optional progress callback used by :mod:`app.chemistry.preparation`.
ProgressCallback = Callable[[int, str], None]


__all__ = [
    "ConversionError",
    "PDBQTConverter",
    "MeekoConverter",
    "OpenBabelConverter",
    "available_converters",
    "get_converter",
    "write_pdbqt",
    "count_torsions",
    "count_branch_records",
]
