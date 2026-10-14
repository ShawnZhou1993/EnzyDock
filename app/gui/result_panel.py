"""Right-hand result section: pose table, interactions and export actions."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..docking.docking_config import DockingResult, PoseResult
from ..utils.logger import get_logger
from ..visualization.interactions import (
    KIND_LABELS,
    Interaction,
    group_counts,
)
from .widgets import Divider

log = get_logger(__name__)

POSE_COLUMNS = ("Pose", "Affinity\n(kcal/mol)", "RMSD l.b.", "RMSD u.b.")
INTERACTION_COLUMNS = ("Interaction", "Receptor", "Ligand", "Distance\n(A)")

#: Colour of the interaction-kind cell, matching the 3D scene.
KIND_COLORS = {
    "salt bridge": "#ff7ab6",
    "hbond": "#ffd479",
    "halogen": "#7ee0ff",
    "pi-stacking": "#b18cff",
    "hydrophobic": "#8f9aa6",
}


class ResultPanel(QGroupBox):
    """Pose table, interaction list and export buttons."""

    #: ``(pose_number)`` - the user selected a row.
    pose_selected = Signal(int)
    export_best_requested = Signal()
    export_all_requested = Signal()
    export_csv_requested = Signal()
    export_report_requested = Signal()
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Result", parent)
        self.setObjectName("panel")
        self._result: DockingResult | None = None
        self._interactions: list[Interaction] = []
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self._affinity_label = QLabel("Best affinity")
        self._affinity_label.setObjectName("muted")
        header.addWidget(self._affinity_label)

        self._affinity_value = QLabel("-")
        self._affinity_value.setObjectName("metric")
        header.addWidget(self._affinity_value)
        header.addStretch(1)

        self._engine_label = QLabel("")
        self._engine_label.setObjectName("muted")
        header.addWidget(self._engine_label)
        layout.addLayout(header)

        self._tabs = QTabWidget()
        self._table = self._build_pose_table()
        self._interaction_table = self._build_interaction_table()
        self._interaction_hint = QLabel("")
        self._interaction_hint.setObjectName("sectionHint")
        self._interaction_hint.setWordWrap(True)

        interactions_page = QWidget()
        interactions_layout = QVBoxLayout(interactions_page)
        interactions_layout.setContentsMargins(0, 0, 0, 0)
        interactions_layout.setSpacing(4)
        interactions_layout.addWidget(self._interaction_table, 1)
        interactions_layout.addWidget(self._interaction_hint)

        self._tabs.addTab(self._table, "Poses")
        self._tabs.addTab(interactions_page, "Interactions")
        layout.addWidget(self._tabs, 1)

        layout.addWidget(Divider())

        export_row = QHBoxLayout()
        self._export_best = QPushButton("Export Best Pose")
        self._export_all = QPushButton("Export All Poses")
        self._export_csv = QPushButton("Export CSV")
        self._export_report = QPushButton("Export Report")
        for button, slot in (
            (self._export_best, self.export_best_requested),
            (self._export_all, self.export_all_requested),
            (self._export_csv, self.export_csv_requested),
            (self._export_report, self.export_report_requested),
        ):
            button.clicked.connect(slot.emit)
            export_row.addWidget(button)

        tooltips = {
            self._export_best: ("Save the lowest-energy pose as a PDB file "
                                "(a PDBQT copy is written next to it)."),
            self._export_all: ("Copy every pose, the CSV table and the run "
                               "report into a folder you choose."),
            self._export_csv: "Save the pose table (pose, affinity, RMSD) as CSV.",
            self._export_report: ("Save a readable report with the receptor, the "
                                  "box, every engine setting, all poses and the "
                                  "interactions of the selected pose."),
        }
        for button, tip in tooltips.items():
            button.setToolTip(tip)
        layout.addLayout(export_row)

        self._refresh_enabled()

    def _build_pose_table(self) -> QTableWidget:
        table = QTableWidget(0, len(POSE_COLUMNS))
        table.setHorizontalHeaderLabels(POSE_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(150)
        table.itemSelectionChanged.connect(self._on_selection_changed)
        return table

    def _build_interaction_table(self) -> QTableWidget:
        table = QTableWidget(0, len(INTERACTION_COLUMNS))
        table.setHorizontalHeaderLabels(INTERACTION_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(140)
        return table

    # ---------------------------------------------------------- public API
    @property
    def result(self) -> DockingResult | None:
        """The result currently displayed, or ``None``."""
        return self._result

    @property
    def interactions(self) -> list[Interaction]:
        """Interactions currently listed (those of the selected pose)."""
        return list(self._interactions)

    def set_result(self, result: DockingResult) -> None:
        """Populate the table from ``result`` and select the best pose."""
        self._result = result
        self._table.blockSignals(True)
        self._table.setRowCount(len(result.poses))
        for row, pose in enumerate(result.poses):
            values = (
                str(pose.pose),
                f"{pose.affinity:.3f}",
                f"{pose.rmsd_lb:.3f}",
                f"{pose.rmsd_ub:.3f}",
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 1:
                    item.setToolTip("More negative = stronger predicted binding")
                self._table.setItem(row, column, item)
        self._table.blockSignals(False)

        best = result.best_affinity
        self._affinity_value.setText(f"{best:.2f} kcal/mol" if best is not None else "-")
        self._engine_label.setText(result.engine)

        self.set_interactions([], None)
        if result.poses:
            self._table.selectRow(0)
        self._refresh_enabled()
        log.info("displayed %d poses, best affinity %s", len(result.poses), best)

    def set_interactions(
        self,
        items: list[Interaction] | tuple[Interaction, ...],
        pose_number: int | None = None,
    ) -> None:
        """Show the interactions belonging to the selected pose."""
        self._interactions = list(items)
        table = self._interaction_table
        table.setRowCount(len(self._interactions))
        for row, item in enumerate(self._interactions):
            values = item.to_row()
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if column == 0:
                    cell.setForeground(Qt.GlobalColor.white)
                    color = KIND_COLORS.get(item.kind)
                    if color:
                        cell.setToolTip(item.kind_label)
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, column, cell)
        if self._interactions:
            table.selectRow(0)
            table.clearSelection()

        counts = group_counts(self._interactions)
        self._tabs.setTabText(
            1, f"Interactions ({len(self._interactions)})" if self._interactions
            else "Interactions"
        )
        if pose_number is None:
            pose_text = ""
        else:
            pose_text = f" of pose {pose_number}"
        if not self._interactions:
            hint = (f"No receptor-ligand contact within 4.5 A{pose_text}."
                    if self._result is not None else
                    "Run a docking to see the interactions of a pose.")
        else:
            detail = ", ".join(
                f"{count}x {KIND_LABELS.get(kind, kind)}"
                for kind, count in counts.items()
            )
            hint = f"{len(self._interactions)} possible interaction(s){pose_text}: {detail}."
        self._interaction_hint.setText(hint)

    def clear(self) -> None:
        """Empty the tables and reset the affinity display."""
        self._result = None
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        self._table.blockSignals(False)
        self.set_interactions([], None)
        self._affinity_value.setText("-")
        self._engine_label.setText("")
        self._refresh_enabled()

    def selected_pose(self) -> PoseResult | None:
        """The pose belonging to the selected row."""
        if self._result is None:
            return None
        row = self._table.currentRow()
        if row < 0 or row >= len(self._result.poses):
            return None
        return self._result.poses[row]

    def select_pose(self, pose_number: int) -> None:
        """Select the row belonging to ``pose_number`` (1-based)."""
        if self._result is None:
            return
        for row, pose in enumerate(self._result.poses):
            if pose.pose == pose_number:
                self._table.selectRow(row)
                return

    def show_interactions_tab(self, show: bool = True) -> None:
        """Switch the tab widget to the interaction list (used by the viewer)."""
        self._tabs.setCurrentIndex(1 if show else 0)

    def current_tab(self) -> int:
        """Index of the visible tab (``0`` poses, ``1`` interactions)."""
        return self._tabs.currentIndex()

    # ------------------------------------------------------------ internals
    def _on_selection_changed(self) -> None:
        pose = self.selected_pose()
        if pose is not None:
            self.status_message.emit(
                f"Pose {pose.pose}: {pose.affinity:.2f} kcal/mol"
            )
            self.pose_selected.emit(pose.pose)

    def _refresh_enabled(self) -> None:
        has_result = self._result is not None and bool(self._result.poses)
        for button in (self._export_best, self._export_all,
                       self._export_csv, self._export_report):
            button.setEnabled(has_result)


__all__ = ["ResultPanel"]
