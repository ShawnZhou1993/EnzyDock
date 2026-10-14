"""Parsing of AutoDock Vina output into :class:`PoseResult` objects.

The *authoritative* source of binding affinities is the ``REMARK VINA RESULT``
line that Vina writes above every model in its output PDBQT::

    MODEL 1
    REMARK VINA RESULT:    -4.516      0.000      0.000
    ...
    ENDMDL

The human readable table Vina prints to stdout is parsed as well, but only as
a cross-check / fallback — stdout formatting differs between Vina, QVina2 and
Smina, while the PDBQT remark does not.

Besides parsing, this module splits the multi-model output into one file per
pose (``pose_1.pdb``, ...) and writes ``summary.csv``.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterator

from ..utils.logger import get_logger
from .docking_config import DockingResult, PoseResult

log = get_logger(__name__)

#: ``REMARK VINA RESULT:   affinity   rmsd_lb   rmsd_ub``
VINA_RESULT_RE = re.compile(
    r"^REMARK\s+VINA\s+RESULT:\s*"
    r"(?P<affinity>-?\d+(?:\.\d+)?)\s+"
    r"(?P<rmsd_lb>-?\d+(?:\.\d+)?)\s+"
    r"(?P<rmsd_ub>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

#: Generic score remark used by Smina / QVina2 (``REMARK  minimizedAffinity``).
MINIMIZED_AFFINITY_RE = re.compile(
    r"^REMARK\s+minimizedAffinity\s+(?P<affinity>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

MODEL_RE = re.compile(r"^MODEL\s+(?P<number>\d+)", re.IGNORECASE)

#: Table rows printed by Vina 1.2.x, e.g. ``"   1       -4.516          0          0"``.
LOG_ROW_RE = re.compile(
    r"^\s*(?P<pose>\d+)\s+(?P<affinity>-?\d+(?:\.\d+)?)\s+"
    r"(?P<rmsd_lb>\d+(?:\.\d+)?)\s+(?P<rmsd_ub>\d+(?:\.\d+)?)\s*$"
)

#: AutoDock atom type -> chemical element symbol.
AD_TYPE_TO_ELEMENT: dict[str, str] = {
    "C": "C", "A": "C",
    "N": "N", "NA": "N", "NS": "N",
    "O": "O", "OA": "O", "OS": "O",
    "S": "S", "SA": "S",
    "H": "H", "HD": "H",
    "F": "F", "CL": "CL", "BR": "BR", "I": "I",
    "MG": "MG", "MN": "MN", "ZN": "ZN", "FE": "FE", "CA": "CA",
    "CU": "CU", "NA_": "NA", "K": "K", "NI": "NI", "CO": "CO", "CD": "CD",
}


def read_output_text(path: str | Path) -> str:
    """Read an engine output file tolerantly.

    Vina writes a stray NUL byte (and Windows line endings); both are
    normalised here so that downstream code deals with clean text.
    """
    raw = Path(path).read_bytes()
    return raw.replace(b"\x00", b"").decode("utf-8", errors="replace")


# --------------------------------------------------------------------- parsing
def parse_pose_energies(text: str) -> list[tuple[float, float, float]]:
    """Extract ``(affinity, rmsd_lb, rmsd_ub)`` triples from ``VINA RESULT`` rows."""
    triples: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        match = VINA_RESULT_RE.match(line.strip())
        if match:
            triples.append((
                float(match.group("affinity")),
                float(match.group("rmsd_lb")),
                float(match.group("rmsd_ub")),
            ))
    return triples


def parse_log_table(text: str) -> list[PoseResult]:
    """Parse the score table Vina prints on stdout.

    Returns an empty list when the text contains no table (for example when
    the run failed), which lets callers fall through to the PDBQT remarks.
    """
    rows: list[PoseResult] = []
    for line in text.splitlines():
        match = LOG_ROW_RE.match(line.rstrip())
        if match:
            rows.append(PoseResult(
                pose=int(match.group("pose")),
                affinity=float(match.group("affinity")),
                rmsd_lb=float(match.group("rmsd_lb")),
                rmsd_ub=float(match.group("rmsd_ub")),
            ))
    # Only trust the table when it is numbered 1..n without gaps; a stray
    # numeric line elsewhere in the log must not be mistaken for a pose.
    if rows and [row.pose for row in rows] == list(range(1, len(rows) + 1)):
        return rows
    return []


def iter_models(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(model_number, model_text)`` for every ``MODEL`` block."""
    current_number = 0
    buffer: list[str] = []
    inside = False
    for line in text.splitlines():
        match = MODEL_RE.match(line.strip())
        if match:
            current_number = int(match.group("number"))
            buffer = []
            inside = True
            continue
        if line.strip() == "ENDMDL":
            if inside:
                yield current_number, "\n".join(buffer)
            inside = False
            buffer = []
            continue
        if inside:
            buffer.append(line)
    if inside and buffer:                              # pragma: no cover
        yield current_number, "\n".join(buffer)


