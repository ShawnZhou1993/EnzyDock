"""Read-only structural analysis of macromolecule files.

This module answers the question *"what is inside this file?"* -- it never
modifies anything.  Actual receptor preparation (protonation, PDBQT writing)
is implemented in :mod:`app.chemistry.preparation` (Step 4).

Supported input
---------------
``.pdb``
    Parsed natively (fixed column format).
``.cif`` / ``.mmcif``
    Parsed through a compact ``_atom_site`` loop reader.

Anything else raises :class:`StructureParseError` with a message that is safe
to show to a non-specialist user.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

from ..utils.logger import get_logger

log = get_logger(__name__)

#: Residue names treated as water.
WATER_RESIDUES = frozenset({"HOH", "WAT", "DOD", "H2O", "TIP", "SOL"})

#: Element symbols treated as metal ions / cofactors worth reporting.
METAL_ELEMENTS = frozenset({
    "MG", "MN", "ZN", "FE", "FE2", "CA", "NA", "K", "CU", "CO", "NI",
    "CD", "HG", "PB", "MO", "W", "PT", "AG", "AU", "BA", "SR", "CS", "LI",
})

SUPPORTED_SUFFIXES = (".pdb", ".ent", ".cif", ".mmcif")


class StructureParseError(ValueError):
    """Raised when a structure file cannot be understood."""


@dataclass
class AtomRecord:
    """Minimal atom information extracted from a structure file."""

    record: str          # "ATOM" or "HETATM"
    name: str
    residue: str
    chain: str
    resseq: int
    element: str
    x: float
    y: float
    z: float
    altloc: str = ""
    icode: str = ""
    occupancy: float = 1.0
    temp_factor: float = 0.0


@dataclass
class StructureSummary:
    """Human readable statistics about a macromolecular structure file."""

    path: Path
    file_format: str
    chains: list[str] = field(default_factory=list)
    residue_count: int = 0
    atom_count: int = 0
    heavy_atom_count: int = 0
    water_count: int = 0
    het_residues: list[str] = field(default_factory=list)
    metals: list[str] = field(default_factory=list)
    has_hydrogens: bool = False
    bounding_box: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None
    warnings: list[str] = field(default_factory=list)

    # -- presentation helpers ---------------------------------------------
    @property
    def chain_count(self) -> int:
        return len(self.chains)

    @property
    def chain_text(self) -> str:
        return ", ".join(self.chains) if self.chains else "-"

    @property
    def center(self) -> tuple[float, float, float] | None:
        """Geometric centre of the structure, handy as a box starting point."""
        if self.bounding_box is None:
            return None
        (x0, y0, z0), (x1, y1, z1) = self.bounding_box
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)

    @property
    def size(self) -> tuple[float, float, float] | None:
        if self.bounding_box is None:
            return None
        (x0, y0, z0), (x1, y1, z1) = self.bounding_box
        return (x1 - x0, y1 - y0, z1 - z0)

    def to_rows(self) -> list[tuple[str, str]]:
        """``(label, value)`` pairs for display in the protein panel."""
        rows = [
            ("File", self.path.name),
            ("Format", self.file_format.upper()),
            ("Chains", self.chain_text),
            ("Residues", f"{self.residue_count:,}"),
            ("Atoms", f"{self.atom_count:,}"),
            ("Waters", f"{self.water_count:,}"),
            ("HETATM residues", ", ".join(self.het_residues) if self.het_residues else "none"),
            ("Metals / ions", ", ".join(self.metals) if self.metals else "none"),
            ("Hydrogens present", "yes" if self.has_hydrogens else "no"),
        ]
        if self.center:
            cx, cy, cz = self.center
            rows.append(("Centre (A)", f"{cx:.2f}, {cy:.2f}, {cz:.2f}"))
        return rows


def read_atoms(path: Path | str) -> list[AtomRecord]:
    """Parse ``path`` and return its atom records.

    Raises
    ------
    StructureParseError
        With a user readable message when the file is missing, empty, of an
        unsupported type, or contains no ``ATOM``/``HETATM`` records.
    """
    path = Path(path)

    if not path.is_file():
        raise StructureParseError(f"The file '{path.name}' does not exist.")
    if path.stat().st_size == 0:
        raise StructureParseError(f"The file '{path.name}' is empty.")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise StructureParseError(
            f"'{path.suffix}' files are not supported yet. "
            "Please provide a .pdb, .cif or .mmcif file."
        )

    try:
        if suffix in (".cif", ".mmcif"):
            atoms = list(_iter_mmcif_atoms(path))
        else:
            atoms = list(_iter_pdb_atoms(path))
    except StructureParseError:
        raise
    except OSError as exc:
        raise StructureParseError(
            f"The structure file could not be read ({exc.strerror or exc})."
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise StructureParseError(
            "The structure file could not be parsed. It may be corrupted or "
            "truncated."
        ) from exc

    if not atoms:
        raise StructureParseError(
            "No ATOM or HETATM records were found in this file. "
            "Please check that it really contains a molecular structure."
        )
    log.debug("parsed %d atoms from %s", len(atoms), path.name)
    return atoms


def summarize_structure(path: Path | str) -> StructureSummary:
    """Build a :class:`StructureSummary` for ``path``."""
    path = Path(path)
    atoms = read_atoms(path)
    return summarize_atoms(atoms, path=path, file_format=_format_of(path))


def summarize_atoms(
    atoms: Sequence[AtomRecord],
    path: Path | str,
    file_format: str = "pdb",
) -> StructureSummary:
    """Build a :class:`StructureSummary` from already parsed atoms."""
    chains: list[str] = []
    residues: set[tuple[str, int, str]] = set()
    het_residues: list[str] = []
    metals: list[str] = []
    water = 0
    heavy = 0
    hydrogens = 0

    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []

    for atom in atoms:
        if atom.chain and atom.chain not in chains:
            chains.append(atom.chain)

        residues.add((atom.chain, atom.resseq, atom.residue))
        xs.append(atom.x)
        ys.append(atom.y)
        zs.append(atom.z)

        element = (atom.element or _guess_element(atom.name)).upper()
        if element in ("H", "D"):
            hydrogens += 1
        else:
            heavy += 1

        if atom.record == "HETATM":
            if atom.residue.upper() in WATER_RESIDUES:
                water += 1
            else:
                if atom.residue not in het_residues:
                    het_residues.append(atom.residue)
                if element in METAL_ELEMENTS and element not in metals:
                    metals.append(element)

    ordered_chains = sorted(chains, key=lambda c: (len(c), c))
    box = None
    if xs:
        box = ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))

    warnings: list[str] = []
    if hydrogens == 0:
        warnings.append(
            "No hydrogen atoms were found; hydrogens will be added during preparation."
        )
    if not ordered_chains:
        warnings.append("No chain identifiers found in the file.")

    return StructureSummary(
        path=Path(path),
        file_format=file_format,
        chains=ordered_chains,
        residue_count=len(residues),
        atom_count=len(atoms),
        heavy_atom_count=heavy,
        water_count=water,
        het_residues=het_residues,
        metals=metals,
        has_hydrogens=hydrogens > 0,
        bounding_box=box,
        warnings=warnings,
    )


def _format_of(path: Path) -> str:
    suffix = path.suffix.lower()
    return "mmcif" if suffix in (".cif", ".mmcif") else "pdb"


def _guess_element(atom_name: str) -> str:
    """Guess an element symbol from a PDB atom name (``" CA "`` -> ``"C"``)."""
    name = atom_name.strip()
    if not name:
        return ""
    # PDB columns: 13-14 hold the element, right justified; when absent we
    # fall back to the leading alphabetic characters of the atom name.
    if len(name) >= 2 and name[0].isdigit():
        name = name[1:]
    return "".join(itertools.takewhile(str.isalpha, name))[:2].upper()


# ---------------------------------------------------------------------------
# PDB
# ---------------------------------------------------------------------------
def _iter_pdb_atoms(path: Path) -> Iterator[AtomRecord]:
    """Yield :class:`AtomRecord` for every ATOM/HETATM line of a PDB file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except UnicodeDecodeError as exc:  # pragma: no cover - replaced above
        raise StructureParseError("The PDB file is not valid UTF-8 text.") from exc

    for line in text.splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        if len(line) < 54:
            # Truncated coordinate record: skip rather than crash.
            continue
        try:
            yield AtomRecord(
                record=line[0:6].strip(),
                name=line[12:16],
                residue=line[17:20].strip(),
                chain=line[21].strip(),
                resseq=int(line[22:26]) if line[22:26].strip() else 0,
                element=line[76:78].strip().upper(),
                x=float(line[30:38]),
                y=float(line[38:46]),
                z=float(line[46:54]),
                altloc=line[16],
                icode=line[26] if len(line) > 26 else "",
                occupancy=_optional_float(line[54:60], 1.0),
                temp_factor=_optional_float(line[60:66], 0.0),
            )
        except ValueError:
            # Malformed coordinate field: ignore this atom.
            continue


