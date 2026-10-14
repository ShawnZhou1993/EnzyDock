"""Copying docking results to a user-chosen location.

All functions take a :class:`~app.docking.docking_config.DockingResult` and a
destination, copy the files that belong to it and return the list of written
paths, so the GUI can tell the user exactly what landed where.

Nothing here ever modifies the workspace: exports are copies, and the run
workspace stays the single source of truth.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..docking.docking_config import DockingResult, PoseResult
from ..docking.result_parser import write_summary_csv
from ..utils.logger import get_logger
from ..utils.paths import ensure_dir, iter_files
from .report import write_report

log = get_logger(__name__)

#: Preferred order of the structure formats written for a pose.
FORMAT_PRIORITY: tuple[str, ...] = (".pdb", ".pdbqt")


class ExportError(RuntimeError):
    """Raised when an export cannot be completed.

    ``message`` is written for a bench scientist, ``details`` carries the
    technical cause for the "Show Technical Details" dialog.
    """

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details


# ------------------------------------------------------------------- helpers
def _stem(path: object) -> str:
    """File stem of ``path`` (``receptor.pdbqt`` -> ``receptor``)."""
    return Path(str(path)).stem if path is not None else ""


def default_export_stem(result: DockingResult) -> str:
    """A filesystem-safe base name for exports of ``result``.

    The workspace folder is named ``<timestamp>_<protein>``, so it identifies
    the run uniquely and reads well in Explorer; docking a second ligand
    against the same protein still produces distinct names.
    """
    if result.workspace is not None:
        name = Path(result.workspace).name
        if name:
            return name
    return "enzydock_results"


def _results_dir(result: DockingResult) -> Path | None:
    """``<workspace>/results`` when the workspace is known."""
    return Path(result.workspace) / "results" if result.workspace else None


def resolve_pose_files(result: DockingResult, pose: PoseResult) -> list[Path]:
    """Existing structure files of one pose, PDB first, PDBQT second.

    Raises
    ------
    ExportError
        When no structure file can be found for the pose (for example when the
        workspace was deleted or moved after the run).
    """
    candidates: list[Path] = []
    if pose.pose_file is not None:
        candidates.append(Path(pose.pose_file))
    results = _results_dir(result)
    if results is not None:
        candidates.append(results / f"pose_{pose.pose}.pdb")
        candidates.append(results / f"pose_{pose.pose}.pdbqt")

    by_suffix: dict[str, Path] = {}
    for candidate in candidates:
        if candidate.is_file():
            by_suffix.setdefault(candidate.suffix.lower(), candidate)
    if not by_suffix:
        raise ExportError(
            f"No structure file is available for pose {pose.pose}.",
            "Looked for " + ", ".join(str(c) for c in candidates),
        )

    ordered = [by_suffix[suffix] for suffix in FORMAT_PRIORITY if suffix in by_suffix]
    ordered += [path for suffix, path in by_suffix.items()
                if suffix not in FORMAT_PRIORITY]
    return ordered


def pose_files(result: DockingResult) -> dict[int, list[Path]]:
    """Map ``pose number -> existing structure files`` for every pose.

    The workspace ``results`` directory is scanned first (it is complete even
    when the in-memory result was restored without pose paths); poses known
    only through :attr:`PoseResult.pose_file` are added afterwards.
    """
    found: dict[int, list[Path]] = {}
    results = _results_dir(result)
    if results is not None:
        for path in iter_files(results, FORMAT_PRIORITY):
            number = _pose_number(path)
            if number is not None:
                found.setdefault(number, []).append(path)

    for pose in result.poses:
        if pose.pose_file is None:
            continue
        path = Path(pose.pose_file)
        if not path.is_file():
            continue
        bucket = found.setdefault(pose.pose, [])
        if path.resolve() not in {p.resolve() for p in bucket}:
            bucket.append(path)

    for files in found.values():
        files.sort(key=_format_rank)
    return dict(sorted(found.items()))


def _format_rank(path: Path) -> int:
    """Sort key putting ``.pdb`` before ``.pdbqt`` before anything else."""
    suffix = path.suffix.lower()
    return FORMAT_PRIORITY.index(suffix) if suffix in FORMAT_PRIORITY else len(FORMAT_PRIORITY)


def _pose_number(path: Path) -> int | None:
    """Extract the pose number from ``pose_3.pdb`` -> ``3``."""
    stem = path.stem
    _, _, tail = stem.partition("_")
    return int(tail) if tail.isdigit() else None


# ------------------------------------------------------------------ exporters
def export_best_pose(result: DockingResult, target: str | Path) -> list[Path]:
    """Write the lowest-energy pose to ``target`` (a file path).

    The companion format is written next to it: asking for ``best.pdb`` also
    produces ``best.pdbqt`` when that file exists, because the PDBQT carries
    the AutoDock atom types other tools expect.

    Returns the list of written files.
    """
    best = result.best_pose
    if best is None:
        raise ExportError(
            "There is no pose to export yet. Please run a docking first.",
            "DockingResult.poses is empty.",
        )

    sources = resolve_pose_files(result, best)
    destination = Path(target)
    ensure_dir(destination.parent)

    written: list[Path] = []
    shutil.copy2(sources[0], destination)
    written.append(destination)
    for extra in sources[1:]:
        companion = destination.with_suffix(extra.suffix)
        shutil.copy2(extra, companion)
        written.append(companion)

    log.info("exported best pose %d (%d file(s)) -> %s",
             best.pose, len(written), destination)
    return written


def export_all_poses(
    result: DockingResult,
    directory: str | Path,
    *,
    with_table: bool = True,
    with_report: bool = True,
) -> list[Path]:
    """Copy every pose (plus table and report) into ``directory``.

    The folder is created when missing.  Existing files with the same name are
    overwritten - the user picked the folder explicitly through the dialog.
    """
    poses = pose_files(result)
    if not poses:
        raise ExportError(
            "No pose files could be found for this run.",
            f"workspace={result.workspace}, poses={len(result.poses)}",
        )

    target = ensure_dir(directory)
    written: list[Path] = []
    for files in poses.values():
        for source in files:
            destination = target / source.name
            shutil.copy2(source, destination)
            written.append(destination)

    if with_table:
        written.append(write_summary_csv(result, target / "summary.csv"))
    if with_report:
        written.append(write_report(result, target / "report.md"))

    log.info("exported %d pose(s) + %d extra file(s) -> %s",
             len(poses), len(written) - sum(len(f) for f in poses.values()), target)
    return written


def export_csv(result: DockingResult, target: str | Path) -> Path:
    """Write the pose table as CSV and return the path."""
    return write_summary_csv(result, target)


def export_report(result: DockingResult, target: str | Path) -> Path:
    """Write the Markdown report and return the path."""
    return write_report(result, target)


__all__ = [
    "FORMAT_PRIORITY",
    "ExportError",
    "default_export_stem",
    "export_all_poses",
    "export_best_pose",
    "export_csv",
    "export_report",
    "pose_files",
    "resolve_pose_files",
]
