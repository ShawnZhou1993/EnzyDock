"""End-to-end orchestration of one docking run.

This is the *only* place that knows the order of the steps.  The GUI hands a
:class:`DockingRequest` to a background worker and gets a
:class:`DockingResult` back; nothing here touches a widget.

The pipeline is deliberately forgiving: pressing **START DOCKING** without
having pressed *Prepare Protein* / *Prepare Ligand* first simply prepares both
as part of the run, so the user cannot reach a half-configured state.

Workspace layout produced per run::

    workspace/<timestamp>_<tag>/
        input/          original (copied, never modified) inputs
        prepared/       receptor.pdbqt, ligand.pdbqt, cleaned PDBs
        docking/        config.txt, command.txt, log.txt, output.pdbqt
        results/        pose_1.pdb ... pose_N.pdb, summary.csv, report.md
        visualization/  files handed to the 3D viewer
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..chemistry.preparation import (
    PreparationOptions,
    PreparedLigand,
    prepare_from_file,
    prepare_from_smiles,
)
from ..chemistry.receptor import (
    PreparedReceptor,
    ReceptorOptions,
    prepare_receptor,
)
from ..export.report import write_report
from ..utils.logger import get_logger
from ..utils.paths import create_workspace, safe_copy
from . import engines                     # noqa: F401  - registers the backends
from .base import DockingEngine, EngineError
from .docking_config import BoxConfig, DockingConfigError, DockingParameters, DockingResult
from .result_parser import attach_pose_files, split_poses, summarise, write_summary_csv

log = get_logger(__name__)

ProgressFn = Callable[[int, str], None]

#: Overall progress ranges reserved for each stage of the run.
STAGE_WORKSPACE = (0, 8)
STAGE_RECEPTOR = (8, 40)
STAGE_LIGAND = (40, 65)
STAGE_DOCKING = (65, 95)
STAGE_RESULTS = (95, 100)


@dataclass
class DockingRequest:
    """Everything a docking run needs, independent of any GUI state."""

    receptor_source: Path
    box: BoxConfig
    parameters: DockingParameters = field(default_factory=DockingParameters)

    #: Either a SMILES string or a ligand file, depending on ``ligand_kind``.
    ligand_source: str = ""
    ligand_kind: str = "smiles"          # "smiles" | "file"

    receptor_options: ReceptorOptions = field(default_factory=ReceptorOptions)
    ligand_options: PreparationOptions = field(default_factory=PreparationOptions)

    #: Results cached from an earlier "Prepare" press; reused when valid.
    prepared_receptor: PreparedReceptor | None = None
    prepared_ligand: PreparedLigand | None = None

    #: Backend created by the GUI so that *Cancel* can reach the process.
    engine: DockingEngine | None = None

    workspace: Path | None = None
    receptor_name: str = ""
    ligand_name: str = ""


def _sub_progress(
    progress: ProgressFn | None,
    span: tuple[int, int],
) -> ProgressFn | None:
    """Map a stage's own 0-100 progress onto a slice of the overall range."""
    if progress is None:
        return None
    low, high = span

    def forward(percent: int, message: str = "") -> None:
        clamped = max(0, min(100, int(percent)))
        progress(low + (high - low) * clamped // 100, message)

    return forward


def _report(progress: ProgressFn | None, percent: int, message: str) -> None:
    if progress is not None:
        progress(percent, message)
    log.info(message)


def _usable(path: Path | None) -> bool:
    """A prepared file counts as usable only when it exists and is non-empty."""
    return path is not None and Path(path).is_file() and Path(path).stat().st_size > 0


# ------------------------------------------------------------------- pipeline
def run_docking(
    request: DockingRequest,
    progress: ProgressFn | None = None,
) -> DockingResult:
    """Prepare, dock and parse - the complete EnzyDock chain.

    Raises
    ------
    DockingConfigError
        When the request is incomplete (no receptor, no ligand, ...).
    EngineError
        With a user readable message when docking itself fails.
    """
    started = time.perf_counter()

    # -- validation ---------------------------------------------------------
    if request.receptor_source is None or not Path(request.receptor_source).is_file():
        raise DockingConfigError(
            "No protein structure is loaded. Please select a protein file first."
        )
    if not request.ligand_source.strip():
        raise DockingConfigError(
            "No ligand is available. Please enter a SMILES string or load a "
            "ligand file first."
        )
    request.parameters.validate()

    # -- workspace ----------------------------------------------------------
    _report(progress, STAGE_WORKSPACE[0], "Creating the run workspace ...")
    tag = request.receptor_name or Path(request.receptor_source).stem or "run"
    workspace = Path(request.workspace) if request.workspace else create_workspace(tag)
    for sub in ("input", "prepared", "docking", "results", "visualization"):
        (workspace / sub).mkdir(parents=True, exist_ok=True)
    log.info("docking workspace: %s", workspace)

    receptor_copy = safe_copy(request.receptor_source, workspace / "input")
    _archive_ligand(request, workspace / "input")
    _report(progress, STAGE_WORKSPACE[1], f"Workspace: {workspace.name}")

    # -- receptor -----------------------------------------------------------
    receptor = request.prepared_receptor
    if not _usable(receptor.pdbqt_path if receptor else None):
        _report(progress, STAGE_RECEPTOR[0], "Preparing the protein ...")
        receptor = prepare_receptor(
            receptor_copy,
            output_dir=workspace / "prepared",
            options=request.receptor_options,
            progress=_sub_progress(progress, STAGE_RECEPTOR),
        )
    else:
        _report(progress, STAGE_RECEPTOR[1],
                "Re-using the receptor prepared earlier.")

    # -- ligand -------------------------------------------------------------
    ligand = request.prepared_ligand
    if not _usable(ligand.pdbqt_path if ligand else None):
        _report(progress, STAGE_LIGAND[0], "Preparing the ligand ...")
        if request.ligand_kind == "file":
            ligand = prepare_from_file(
                request.ligand_source,
                output_dir=workspace / "prepared",
                options=request.ligand_options,
                progress=_sub_progress(progress, STAGE_LIGAND),
            )
        else:
            ligand = prepare_from_smiles(
                request.ligand_source,
                output_dir=workspace / "prepared",
                options=request.ligand_options,
                progress=_sub_progress(progress, STAGE_LIGAND),
            )
    else:
        _report(progress, STAGE_LIGAND[1], "Re-using the ligand prepared earlier.")

    assert receptor is not None and ligand is not None   # noqa: S101 - set above
    if receptor.pdbqt_path is None or ligand.pdbqt_path is None:
        raise DockingConfigError(
            "Preparation did not produce the files AutoDock Vina needs "
            "(receptor.pdbqt and ligand.pdbqt). Please prepare both again."
        )

    # -- docking ------------------------------------------------------------
    _report(progress, STAGE_DOCKING[0], "Starting the docking engine ...")
    if request.engine is not None:
        engine = request.engine
        engine.reset()
    else:
        engine = DockingEngine.create(request.parameters.engine)
    available, reason = engine.is_available()
    if not available:
        raise EngineError(reason, f"engine: {request.parameters.engine}")

    log_lines: list[str] = []
    engine.log_line.connect(log_lines.append)

    result = engine.run(
        receptor=receptor.pdbqt_path,
        ligand=ligand.pdbqt_path,
        box=request.box,
        parameters=request.parameters,
        output_dir=workspace / "docking",
        progress=_sub_progress(progress, STAGE_DOCKING),
    )

    # -- results ------------------------------------------------------------
    _report(progress, STAGE_RESULTS[0], "Parsing docking results ...")
    results_dir = workspace / "results"
    try:
        split_poses(result.output_file, results_dir, write_pdb=True)
        attach_pose_files(result.poses, results_dir)
    except OSError as exc:                             # pragma: no cover
        log.warning("could not split pose files: %s", exc)

    result.workspace = workspace
    result.receptor = receptor.pdbqt_path
    result.ligand = ligand.pdbqt_path
    result.elapsed_seconds = time.perf_counter() - started

    summary_path = results_dir / "summary.csv"
    try:
        write_summary_csv(result, summary_path)
        write_report(result, results_dir / "report.md")
    except OSError as exc:                             # pragma: no cover
        log.warning("could not write the result summary: %s", exc)

    _report(progress, STAGE_RESULTS[1], summarise(result))
    return result


def _archive_ligand(request: DockingRequest, input_dir: Path) -> None:
    """Keep a copy of the ligand input next to the protein, for the record."""
    input_dir.mkdir(parents=True, exist_ok=True)
    if request.ligand_kind == "file":
        source = Path(request.ligand_source)
        if source.is_file():
            safe_copy(source, input_dir)
        return
    target = input_dir / "ligand.smi"
    target.write_text(request.ligand_source.strip() + "\n", encoding="utf-8")


__all__ = ["DockingRequest", "run_docking", "STAGE_DOCKING"]
