"""EnzyDock main window.

The window is a *controller*: it wires GUI signals to the computational
backend but never contains scientific logic itself.  Every backend step is
invoked through a clearly named handler so that Steps 3-8 can replace the
current placeholders (which only log and inform the user) with real calls to
:mod:`app.chemistry` / :mod:`app.docking`.

Layout
------
::

    +--------------------------------------------------------------+
    |  EnzyDock / Molecular Docking Platform        (menu bar)     |
    +-------------------------+------------------------------------+
    | Protein                 | Result (affinity + pose table)     |
    | Ligand / Substrate      |------------------------------------+
    | Docking (box + params)  | 3D viewer                          |
    +-------------------------+------------------------------------+
    | Activity log                                                 |
    +--------------------------------------------------------------+
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..chemistry.preparation import (
    PreparedLigand,
    PreparationOptions,
    build_3d,
    prepare_ligand,
)
from ..chemistry.receptor import PreparedReceptor, ReceptorOptions, prepare_receptor
from ..docking.base import DockingEngine, EngineError
from ..docking.docking_config import BoxConfig, DockingParameters, DockingResult
from ..docking.pipeline import DockingRequest, run_docking
from ..export import (
    ExportError,
    default_export_stem,
    export_all_poses,
    export_best_pose,
    export_csv,
    export_report,
)
from ..export.report import input_labels
from ..utils.logger import current_log_file, get_logger
from ..utils.paths import (
    create_workspace,
    resource,
    safe_copy,
    user_data_dir,
    workspace_root,
)
from ..utils.platforms import open_in_editor, reveal_path
from ..utils.settings import AppSettings
from ..visualization.interactions import describe
from .dialogs import show_error, show_info, show_report, show_warning
from .docking_panel import DockingPanel, RunBar
from .ligand_panel import LigandPanel
from .log_widget import LogPanel
from .protein_panel import ProteinOptions, ProteinPanel
from .result_panel import ResultPanel
from .viewer import MoleculeViewer
from .workers import FunctionWorker

log = get_logger(__name__)


class MainWindow(QMainWindow):
    """Top level window: owns the panels, the log and the status bar."""

    #: Emitted when the user changes any input, used later by autosave / undo.
    inputs_changed = Signal()

    def __init__(self, settings: AppSettings | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._settings = settings or AppSettings()
        self._workspace: Path | None = None         # current run workspace
        self._worker: FunctionWorker | None = None  # only one job at a time
        self._prepared_ligand: PreparedLigand | None = None
        self._prepared_receptor: PreparedReceptor | None = None
        self._docking_result: DockingResult | None = None
        #: Something the user must know about the current 3D view (an empty
        #: string when everything is fine).  Kept so a "docking finished"
        #: status line cannot quietly erase it.
        self._viewer_warning: str = ""
        self._engine: DockingEngine | None = None     # engine of the running job
        self._engine_checked = False                  # status bar probe done?

        self.setWindowTitle(f"EnzyDock {__version__} - Molecular Docking Platform")
        self.resize(1280, 860)
        self.setMinimumSize(1040, 700)

        self._build_panels()
        self._build_layout()
        self._build_menu()
        self._build_statusbar()
        self._connect_signals()
        self._restore_geometry()

        log.info("EnzyDock %s started", __version__)
        self.statusBar().showMessage("Ready - load a protein and a ligand to begin.", 6000)

    # ------------------------------------------------------------- assembly
    def _build_panels(self) -> None:
        self.protein_panel = ProteinPanel(self._settings, self)
        self.ligand_panel = LigandPanel(self._settings, self)
        self.docking_panel = DockingPanel(self._settings, self)
        self.run_bar = RunBar(self)
        self.result_panel = ResultPanel(self)
        self.viewer = MoleculeViewer(self)
        self.log_panel = LogPanel(self)
        self.log_panel.attach(get_logger("enzydock"))

    def _build_layout(self) -> None:
        # --- header -----------------------------------------------------------
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(4, 2, 4, 6)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("EnzyDock")
        title.setObjectName("title")
        subtitle = QLabel("Molecular Docking Platform")
        subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header_layout.addLayout(titles)
        header_layout.addStretch(1)

        version = QLabel(f"v{__version__}")
        version.setObjectName("muted")
        header_layout.addWidget(version)

        # --- left column: scrollable inputs + pinned run bar --------------------
        inputs = QWidget()
        inputs_layout = QVBoxLayout(inputs)
        inputs_layout.setContentsMargins(8, 4, 8, 8)
        inputs_layout.setSpacing(10)
        inputs_layout.addWidget(self.protein_panel)
        inputs_layout.addWidget(self.ligand_panel)
        inputs_layout.addWidget(self.docking_panel)
        inputs_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(inputs)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 0, 8, 8)
        left_layout.setSpacing(8)
        left_layout.addWidget(scroll, 1)
        left_layout.addWidget(self.run_bar)
        left.setMinimumWidth(420)
        left.setMaximumWidth(560)

        # --- right column --------------------------------------------------------
        right_split = QSplitter(Qt.Orientation.Vertical)
        right_split.addWidget(self.result_panel)
        right_split.addWidget(self.viewer)
        right_split.setStretchFactor(0, 4)
        right_split.setStretchFactor(1, 6)
        right_split.setSizes([340, 460])

        # --- central horizontal split ---------------------------------------------
        self._main_split = QSplitter(Qt.Orientation.Horizontal)
        self._main_split.addWidget(left)
        self._main_split.addWidget(right_split)
        self._main_split.setStretchFactor(0, 0)
        self._main_split.setStretchFactor(1, 1)
        self._main_split.setSizes([440, 840])

        central_split = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(8, 4, 8, 4)
        top_layout.setSpacing(4)
        top_layout.addWidget(header)
        top_layout.addWidget(self._main_split, 1)

        central_split.addWidget(top)
        central_split.addWidget(self.log_panel)
        central_split.setStretchFactor(0, 1)
        central_split.setStretchFactor(1, 0)
        central_split.setSizes([620, 170])

        self.setCentralWidget(central_split)

    def _build_menu(self) -> None:
        menubar: QMenuBar = self.menuBar()

        file_menu = menubar.addMenu("&File")
        open_protein = QAction("Open &Protein ...", self)
        open_protein.triggered.connect(self.protein_panel.select_file)
        file_menu.addAction(open_protein)

        open_ligand = QAction("Open &Ligand ...", self)
        open_ligand.triggered.connect(self.ligand_panel.select_file)
        file_menu.addAction(open_ligand)

        file_menu.addSeparator()
        workspace_action = QAction("Open &Workspace Folder", self)
        workspace_action.triggered.connect(self._open_workspace)
        file_menu.addAction(workspace_action)

        results_action = QAction("Open &Results Folder", self)
        results_action.triggered.connect(self._open_results_folder)
        file_menu.addAction(results_action)

        log_action = QAction("Open &Log File", self)
        log_action.triggered.connect(self._open_log_file)
        file_menu.addAction(log_action)

        file_menu.addSeparator()
        engine_action = QAction("Set Docking &Engine ...", self)
        engine_action.setStatusTip(
            "Use a different AutoDock Vina build, QVina2 or Smina")
        engine_action.triggered.connect(self._set_engine_location)
        file_menu.addAction(engine_action)

        bundled_action = QAction("Use the &Bundled Docking Engine", self)
        bundled_action.setStatusTip(
            "Forget the chosen path and go back to the engine that ships "
            "with EnzyDock")
        bundled_action.triggered.connect(self._use_bundled_engine)
        file_menu.addAction(bundled_action)

        file_menu.addSeparator()
        exit_action = QAction("E&xit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        help_menu = menubar.addMenu("&Help")
        diagnostics_action = QAction("Run &Diagnostics ...", self)
        diagnostics_action.triggered.connect(self._run_diagnostics)
        help_menu.addAction(diagnostics_action)

        about_action = QAction("&About EnzyDock", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _build_statusbar(self) -> None:
        bar: QStatusBar = self.statusBar()
        bar.setContentsMargins(8, 0, 8, 0)

        self._engine_status = QLabel("Engine: checking ...")
        self._engine_status.setObjectName("muted")
        self._engine_status.setToolTip(
            "Docking backend used for the next run; the location is checked at "
            "start-up."
        )
        bar.addPermanentWidget(self._engine_status)
        # Detected lazily: probing runs a subprocess, so it happens after the
        # window is on screen (see ``showEvent``).

        self._workspace_status = QLabel("")
        self._workspace_status.setObjectName("muted")
        bar.addPermanentWidget(self._workspace_status)
        self._update_workspace_status()

    def _connect_signals(self) -> None:
        # protein
        self.protein_panel.protein_loaded.connect(self._on_protein_loaded)
        self.protein_panel.prepare_requested.connect(self._on_prepare_protein)
        self.protein_panel.status_message.connect(self._show_status)

        # ligand
        self.ligand_panel.generate_3d_requested.connect(self._on_generate_3d)
        self.ligand_panel.ligand_changed.connect(lambda _s: self.inputs_changed.emit())
        self.ligand_panel.prepare_requested.connect(self._on_prepare_ligand)
        self.ligand_panel.status_message.connect(self._show_status)

        # docking
        self.run_bar.run_requested.connect(self.docking_panel.request_run)
        self.run_bar.cancel_requested.connect(self.docking_panel.request_cancel)
        self.docking_panel.run_requested.connect(self._on_run_docking)
        self.docking_panel.cancel_requested.connect(self._on_cancel_docking)
        self.docking_panel.detect_site_requested.connect(self._on_detect_site)
        self.docking_panel.status_message.connect(self._show_status)

        # results
        self.result_panel.pose_selected.connect(self._show_pose)
        self.result_panel.status_message.connect(self._show_status)
        self.result_panel.export_best_requested.connect(self._on_export_best)
        self.result_panel.export_all_requested.connect(self._on_export_all)
        self.result_panel.export_csv_requested.connect(self._on_export_csv)
        self.result_panel.export_report_requested.connect(self._on_export_report)

        # viewer
        self.viewer.status_message.connect(self._show_status)
        self.viewer.display_toggled.connect(self._on_display_toggled)

    # --------------------------------------------------------------- status
    def _show_status(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)

    def _update_workspace_status(self, path: Path | None = None) -> None:
        target = path or workspace_root(create=False)
        self._workspace_status.setText(f"Workspace: {target}")

    def set_engine_status(self, text: str) -> None:
        """Update the permanent 'Engine:' indicator in the status bar."""
        self._engine_status.setText(f"Engine: {text}")

    def set_busy(self, busy: bool) -> None:
        """Disable input panels while a long job runs."""
        self.protein_panel.set_busy(busy)
        self.ligand_panel.set_busy(busy)
        self.docking_panel.set_running(busy)
        self.run_bar.set_running(busy)
        if not busy:
            self.run_bar.reset_progress()

    def set_progress(self, percent: int, message: str = "") -> None:
        """Report pipeline progress (0-100, or ``-1`` for indeterminate)."""
        self.run_bar.set_progress(percent, message)
        if message:
            self._show_status(message)

    # -------------------------------------------------------------- handlers
    def _on_protein_loaded(self, path: Path) -> None:
        center = self.protein_panel.center_from_structure()
        if center is not None and all(value == 0.0 for value in
                                      self.docking_panel.box().center):
            self.docking_panel.set_center(center)
            log.info("box centre initialised from protein centroid: %s", center)

    def _on_prepare_protein(self, path: Path, options: ProteinOptions) -> None:
        """Clean, protonate and convert the receptor on a worker thread."""
        log.info("prepare protein requested: %s (%s)", Path(path).name, options)
        workspace = self._ensure_workspace(Path(path).stem or "protein")
        self._archive_protein_input(path, workspace / "input")

        receptor_options = ReceptorOptions(
            remove_waters=options.remove_waters,
            remove_hetero=options.remove_hetero,
            add_hydrogens=options.add_hydrogens,
            keep_chains=options.keep_chains,
        )
        worker = FunctionWorker(
            prepare_receptor, str(path), workspace / "prepared", receptor_options,
            title="Protein preparation",
        )
        worker.succeeded.connect(self._on_protein_prepared)
        self._start_worker(worker, "Protein preparation")

    def _on_generate_3d(self, smiles: str) -> None:
        """Embed + optimise the ligand in the background (Step 3)."""
        log.info("generate 3D requested for: %s", smiles)
        mol = self.ligand_panel.molecule
        if mol is None:
            show_warning(self, "Please provide a ligand first (SMILES or file).")
            return
        worker = FunctionWorker(build_3d, mol, PreparationOptions(),
                                title="3D structure generation")
        worker.succeeded.connect(self._on_3d_ready)
        self._start_worker(worker, "3D structure generation")

    def _on_prepare_ligand(self, source: str, payload: object) -> None:
        """Turn the current ligand into a docking-ready PDBQT (Step 3)."""
        log.info("prepare ligand requested (%s): %s", source, payload)
        mol = self.ligand_panel.molecule
        if mol is None:
            show_warning(self, "Please provide a ligand first (SMILES or file).")
            return

        name = self._ligand_label()
        workspace = self._ensure_workspace(name)
        self._archive_ligand_input(source, payload, workspace / "input")

        worker = FunctionWorker(
            prepare_ligand, mol, name, workspace / "prepared", PreparationOptions(),
            title="Ligand preparation",
        )
        worker.succeeded.connect(self._on_ligand_prepared)
        self._start_worker(worker, "Ligand preparation")

    def _ligand_label(self) -> str:
        """Short, filesystem-friendly name for the current ligand.

        A file based ligand uses its file stem (``aspirin``); a typed SMILES
        has no name worth putting in a folder, so it becomes ``ligand``.
        """
        summary = self.ligand_panel.summary
        if summary is None or summary.source == "SMILES":
            return "ligand"
        return Path(summary.source).stem or "ligand"

    # ------------------------------------------------------------ workspace
    def _ensure_workspace(self, tag: str | None = None) -> Path:
        """Return the current run workspace, creating it on first use."""
        if self._workspace is None:
            self._workspace = create_workspace(tag)
            self._update_workspace_status(self._workspace)
            log.info("created workspace %s", self._workspace)
        return self._workspace

    def _archive_ligand_input(self, source: str, payload: object,
                              input_dir: Path) -> None:
        """Copy the user's ligand input into the workspace (never modify it)."""
        try:
            if source == "file" and payload and Path(str(payload)).is_file():
                safe_copy(str(payload), input_dir)
            elif payload:
                target = input_dir / "ligand.smi"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(payload), encoding="utf-8")
        except OSError as exc:                         # pragma: no cover
            log.warning("could not archive the ligand input: %s", exc)

    def _archive_protein_input(self, path: Path, input_dir: Path) -> None:
        """Copy the user's structure file into the workspace (read-only input)."""
        try:
            if Path(path).is_file():
                safe_copy(str(path), input_dir)
        except OSError as exc:                         # pragma: no cover
            log.warning("could not archive the protein input: %s", exc)

    # -------------------------------------------------------------- workers
    def _start_worker(self, worker: FunctionWorker, title: str) -> bool:
        """Run ``worker`` unless another job is still busy. ``True`` on start."""
        if self._worker is not None and self._worker.isRunning():
            show_warning(self, f"'{title}' was not started: another job is still running.")
            return False
        self._worker = worker
        worker.progress.connect(self.set_progress)
        worker.failed.connect(lambda message, details:
                              show_error(self, message, details, title))
        worker.finished.connect(self._on_worker_finished)
        self.set_busy(True)
        self.set_progress(0, f"{title} ...")
        worker.start()
        return True

    def _on_worker_finished(self) -> None:
        """Re-enable the UI once the background job is done."""
        self.set_busy(False)
        self.set_progress(100, "Done")
        self._worker = None

    # ------------------------------------------------------ job completions
    def _on_3d_ready(self, built: object) -> None:
        """Show the freshly built 3D conformer."""
        mol = getattr(built, "mol", None)
        if mol is None:                                # pragma: no cover
            return
        self.ligand_panel.apply_molecule(mol)
        energy = getattr(built, "energy", None)
        force_field = getattr(built, "force_field", "none")
        text = f"3D structure generated ({force_field}"
        text += f", {energy:.1f} kcal/mol)" if energy is not None else ")"
        self._show_status(text)
        log.info("%s | reused input coordinates: %s", text,
                 getattr(built, "reused_input_coordinates", False))

    def _on_ligand_prepared(self, prepared: object) -> None:
        """Display and remember the docking-ready ligand."""
        self._prepared_ligand = prepared
        self.ligand_panel.show_prepared(prepared)
        pdbqt = getattr(prepared, "pdbqt_path", None)
        torsions = getattr(prepared, "rotatable_bonds", 0)
        if pdbqt is not None:
            self._show_status(
                f"Ligand ready: {torsions} rotatable bonds, "
                f"docking input written to {Path(pdbqt).name}."
            )
            log.info("ligand prepared -> %s", pdbqt)

    def _on_protein_prepared(self, prepared: object) -> None:
        """Display and remember the docking-ready receptor."""
        self._prepared_receptor = prepared
        self.protein_panel.show_prepared(prepared)
        pdbqt = getattr(prepared, "pdbqt_path", None)
        if pdbqt is None:                              # pragma: no cover
            return
        self._show_status(
            f"Receptor ready: {prepared.residue_count:,} residues, "
            f"{prepared.atom_count:,} atoms -> {Path(pdbqt).name}."
        )
        log.info("receptor prepared -> %s", pdbqt)
        # The prepared receptor is a better box anchor than the raw input
        # (waters and hetero atoms are gone, so the centroid is meaningful).
        center = prepared.center
        if center is not None:
            self.docking_panel.set_center(
                (round(center[0], 2), round(center[1], 2), round(center[2], 2))
            )

    def _on_detect_site(self) -> None:
        """V1.1 will call P2Rank/fpocket; today we use the geometric centre."""
        summary = self.protein_panel.summary
        if summary is None or summary.center is None:
            show_info(
                self,
                "Load a protein first - the binding box can then be centred on it.\n\n"
                "Automatic pocket detection (P2Rank / fpocket) is planned for V1.1.",
            )
            return
        self.docking_panel.set_center(summary.center)
        self._show_status(
            "Box centred on the protein's geometric centre "
            f"({summary.center[0]:.1f}, {summary.center[1]:.1f}, {summary.center[2]:.1f})."
        )

    def _on_run_docking(self, box: BoxConfig, parameters: DockingParameters) -> None:
        """Dispatch a complete docking run to a background worker."""
        log.info("run requested: %s | %s", box, parameters)

        protein_path = self.protein_panel.path
        if protein_path is None:
            show_info(
                self,
                "Please load a protein structure first.\n\n"
                "Docking needs a receptor (PDB or mmCIF) in addition to the ligand.",
                "Nothing to dock yet",
            )
            return

        kind, value = self.ligand_panel.source
        if not value:
            show_info(
                self,
                "Please enter a SMILES string or load a ligand file first.",
                "Nothing to dock yet",
            )
            return

        try:
            engine = DockingEngine.create(parameters.engine, settings=self._settings)
        except EngineError as exc:
            show_error(self, exc.message, exc.details, "Docking engine")
            return

        available, reason = engine.is_available()
        if not available:
            details = engine.executable_path() or ""
            show_error(self, reason, details, "Docking engine not available")
            return

        self._engine = engine
        request = DockingRequest(
            receptor_source=Path(protein_path),
            ligand_source=value,
            ligand_kind=kind,
            box=box,
            parameters=parameters,
            receptor_options=self.protein_panel.options().to_receptor_options(),
            ligand_options=PreparationOptions(),
            prepared_receptor=self._prepared_receptor,
            prepared_ligand=self._prepared_ligand,
            receptor_name=Path(protein_path).stem,
            ligand_name=self._ligand_label(),
            engine=engine,
        )

        worker = FunctionWorker(run_docking, request, title="Docking")
        worker.succeeded.connect(self._on_docking_finished)
        if self._start_worker(worker, "Docking"):
            self.result_panel.clear()
            self._show_status("Docking started ...")

    def _on_cancel_docking(self) -> None:
        """Ask the running engine to stop; the worker reports the outcome."""
        engine = self._engine
        if engine is None:
            self._show_status("Nothing to cancel.")
            return
        log.info("user requested cancellation of the docking run")
        engine.cancel()
        self._show_status("Cancelling the docking run ...")

    def _on_docking_finished(self, result: DockingResult) -> None:
        """Show the pose table and load the best pose into the viewer."""
        self._docking_result = result
        self.result_panel.set_result(result)
        best = result.best_affinity
        text = (f"Docking finished: {len(result.poses)} poses, "
                f"best affinity {best:.2f} kcal/mol" if best is not None
                else "Docking finished without any poses.")
        self._show_status(text)
        log.info("%s (workspace %s)", text, result.workspace)

        if result.box is not None:
            self.viewer.set_box(result.box)

        if result.poses:
            self._show_pose(result.poses[0].pose)

        if result.workspace is not None:
            self._show_status(f"{text} - results in {result.workspace.name}")

        # A problem with the 3D view must survive the line above - otherwise
        # "the receptor is missing" is on screen for a few milliseconds only.
        if self._viewer_warning:
            self._show_status(f"{text} - {self._viewer_warning}")

    def _show_pose(self, pose_number: int) -> None:
        """Load one pose (plus the receptor) into the 3D viewer."""
        result = self._docking_result
        if result is None:
            return
        pose = next((p for p in result.poses if p.pose == pose_number), None)
        workspace = result.workspace
        if pose is None or workspace is None:
            return

        receptor = self._receptor_for_viewer(result, workspace)
        if receptor is not None:
            self._viewer_warning = ""
            self.viewer.set_protein(receptor)
        else:
            self.viewer.clear_scene()
            log.error("no receptor structure found in workspace %s", workspace)
            self._viewer_warning = (
                "the receptor structure is missing, so only the pose is shown "
                f"(looked in {workspace / 'prepared'} and {workspace / 'input'})"
            )
            self._show_status(f"The 3D view: {self._viewer_warning}.")

        if pose.pose_file is not None and Path(pose.pose_file).is_file():
            self.viewer.set_ligand(pose.pose_file)
        else:
            log.warning("pose %s has no structure file", pose_number)
            self._show_status(
                f"Pose {pose_number} has no structure file - the 3D view was "
                "not updated."
            )
        self.viewer.show_pose(pose_number)

        # Keep the interaction list in step with the 3D view: both describe
        # the very same pose.
        self.result_panel.set_interactions(
            self.viewer.scene.interactions, pose_number
        )

    def _receptor_for_viewer(
        self, result: DockingResult, workspace: Path
    ) -> Path | None:
        """First receptor structure that exists for this run, or ``None``.

        Several files describe the same receptor; showing *something* matters
        much more than showing a specific one, so every plausible location is
        tried - including the untouched input file, which is always archived
        in the workspace even when preparation output was deleted later.
        """
        prepared = workspace / "prepared"
        for name in ("receptor_clean.pdb", "receptor_protonated.pdb"):
            candidate = prepared / name
            if candidate.is_file():
                return candidate

        input_receptor, _ligand = input_labels(result)
        if input_receptor and not input_receptor.startswith("SMILES"):
            candidate = workspace / "input" / input_receptor
            if candidate.is_file():
                log.info("using the original input structure for the 3D view: %s",
                         candidate.name)
                return candidate

        for candidate in (prepared / "receptor.pdbqt", result.receptor):
            if candidate is not None and Path(candidate).is_file():
                log.warning("only the PDBQT receptor is available for the 3D view: %s",
                            Path(candidate).name)
                return Path(candidate)
        return None

    def _on_display_toggled(self, name: str, enabled: bool) -> None:
        """Explain in one line what the toggle actually did."""
        log.debug("viewer display '%s' -> %s", name, enabled)
        scene = self.viewer.scene
        if not enabled or scene.is_empty:
            return
        if name == "pocket":
            residues = scene.pocket_labels
            detail = ", ".join(residues[:8]) + (" ..." if len(residues) > 8 else "")
            self._show_status(
                f"Binding pocket: {len(residues)} residues ({scene.pocket_source})"
                + (f" - {detail}" if detail else "")
            )
        elif name == "surface":
            self._show_status(
                "Showing the receptor surface - switch it off again if the "
                "view becomes slow."
            )
        elif name == "hbonds":
            bonds = scene.hbonds
            if bonds:
                self._show_status(
                    f"{len(bonds)} possible hydrogen bond(s) within 3.5 A "
                    f"(closest: {bonds[0].label})"
                )
            else:
                self._show_status(
                    "No receptor-ligand contact within 3.5 A - try a different pose."
                )
        elif name == "contacts":
            items = scene.interactions
            pose = self.result_panel.selected_pose()
            if items:
                self.result_panel.show_interactions_tab(True)
                self.result_panel.set_interactions(
                    items, pose.pose if pose is not None else None
                )
                self._show_status(
                    f"{len(items)} possible interaction(s): {describe(items)}"
                )
            else:
                self._show_status(
                    "No receptor-ligand interactions within 4.5 A - "
                    "try a different pose."
                )

    # ---------------------------------------------------------------- export
    def _export_result(self) -> DockingResult | None:
        """The finished result to export, or ``None`` plus a status hint.

        The export buttons are disabled until a run finished, so this is only
        a guard for programmatic calls - it must never raise or open a dialog.
        """
        result = self._docking_result
        if result is None or not result.poses:
            self._show_status("Nothing to export yet - run a docking first.")
            log.warning("export requested without a finished docking run")
            return None
        return result

    def _default_export_path(self, suffix: str) -> Path:
        """``<remembered folder>/<run>_<suffix>`` for a save dialog."""
        result = self._docking_result
        stem = default_export_stem(result) if result is not None else "enzydock_results"
        return self._settings.last_export_dir() / f"{stem}{suffix}"

    def _on_export_best(self) -> None:
        """Save the lowest-energy pose as PDB (plus its PDBQT companion)."""
        result = self._export_result()
        if result is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export best pose",
            str(self._default_export_path("_best_pose.pdb")),
            "PDB structure (*.pdb);;All files (*.*)",
        )
        if not path:
            return
        try:
            written = export_best_pose(result, path)
        except (ExportError, OSError) as exc:
            show_error(self, getattr(exc, "message",
                                     "The pose file could not be written."),
                       exc, "Export best pose")
            return
        best = result.best_pose
        self._finish_export(
            written,
            f"Best pose (pose {best.pose}, {best.affinity:.2f} kcal/mol) exported",
        )

    def _on_export_all(self) -> None:
        """Copy every pose, the CSV table and the report into one folder."""
        result = self._export_result()
        if result is None:
            return
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose a folder for all poses",
            str(self._default_export_path("_poses")),
        )
        if not directory:
            return
        try:
            written = export_all_poses(result, directory)
        except (ExportError, OSError) as exc:
            show_error(self, getattr(exc, "message",
                                     "The pose files could not be written."),
                       exc, "Export all poses")
            return
        self._finish_export(written, f"{len(result.poses)} poses exported")

    def _on_export_csv(self) -> None:
        """Save the pose table as CSV."""
        result = self._export_result()
        if result is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export pose table (CSV)",
            str(self._default_export_path("_summary.csv")),
            "CSV table (*.csv);;All files (*.*)",
        )
        if not path:
            return
        try:
            written = [export_csv(result, path)]
        except OSError as exc:
            show_error(self, "The CSV file could not be written.", exc, "Export CSV")
            return
        self._finish_export(written, f"{len(result.poses)} rows exported")

    def _on_export_report(self) -> None:
        """Save the human readable run report."""
        result = self._export_result()
        if result is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export docking report",
            str(self._default_export_path("_report.md")),
            "Markdown report (*.md);;Text file (*.txt);;All files (*.*)",
        )
        if not path:
            return
        try:
            written = [export_report(result, path)]
        except OSError as exc:
            show_error(self, "The report file could not be written.", exc,
                       "Export report")
            return
        self._finish_export(written, "Report exported")

    def _finish_export(self, files: list[Path], what: str) -> None:
        """Remember the folder, log the outcome and tell the user."""
        if not files:                                  # pragma: no cover
            return
        self._settings.remember_export_dir(files[0])
        self._settings.sync()
        self._show_status(f"{what} to {files[0]}")
        log.info("%s -> %s", what, ", ".join(str(path) for path in files))
        show_info(
            self,
            f"{what} to:\n\n" + "\n".join(f"- {path}" for path in files),
            "Export finished",
        )

    # ----------------------------------------------------------------- menu
    def _open_workspace(self) -> None:
        """Reveal the workspace folder in the system file manager."""
        self._reveal(workspace_root(create=True))

    def _open_results_folder(self) -> None:
        """Reveal ``<workspace>/results`` of the current run."""
        result = self._docking_result
        if result is None or result.workspace is None:
            show_info(self, "No results have been produced yet - run a docking first.")
            return
        candidate = Path(result.workspace) / "results"
        self._reveal(candidate if candidate.is_dir() else Path(result.workspace))

    def _reveal(self, path: Path) -> None:
        """Open ``path`` in the platform's file manager."""
        if not reveal_path(path):                     # pragma: no cover
            show_error(self, f"Could not open the folder:\n{path}")

    def _open_log_file(self) -> None:
        target = current_log_file()
        if target is None or not Path(target).is_file():
            show_info(self, "No log file has been written yet.")
            return
        if not open_in_editor(Path(target)):          # pragma: no cover
            show_error(self, "Could not open the log file.")

    def _show_about(self) -> None:
        show_info(
            self,
            f"EnzyDock {__version__}\n\n"
            "Molecular docking platform for enzyme engineering.\n"
            "GUI: PySide6 | Chemistry: RDKit | PDBQT: meeko | Engine: AutoDock Vina\n\n"
            "This build: Steps 1-10 (GUI, ligand and receptor preparation, "
            "AutoDock Vina docking, result parsing, 3D viewer, result export, "
            "end-to-end tests and desktop packaging).\n\n"
            "Docking engine: AutoDock Vina 1.2.7, (c) The Scripps Research "
            "Institute, redistributed unmodified under the Apache License 2.0.\n"
            "Full notices: app/resources/licenses/. Please cite AutoDock Vina "
            "in any publication that uses these results.",
            "About EnzyDock",
        )

    def _run_diagnostics(self) -> None:
        """Check that every backend really works and show the report.

        The checks run on a worker thread because preparing a protein takes a
        couple of seconds; the report can be copied straight into a bug report.
        """
        from ..selftest import SelftestOptions, format_report, run_checks

        options = SelftestOptions(pdb=self.protein_panel.path, dock=False)

        worker = FunctionWorker(run_checks, options, title="Diagnostics")
        worker.succeeded.connect(
            lambda checks: self._show_diagnostics_report(checks, format_report)
        )
        self._start_worker(worker, "Diagnostics")

    def _show_diagnostics_report(self, checks, formatter) -> None:
        """Present the diagnostics result to the user."""
        failed = [check.name for check in checks if not check.ok]
        intro = ("Every backend EnzyDock depends on has been checked. "
                 "Copy this report into a bug report if something fails.")
        if failed:
            intro = ("Some checks failed: " + ", ".join(failed) +
                     "\n\nCopy this report into a bug report.")
        show_report(self, "EnzyDock diagnostics", formatter(checks), intro)

        if failed:
            self._show_status(f"Diagnostics: {len(failed)} check(s) failed")
        else:
            self._show_status(f"Diagnostics: all {len(checks)} checks passed")

    # ------------------------------------------------------------- lifecycle
    # ------------------------------------------------------------ lifecycle
    def showEvent(self, event) -> None:                 # noqa: D102 - Qt override
        super().showEvent(event)
        if not self._engine_checked:
            self._engine_checked = True
            # Probing an engine starts a subprocess; do it once the window is
            # visible so start-up stays instant.
            QTimer.singleShot(0, self._refresh_engine_status)

    def _refresh_engine_status(self) -> None:
        """Report whether the selected docking engine can actually be run."""
        name = self.docking_panel.engine_name()
        try:
            engine = DockingEngine.create(name, settings=self._settings)
        except EngineError as exc:                     # pragma: no cover
            self._engine_status.setText(f"Engine: {name} - unavailable")
            self._engine_status.setToolTip(exc.details)
            return
        available, reason = engine.is_available()
        if available:
            version = engine.version() if hasattr(engine, "version") else ""
            detail = version or engine.name
            self._engine_status.setText(f"Engine: {detail} - ready")
            self._engine_status.setToolTip(reason)
        else:
            self._engine_status.setText(f"Engine: {name} - not found")
            self._engine_status.setToolTip(reason)
            log.warning("docking engine unavailable: %s", reason)

    def _set_engine_location(self) -> None:
        """Let the user point EnzyDock at a docking engine of their own.

        EnzyDock ships with AutoDock Vina, so this is only needed to use a
        different build (QVina2, Smina, a faster Vina).  The choice is stored
        in the settings and wins over the bundled copy.
        """
        from ..docking.vina_runner import VINA_SETTINGS_KEY
        from ..utils.executables import probe_executable

        from ..utils.platforms import (
            VINA_FILE_FILTER,
            bundled_vina_name,
            quarantine_hint,
            runnable_copy,
        )

        stored = self._settings.get_str(VINA_SETTINGS_KEY).strip()
        start = stored or str(resource("bin"))
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            f"Select the docking engine ({bundled_vina_name()})",
            start,
            VINA_FILE_FILTER,
        )
        if not chosen:
            return

        path = Path(chosen)
        usable = runnable_copy(path, user_data_dir(create=False) / "bin")
        if usable is None:
            show_error(
                self,
                "That file is not a program EnzyDock can run:\n\n"
                f"    {path}\n\n"
                f"{quarantine_hint()}",
                f"Not executable: {path}",
                "Docking engine",
            )
            return
        probed = probe_executable(usable, source="the file you selected")
        if not probed.version:
            show_error(
                self,
                "That file could not be started as a docking engine:\n\n"
                f"    {path}\n\n"
                "Choose the AutoDock Vina program itself - a file called "
                f"{bundled_vina_name()}, about 1 MB in size. "
                "EnzyDock keeps using the engine it came with.",
                f"The program did not answer '--version': {path}",
                "Docking engine",
            )
            return

        self._settings.set_value(VINA_SETTINGS_KEY, str(path))
        self._refresh_engine_status()
        log.info("docking engine set to %s by the user", path)
        show_info(
            self,
            f"EnzyDock will use this program for docking:\n\n"
            f"    {path}\n\n{probed.version}",
            "Docking engine",
        )

    def _use_bundled_engine(self) -> None:
        """Forget a custom engine path and fall back to the bundled Vina."""
        from ..docking.vina_runner import VINA_SETTINGS_KEY

        self._settings.set_value(VINA_SETTINGS_KEY, "")
        self._refresh_engine_status()
        log.info("docking engine reset to the bundled copy")
        show_info(
            self,
            "EnzyDock is back on the AutoDock Vina that ships with it:\n\n"
            f"    {resource('bin', VINA_BUNDLED_NAME)}",
            "Docking engine",
        )

    def _restore_geometry(self) -> None:
        geometry = self._settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        state = self._settings.value("window/splitter")
        if state:
            self._main_split.restoreState(state)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Persist geometry and settings, then close."""
        self._settings.set_value("window/geometry", self.saveGeometry())
        self._settings.set_value("window/splitter", self._main_split.saveState())
        self._settings.sync()
        log.info("EnzyDock closed")
        super().closeEvent(event)


__all__ = ["MainWindow"]