def parse_poses(output_path: str | Path) -> list[PoseResult]:
    """Parse every pose of a multi-model PDBQT file.

    Poses are numbered by their position in the file (1-based) and sorted by
    affinity, because Vina already emits them best-first.
    """
    text = read_output_text(output_path)
    energies = parse_pose_energies(text)
    if not energies:
        log.warning("no 'REMARK VINA RESULT' lines in %s", output_path)
        return []

    poses: list[PoseResult] = []
    models = list(iter_models(text))
    for index, (affinity, rmsd_lb, rmsd_ub) in enumerate(energies, start=1):
        model_number = models[index - 1][0] if index - 1 < len(models) else index
        poses.append(PoseResult(
            pose=index,
            affinity=affinity,
            rmsd_lb=rmsd_lb,
            rmsd_ub=rmsd_ub,
            pose_file=None,
        ))
        log.debug("pose %d (model %d): %.3f kcal/mol", index, model_number, affinity)
    return poses


# --------------------------------------------------------------- file splitting
def pdbqt_to_pdb(model_text: str, title: str = "") -> str:
    """Convert one PDBQT model block into a plain PDB file body.

    Coordinates and residue information are kept; the AutoDock charge and
    type columns are replaced by a proper element column so that standard
    viewers and PyMOL read the file correctly.
    """
    lines: list[str] = []
    if title:
        lines.append(f"REMARK    {title}")
    serial = 0
    for raw in model_text.splitlines():
        if not raw.startswith(("ATOM", "HETATM")):
            continue                                    # ROOT/BRANCH/TORSDOF/...
        serial += 1
        record = raw[:66].rstrip()
        record = f"{record:<66s}"                       # pad to the tempFactor column
        ad_type = raw[77:79].strip().upper() if len(raw) >= 79 else ""
        element = AD_TYPE_TO_ELEMENT.get(ad_type, ad_type[:2] or "C")
        lines.append(f"{record}{'':>10s}{element:>2s}")
    lines.append("END")
    return "\n".join(lines) + "\n"


def split_poses(
    output_path: str | Path,
    results_dir: str | Path,
    *,
    prefix: str = "pose",
    write_pdb: bool = True,
) -> list[Path]:
    """Write one ``<prefix>_N.pdbqt`` (and ``.pdb``) file per pose.

    Returns the list of created files, PDB first when ``write_pdb`` is set, so
    that callers can hand ``files[0]`` straight to the 3D viewer.
    """
    text = read_output_text(output_path)
    directory = Path(results_dir)
    directory.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    for number, model_text in iter_models(text):
        pdbqt_path = directory / f"{prefix}_{number}.pdbqt"
        pdbqt_path.write_text(model_text.rstrip() + "\nENDMDL\n", encoding="utf-8")
        if write_pdb:
            pdb_path = directory / f"{prefix}_{number}.pdb"
            pdb_path.write_text(
                pdbqt_to_pdb(model_text, title=f"EnzyDock pose {number}"),
                encoding="utf-8",
            )
            created.append(pdb_path)
        created.append(pdbqt_path)
    log.info("split %d pose files into %s", len(created), directory)
    return created


def attach_pose_files(poses: list[PoseResult], results_dir: str | Path) -> None:
    """Point every pose at its ``pose_N.pdb`` when that file exists."""
    directory = Path(results_dir)
    for pose in poses:
        for suffix in (".pdb", ".pdbqt"):
            candidate = directory / f"pose_{pose.pose}{suffix}"
            if candidate.is_file():
                pose.pose_file = candidate
                break


# ------------------------------------------------------------------- reporting
def write_summary_csv(result: DockingResult, path: str | Path) -> Path:
    """Write the pose table to ``path`` and return it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["pose", "affinity_kcal_mol", "rmsd_lower_bound",
                         "rmsd_upper_bound"])
        for pose in result.poses:
            writer.writerow([pose.pose, f"{pose.affinity:.3f}",
                             f"{pose.rmsd_lb:.3f}", f"{pose.rmsd_ub:.3f}"])
    log.info("wrote summary CSV: %s", target)
    return target


def summarise(result: DockingResult) -> str:
    """A short human readable one-liner about a finished run."""
    best = result.best_affinity
    if best is None:
        return "Docking finished but produced no poses."
    return (f"{len(result.poses)} poses from {result.engine}; "
            f"best affinity {best:.2f} kcal/mol")


__all__ = [
    "AD_TYPE_TO_ELEMENT",
    "attach_pose_files",
    "iter_models",
    "parse_log_table",
    "parse_pose_energies",
    "parse_poses",
    "pdbqt_to_pdb",
    "read_output_text",
    "split_poses",
    "summarise",
    "write_summary_csv",
]
