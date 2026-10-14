"""Ligand / substrate handling.

Covers the *analysis* half of the ligand workflow:

* parse a SMILES string or read ``.sdf`` / ``.mol`` / ``.mol2`` / ``.pdb``,
* report formula, molecular weight, atom count and rotatable bonds,
* report problems in language a bench scientist understands.

The 3D embedding + optimisation + PDBQT conversion steps belong to
:mod:`app.chemistry.preparation` (Step 3) and :mod:`app.chemistry.conversion`
(Step 3); this module stays strictly read-only and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, rdMolDescriptors

from ..utils.logger import get_logger

log = get_logger(__name__)

# RDKit is quite chatty about sanitisation problems; keep the log readable.
RDLogger.DisableLog("rdApp.*")

SUPPORTED_LIGAND_SUFFIXES = (".sdf", ".sd", ".mol", ".mol2", ".pdb", ".smi")


class LigandError(ValueError):
    """Raised when a ligand cannot be parsed or described.

    ``message`` is user facing, ``details`` holds the technical cause.
    """

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details


@dataclass
class LigandSummary:
    """Descriptive statistics of a small molecule."""

    name: str = "ligand"
    source: str = "SMILES"            # "SMILES" or the original file name
    smiles: str = ""
    formula: str = ""
    molecular_weight: float = 0.0
    atom_count: int = 0
    heavy_atom_count: int = 0
    rotatable_bonds: int = 0
    ring_count: int = 0
    hbd: int = 0                      # hydrogen bond donors
    hba: int = 0                      # hydrogen bond acceptors
    logp: float = 0.0
    charge: int = 0
    has_3d: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_rows(self) -> list[tuple[str, str]]:
        """``(label, value)`` pairs for display in the ligand panel."""
        return [
            ("Source", self.source),
            ("Molecular formula", self.formula or "-"),
            ("Molecular weight", f"{self.molecular_weight:.2f} g/mol" if self.molecular_weight else "-"),
            ("Atom count", f"{self.atom_count:,}" if self.atom_count else "-"),
            ("Heavy atoms", f"{self.heavy_atom_count:,}" if self.heavy_atom_count else "-"),
            ("Rotatable bonds", str(self.rotatable_bonds) if self.atom_count else "-"),
            ("Rings", str(self.ring_count) if self.atom_count else "-"),
            ("H-bond donors / acceptors",
             f"{self.hbd} / {self.hba}" if self.atom_count else "-"),
            ("cLogP", f"{self.logp:.2f}" if self.atom_count else "-"),
            ("Formal charge", str(self.charge) if self.atom_count else "-"),
            ("3D coordinates", "yes" if self.has_3d else "no (2D only)"),
        ]


def molecule_from_smiles(smiles: str) -> Chem.Mol:
    """Parse a SMILES string into an RDKit molecule.

    A trailing label is tolerated: ``.smi`` files and copy-pasted lines often
    carry a name after the SMILES (``CC(=O)Oc1ccccc1C(=O)O\\taspirin``), and a
    SMILES string itself never contains whitespace, so the first token is the
    molecule and the rest is a title.

    Raises
    ------
    LigandError
        When the string is empty or cannot be interpreted as a molecule.
    """
    if not smiles or not smiles.strip():
        raise LigandError(
            "Please enter a SMILES string first, for example: CC(=O)Oc1ccccc1C(=O)O",
            "Empty SMILES input.",
        )

    text = smiles.strip().split()[0]
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        raise LigandError(
            "This SMILES string could not be interpreted as a molecule. "
            "Please check for unbalanced brackets, an impossible valence, or a typo.",
            f"RDKit MolFromSmiles returned None for: {text!r}",
        )
    log.debug("SMILES parsed: %s -> %d atoms", text, mol.GetNumAtoms())
    return mol


def molecule_from_file(path: Path | str) -> Chem.Mol:
    """Read the **first** molecule from a structure file.

    Supported: ``.sdf``/``.sd``, ``.mol``, ``.mol2``, ``.pdb``.

    Raises
    ------
    LigandError
        When the extension is unsupported, the file is unreadable, or it does
        not contain a valid molecule.
    """
    path = Path(path)

    if not path.is_file():
        raise LigandError(f"The file '{path.name}' does not exist.", f"{path}")
    if path.stat().st_size == 0:
        raise LigandError(f"The file '{path.name}' is empty.", f"{path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_LIGAND_SUFFIXES:
        raise LigandError(
            f"'{path.suffix}' ligand files are not supported. "
            "Please use .sdf, .mol, .mol2 or .pdb.",
            f"{path}",
        )

    mol: Chem.Mol | None = None
    try:
        if suffix in (".sdf", ".sd"):
            supplier = Chem.SDMolSupplier(str(path), removeHs=False, sanitize=True)
            for candidate in supplier:
                if candidate is not None:
                    mol = candidate
                    break
            if mol is None:
                raise LigandError(
                    "No valid molecule could be read from this SDF file.",
                    f"SDMolSupplier returned no valid molecule for {path}",
                )
        elif suffix == ".mol":
            mol = Chem.MolFromMolFile(str(path), removeHs=False, sanitize=True)
        elif suffix == ".mol2":
            mol = Chem.MolFromMol2File(str(path), removeHs=False, sanitize=True)
        elif suffix == ".pdb":
            mol = Chem.MolFromPDBFile(str(path), removeHs=False, sanitize=True)
        elif suffix == ".smi":
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            return molecule_from_smiles(text.split()[0] if text.split() else "")
    except LigandError:
        raise
    except Exception as exc:
        raise LigandError(
            f"The ligand file '{path.name}' could not be read.",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    if mol is None:
        raise LigandError(
            f"The ligand file '{path.name}' does not contain a valid molecule. "
            "It may be corrupted, or written in a dialect RDKit cannot read.",
            f"RDKit returned None while reading {path}",
        )
    log.debug("ligand file parsed: %s -> %d atoms", path.name, mol.GetNumAtoms())
    return mol


def summarize_molecule(
    mol: Chem.Mol,
    name: str = "ligand",
    source: str = "SMILES",
    smiles: str = "",
) -> LigandSummary:
    """Compute a :class:`LigandSummary` from an RDKit molecule."""
    if mol is None:
        raise LigandError("No molecule to describe.", "summarize_molecule got None")

    warnings: list[str] = []
    heavy = mol.GetNumHeavyAtoms()
    if heavy == 0:
        raise LigandError(
            "The molecule contains no heavy atoms - docking is not possible.",
            "GetNumHeavyAtoms() == 0",
        )
    if heavy > 150:
        warnings.append(
            "This molecule is very large; docking may be slow or unreliable."
        )

    try:
        formula = rdMolDescriptors.CalcMolFormula(mol)
    except Exception:                                # pragma: no cover
        formula = ""

    has_3d = bool(mol.GetNumConformers()) and mol.GetConformer().Is3D()

    try:
        mw = float(Descriptors.MolWt(mol))
        logp = float(Descriptors.MolLogP(mol))
        hbd = int(rdMolDescriptors.CalcNumHBD(mol))
        hba = int(rdMolDescriptors.CalcNumHBA(mol))
        rotatable = int(rdMolDescriptors.CalcNumRotatableBonds(mol))
        rings = int(rdMolDescriptors.CalcNumRings(mol))
    except Exception as exc:                          # pragma: no cover
        raise LigandError(
            "The molecule could not be analysed chemically.",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    if rotatable > 15:
        warnings.append(
            f"{rotatable} rotatable bonds: the conformational search will be "
            "expensive, consider a larger exhaustiveness."
        )

    return LigandSummary(
        name=name or "ligand",
        source=source,
        smiles=smiles or _safe_smiles(mol),
        formula=formula,
        molecular_weight=mw,
        atom_count=mol.GetNumAtoms(),
        heavy_atom_count=heavy,
        rotatable_bonds=rotatable,
        ring_count=rings,
        hbd=hbd,
        hba=hba,
        logp=logp,
        charge=Chem.GetFormalCharge(mol),
        has_3d=has_3d,
        warnings=warnings,
    )


def _safe_smiles(mol: Chem.Mol) -> str:
    """Return canonical SMILES, or an empty string on failure."""
    try:
        return Chem.MolToSmiles(mol)
    except Exception:                                 # pragma: no cover
        return ""


def ligand_from_smiles(smiles: str) -> tuple[Chem.Mol, LigandSummary]:
    """Convenience helper: parse then describe a SMILES string."""
    mol = molecule_from_smiles(smiles)
    summary = summarize_molecule(mol, name=_name_from_smiles(smiles), source="SMILES",
                                 smiles=smiles)
    return mol, summary


def ligand_from_file(path: Path | str) -> tuple[Chem.Mol, LigandSummary]:
    """Convenience helper: read then describe a ligand file."""
    path = Path(path)
    mol = molecule_from_file(path)
    summary = summarize_molecule(mol, name=path.stem, source=path.name)
    return mol, summary


def _name_from_smiles(smiles: str) -> str:
    """Short display name derived from a SMILES string."""
    token = smiles.strip().split()[0] if smiles.strip() else "ligand"
    return token[:24] if len(token) > 24 else token


def to_display(obj: Any) -> str:                       # pragma: no cover
    """Best effort string conversion used by the GUI table views."""
    return "" if obj is None else str(obj)