# ---------------------------------------------------------------------------
# mmCIF
# ---------------------------------------------------------------------------
def _iter_mmcif_atoms(path: Path) -> Iterator[AtomRecord]:
    """Yield :class:`AtomRecord` from an mmCIF ``_atom_site`` loop.

    Only the standard ``_atom_site`` tags are required; missing optional
    columns (``auth_asym_id``, ``type_symbol``) degrade gracefully.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines()]
    total = len(lines)

    index = 0
    while index < total:
        if not lines[index].startswith("_atom_site."):
            index += 1
            continue

        # Collect the contiguous run of _atom_site.* tags (the loop header).
        header: list[str] = []
        while index < total and lines[index].startswith("_atom_site."):
            header.append(lines[index].lstrip("_").split(".", 1)[1])
            index += 1
        if not header:
            continue

        # Immediately following lines are the data rows of that same loop.
        while index < total:
            line = lines[index]
            if not line or line.startswith(("#", "loop_", "data_", "_")):
                break
            tokens = _tokenize_cif_line(line)
            if len(tokens) != len(header):
                break                      # the loop ended
            atom = _mmcif_row_to_atom(dict(zip(header, tokens)))
            if atom is not None:
                yield atom
            index += 1
        return                              # only one _atom_site loop is expected


def _mmcif_row_to_atom(row: dict[str, str]) -> AtomRecord | None:
    """Convert one parsed mmCIF data row into an :class:`AtomRecord`.

    Returns ``None`` when coordinates are missing or malformed so that a
    single bad row cannot abort the whole parse.
    """
    try:
        return AtomRecord(
            record=(row.get("group_PDB") or "ATOM").strip().upper(),
            name=row.get("label_atom_id", ""),
            residue=row.get("label_comp_id", ""),
            chain=(row.get("auth_asym_id") or row.get("label_asym_id") or "").strip(),
            resseq=int(float(row.get("auth_seq_id") or 0)),
            element=(row.get("type_symbol") or "").strip().upper(),
            x=float(row["Cartn_x"]),
            y=float(row["Cartn_y"]),
            z=float(row["Cartn_z"]),
            altloc=_cif_text(row.get("label_alt_id")),
            icode=_cif_text(row.get("pdbx_PDB_ins_code")),
            occupancy=_optional_float(row.get("occupancy"), 1.0),
            temp_factor=_optional_float(row.get("B_iso_or_equiv"), 0.0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _cif_text(value: str | None) -> str:
    """Normalise an mmCIF text field.

    mmCIF uses ``.`` for "not applicable" and ``?`` for "unknown"; both must
    become an empty string, otherwise they end up as literal characters in the
    altLoc / insertion-code columns of any PDB written later.
    """
    if not value:
        return ""
    stripped = value.strip()
    return "" if stripped in (".", "?") else stripped


def _optional_float(value: str | None, default: float) -> float:
    """Parse ``value`` as a float, falling back to ``default``.

    mmCIF uses ``.`` and ``?`` for "not applicable", both of which must not
    abort the parse.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tokenize_cif_line(line: str) -> list[str]:
    """Split an mmCIF data line respecting single and double quotes."""
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in line:
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens
