"""Binding-site definition, docking parameters and the RUN control.

The panel owns no computation at all: it produces plain
:class:`~app.docking.docking_config.BoxConfig` /
:class:`~app.docking.docking_config.DockingParameters` objects and emits
:attr:`run_requested`.  Whatever sits behind that signal can be swapped from
Vina to Smina to an AI rescoring backend without touching this file.
"""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..docking.base import DockingEngine
from ..docking.docking_config import (
    DEFAULT_BOX_SIZE,
    DEFAULT_ENERGY_RANGE,
    DEFAULT_EXHAUSTIVENESS,
    DEFAULT_NUM_POSES,
    BoxConfig,
    DockingConfigError,
    DockingParameters,
)
from ..utils.logger import get_logger
from ..utils.settings import AppSettings
from .dialogs import show_error, show_info
from .widgets import Divider, section_hint

log = get_logger(__name__)

FALLBACK_ENGINE = "AutoDock Vina"


class DockingPanel(QGroupBox):
    """Left-hand 'Binding Site' + 'Docking Parameters' + 'RUN' section."""

    #: ``(box, parameters)`` - user pressed START DOCKING.
    run_requested = Signal(object, object)
    #: user pressed Cancel while a run was in progress.
    cancel_requested = Signal()
    #: user wants automatic pocket detection (V1.1).
    detect_site_requested = Signal()
    status_message = Signal(str)

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__("Docking", parent)
        self.setObjectName("panel")
        self._settings = settings
        self._running = False

        self._build_ui()
        self._restore_settings()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- binding site ----------------------------------------------------
        site_label = QLabel("Binding site")
        site_label.setObjectName("muted")
        layout.addWidget(site_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)

        self._center: list[QDoubleSpinBox] = []
        self._size: list[QDoubleSpinBox] = []
        for axis_index, axis in enumerate("XYZ"):
            grid.addWidget(QLabel(f"Center {axis}:"), axis_index, 0)
            center = self._make_spin(-100000.0, 100000.0, 0.0, 2, " A")
            grid.addWidget(center, axis_index, 1)
            self._center.append(center)

        for axis_index, axis in enumerate("XYZ"):
            grid.addWidget(QLabel(f"Size {axis}:"), axis_index, 2)
            size = self._make_spin(1.0, 60.0, DEFAULT_BOX_SIZE[axis_index], 1, " A")
            grid.addWidget(size, axis_index, 3)
            self._size.append(size)

        layout.addLayout(grid)

        site_buttons = QHBoxLayout()
        self._detect_btn = QPushButton("Detect Binding Site")
        self._detect_btn.clicked.connect(self._on_detect)
        site_buttons.addWidget(self._detect_btn)

        self._reset_btn = QPushButton("Reset")
        self._reset_btn.clicked.connect(self.reset_box)
        site_buttons.addWidget(self._reset_btn)
        site_buttons.addStretch(1)
        layout.addLayout(site_buttons)

        layout.addWidget(section_hint(
            "Automatic pocket detection arrives in V1.1; for now enter the box "
            "manually or press Detect to use the protein's geometric centre."
        ))

        layout.addWidget(Divider())

        # --- parameters -------------------------------------------------------
        params_label = QLabel("Docking parameters")
        params_label.setObjectName("muted")
        layout.addWidget(params_label)

        params = QGridLayout()
        params.setHorizontalSpacing(10)
        params.setVerticalSpacing(5)

        self._exhaustiveness = QSpinBox()
        self._exhaustiveness.setRange(1, 100)
        self._exhaustiveness.setValue(DEFAULT_EXHAUSTIVENESS)
        self._exhaustiveness.setToolTip(
            "Search effort. 8 is a good default; raise it to 16-32 for flexible ligands."
        )
        params.addWidget(QLabel("Exhaustiveness:"), 0, 0)
        params.addWidget(self._exhaustiveness, 0, 1)

        self._num_poses = QSpinBox()
        self._num_poses.setRange(1, 100)
        self._num_poses.setValue(DEFAULT_NUM_POSES)
        params.addWidget(QLabel("Number of poses:"), 1, 0)
        params.addWidget(self._num_poses, 1, 1)

        self._energy_range = QDoubleSpinBox()
        self._energy_range.setRange(0.0, 20.0)
        self._energy_range.setDecimals(1)
        self._energy_range.setSingleStep(0.5)
        self._energy_range.setValue(DEFAULT_ENERGY_RANGE)
        self._energy_range.setSuffix(" kcal/mol")
        params.addWidget(QLabel("Energy range:"), 2, 0)
        params.addWidget(self._energy_range, 2, 1)

        self._seed = QSpinBox()
        self._seed.setRange(-1, 2_000_000_000)
        self._seed.setSpecialValueText("random")
        self._seed.setValue(-1)
        self._seed.setToolTip("-1 / 'random' lets the engine pick a seed.")
        params.addWidget(QLabel("Seed:"), 3, 0)
        params.addWidget(self._seed, 3, 1)

        self._engine = QComboBox()
        self._engine.addItems(self._engine_names())
        params.addWidget(QLabel("Engine:"), 4, 0)
        params.addWidget(self._engine, 4, 1)

        layout.addLayout(params)

        for widget in (self._exhaustiveness, self._num_poses,
                       self._energy_range, self._seed):
            widget.valueChanged.connect(self._persist_settings)
        self._engine.currentTextChanged.connect(self._persist_settings)

    # ------------------------------------------------------------ factories
    @staticmethod
    def _make_spin(minimum: float, maximum: float, value: float,
                   decimals: int, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSingleStep(1.0 if decimals == 1 else 0.5)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setMinimumWidth(96)
        return spin

    @staticmethod
    def _engine_names() -> list[str]:
        """Registered engine names, always including the Vina default."""
        names = DockingEngine.available_engines()
        if not names:
            names = [FALLBACK_ENGINE]
        return names

    # ---------------------------------------------------------- public API
    def box(self) -> BoxConfig:
        """Return the box currently shown in the panel (validated)."""
        return BoxConfig(
            center=(self._center[0].value(), self._center[1].value(), self._center[2].value()),
            size=(self._size[0].value(), self._size[1].value(), self._size[2].value()),
        )

    def parameters(self) -> DockingParameters:
        """Return the parameters currently shown in the panel (validated)."""
        seed = self._seed.value()
        return DockingParameters(
            exhaustiveness=self._exhaustiveness.value(),
            num_poses=self._num_poses.value(),
            energy_range=self._energy_range.value(),
            seed=None if seed < 0 else seed,
            engine=self._engine.currentText(),
        )

    def set_center(self, center: Sequence[float]) -> None:
        """Programmatically set the box centre (e.g. from pocket detection)."""
        for spin, value in zip(self._center, center):
            spin.setValue(float(value))
        log.debug("box centre set to %s", tuple(center))

    def reset_box(self) -> None:
        """Restore the default 22 A cube centred on the origin."""
        for spin in self._center:
            spin.setValue(0.0)
        for spin, value in zip(self._size, DEFAULT_BOX_SIZE):
            spin.setValue(value)
        self.status_message.emit("Binding box reset to a 22 A cube at the origin.")

    def set_running(self, running: bool) -> None:
        """Switch the parameter widgets between idle and running state."""
        self._running = running
        self._detect_btn.setEnabled(not running)
        self._reset_btn.setEnabled(not running)
        for spin in (*self._center, *self._size):
            spin.setEnabled(not running)
        self._exhaustiveness.setEnabled(not running)
        self._num_poses.setEnabled(not running)
        self._energy_range.setEnabled(not running)
        self._seed.setEnabled(not running)
        self._engine.setEnabled(not running)

    # ------------------------------------------------------------- actions
    def engine_name(self) -> str:
        """Name of the docking backend currently selected."""
        return self._engine.currentText()

    def set_exhaustiveness(self, value: int) -> None:
        """Programmatically set the search effort (used by the smoke test)."""
        self._exhaustiveness.setValue(int(value))

    def request_run(self) -> None:
        """Validate the current settings and emit :attr:`run_requested`.

        Called by the run bar (see :class:`RunBar`), which is pinned below the
        scrollable input area so *START DOCKING* is always reachable.
        """
        try:
            box = self.box()
            parameters = self.parameters()
            parameters.validate()
        except DockingConfigError as exc:
            show_error(self, str(exc), "docking_panel: invalid parameters",
                       "Please check your docking settings")
            return
        if box.volume < 15 ** 3:
            show_info(
                self,
                "The docking box is quite small (below 15 A per edge). "
                "Make sure the ligand fits inside it with some margin.",
            )
        log.info("docking requested: %s | %s", box, parameters)
        self.run_requested.emit(box, parameters)

    def request_cancel(self) -> None:
        """Ask the controller to cancel the running job."""
        log.info("user requested cancellation")
        if self._running:
            self.status_message.emit("Cancelling the current run ...")
        self.cancel_requested.emit()

    def _on_detect(self) -> None:
        """Ask the controller for a binding site (geometric centre for now)."""
        self.status_message.emit("Detecting a binding site ...")
        self.detect_site_requested.emit()

    # ------------------------------------------------------------ settings
    def _persist_settings(self) -> None:
        self._settings.set_value("docking/exhaustiveness", self._exhaustiveness.value())
        self._settings.set_value("docking/num_poses", self._num_poses.value())
        self._settings.set_value("docking/energy_range", self._energy_range.value())
        self._settings.set_value("docking/seed", self._seed.value())
        self._settings.set_value("docking/engine", self._engine.currentText())

    def _restore_settings(self) -> None:
        self._exhaustiveness.setValue(self._settings.get_int("docking/exhaustiveness"))
        self._num_poses.setValue(self._settings.get_int("docking/num_poses"))
        self._energy_range.setValue(self._settings.get_float("docking/energy_range"))
        self._seed.setValue(self._settings.get_int("docking/seed"))
        size = (
            self._settings.get_float("box/size_x"),
            self._settings.get_float("box/size_y"),
            self._settings.get_float("box/size_z"),
        )
        for spin, value in zip(self._size, size):
            spin.setValue(value)

        engine = self._settings.get_str("docking/engine")
        available = self._engine_names()
        if engine in available:
            self._engine.setCurrentText(engine)
        elif FALLBACK_ENGINE in available:
            self._engine.setCurrentText(FALLBACK_ENGINE)


class RunBar(QWidget):
    """Pinned control strip holding *START DOCKING* and the progress report.

    It lives outside the scrollable input column so the primary action is
    always visible, whatever the window size.
    """

    #: the user pressed START DOCKING.
    run_requested = Signal()
    #: the user pressed Cancel.
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("runbar")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self._run_btn = QPushButton("START DOCKING")
        self._run_btn.setObjectName("run")
        self._run_btn.setMinimumHeight(44)
        self._run_btn.setToolTip("Prepare the structures and run the docking engine")
        self._run_btn.clicked.connect(self.run_requested.emit)
        layout.addWidget(self._run_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("danger")
        self._cancel_btn.setMinimumHeight(44)
        self._cancel_btn.clicked.connect(self.cancel_requested.emit)
        self._cancel_btn.hide()
        layout.addWidget(self._cancel_btn)

        self._progress = QProgressBar()
        self._progress.setObjectName("busy")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("Idle")
        self._progress.setTextVisible(True)
        layout.addWidget(self._progress)

        self._stage_label = QLabel("")
        self._stage_label.setObjectName("sectionHint")
        self._stage_label.setWordWrap(True)
        self._stage_label.hide()
        layout.addWidget(self._stage_label)

    # ---------------------------------------------------------- public API
    def set_running(self, running: bool) -> None:
        """Toggle between the RUN and Cancel presentation."""
        self._run_btn.setVisible(not running)
        self._cancel_btn.setVisible(running)
        if not running:
            self._stage_label.hide()

    def set_progress(self, percent: int, message: str = "") -> None:
        """Update the progress bar; ``percent < 0`` means *indeterminate*."""
        if percent < 0:
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(max(0, min(100, percent)))
        if message:
            self._progress.setFormat(f"{message}  %p%" if percent >= 0 else message)
            self._stage_label.setText(message)
            self._stage_label.show()
        log.debug("progress %s%%: %s", percent, message)

    def reset_progress(self) -> None:
        """Return the bar to its idle state."""
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("Idle")
        self._stage_label.hide()


__all__ = ["DockingPanel", "RunBar"]
