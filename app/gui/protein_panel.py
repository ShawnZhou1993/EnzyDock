"""Protein / receptor input panel.

Responsibilities
----------------
* select a ``.pdb`` / ``.cif`` / ``.mmcif`` file,
* show what is inside it (chains, residues, atoms, waters, HETATM, metals),
* let the user choose preparation options and chains,
* emit :attr:`prepare_requested` so the *controller* (main window) can run the
  preparation on a worker thread.

The panel never prepares anything itself: it only collects intent.  This keeps
the computational backend completely independent of Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..chemistry.protein import (
    SUPPORTED_SUFFIXES,
    StructureParseError,
    StructureSummary,
    summarize_structure,
)
from ..chemistry.receptor import ReceptorOptions
from ..utils.logger import get_logger
from ..utils.settings import AppSettings
from .dialogs import show_error
from .widgets import Divider, InfoGrid, section_hint

log = get_logger(__name__)

FILE_FILTER = (
    "Protein structure files (*.pdb *.cif *.mmcif *.ent);;"
    "PDB files (*.pdb *.ent);;mmCIF files (*.cif *.mmcif);;All files (*.*)"
)


@dataclass
class ProteinOptions:
    """Preparation choices made in the panel (passed to the controller)."""

    remove_waters: bool = True
    remove_hetero: bool = True
    add_hydrogens: bool = True
    keep_chains: list[str] | None = None     # None => every chain

    def to_receptor_options(self) -> ReceptorOptions:
        """Translate the GUI choices into a backend :class:`ReceptorOptions`."""
        return ReceptorOptions(
            remove_waters=self.remove_waters,
            remove_hetero=self.remove_hetero,
            add_hydrogens=self.add_hydrogens,
            keep_chains=list(self.keep_chains) if self.keep_chains else None,
        )


class ProteinPanel(QGroupBox):
    """Left-hand 'Protein' section of the main window."""

    #: ``(path)`` - a structure file was loaded and summarised successfully.
    protein_loaded = Signal(object)
    #: ``(path, options)`` - the user pressed *Prepare Protein*.
    prepare_requested = Signal(object, object)
    #: ``(str)`` - status message for the status bar / log.
    status_message = Signal(str)

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__("Protein", parent)
        self.setObjectName("panel")
        self._settings = settings
        self._path: Path | None = None
        self._summary: StructureSummary | None = None
        self._prepared: object | None = None
        self._busy = False

        self._build_ui()
        self._load_settings()
        self._refresh_enabled()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- file selection ------------------------------------------------
        file_row = QHBoxLayout()
        self._select_btn = QPushButton("Select Protein")
        self._select_btn.setObjectName("primary")
        self._select_btn.clicked.connect(self.select_file)
        file_row.addWidget(self._select_btn)

        self._file_label = QLabel("No file loaded")
        self._file_label.setObjectName("muted")
        self._file_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        file_row.addWidget(self._file_label, 1)
        layout.addLayout(file_row)

        layout.addWidget(section_hint(
            "Accepts .pdb, .cif and .mmcif. Your original file is never modified."
        ))

        self._info = InfoGrid(self)
        self._info.show_placeholder("Load a structure to see its composition.")
        layout.addWidget(self._info)

        self._warning_label = QLabel("")
        self._warning_label.setObjectName("sectionHint")
        self._warning_label.setWordWrap(True)
        self._warning_label.hide()
        layout.addWidget(self._warning_label)

        layout.addWidget(Divider())

        # --- preparation options -------------------------------------------
        options_label = QLabel("Preparation options")
        options_label.setObjectName("muted")
        layout.addWidget(options_label)

        self._remove_waters = QCheckBox("Remove waters")
        self._remove_hetero = QCheckBox("Remove hetero atoms (HETATM)")
        self._add_hydrogens = QCheckBox("Add hydrogens")
        for box in (self._remove_waters, self._remove_hetero, self._add_hydrogens):
            box.stateChanged.connect(self._on_options_changed)
            layout.addWidget(box)

        # --- chain selection -------------------------------------------------
        chains_header = QHBoxLayout()
        chains_label = QLabel("Chains to keep")
        chains_label.setObjectName("muted")
        chains_header.addWidget(chains_label)
        chains_header.addStretch(1)
        self._select_all_chains = QPushButton("All")
        self._select_all_chains.setFixedWidth(46)
        self._select_all_chains.clicked.connect(lambda: self._set_all_chains(True))
        self._clear_chains = QPushButton("None")
        self._clear_chains.setFixedWidth(52)
        self._clear_chains.clicked.connect(lambda: self._set_all_chains(False))
        chains_header.addWidget(self._select_all_chains)
        chains_header.addWidget(self._clear_chains)
        layout.addLayout(chains_header)

        self._chain_list = QListWidget()
        self._chain_list.setMaximumHeight(70)
        self._chain_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._chain_list.itemChanged.connect(self._on_options_changed)
        layout.addWidget(self._chain_list)

        layout.addWidget(Divider())

        self._prepare_btn = QPushButton("Prepare Protein")
        self._prepare_btn.clicked.connect(self.request_prepare)
        layout.addWidget(self._prepare_btn)

    # ------------------------------------------------------------- settings
    def _load_settings(self) -> None:
        self._remove_waters.setChecked(self._settings.get_bool("protein/remove_waters"))
        self._remove_hetero.setChecked(self._settings.get_bool("protein/remove_hetero"))
        self._add_hydrogens.setChecked(True)

    def _on_options_changed(self) -> None:
        self._settings.set_value("protein/remove_waters", self._remove_waters.isChecked())
        self._settings.set_value("protein/remove_hetero", self._remove_hetero.isChecked())
        self._settings.set_value("protein/keep_chains", ",".join(self.selected_chains()))

    # ---------------------------------------------------------- public API
    @property
    def path(self) -> Path | None:
        """Currently loaded structure file."""
        return self._path

    @property
    def summary(self) -> StructureSummary | None:
        """Statistics of the loaded structure, or ``None``."""
        return self._summary

    @property
    def prepared(self) -> object | None:
        """The :class:`PreparedReceptor` from the last preparation run."""
        return self._prepared

    def show_prepared(self, prepared: object) -> None:
        """Present the outcome of receptor preparation.

        ``prepared`` is a :class:`~app.chemistry.receptor.PreparedReceptor`;
        it is typed loosely so the GUI module does not have to import the
        chemistry layer just for an annotation.
        """
        self._prepared = prepared
        self._info.set_rows(prepared.to_rows())

        warnings = list(getattr(prepared, "warnings", []))
        pdbqt = getattr(prepared, "pdbqt_path", None)
        suffix = f"  Docking receptor: {pdbqt.name}" if pdbqt else ""
        if warnings:
            self._warning_label.setText("Note: " + " ".join(warnings) + suffix)
            self._warning_label.show()
        elif suffix:
            self._warning_label.setText(suffix.strip())
            self._warning_label.show()
        else:
            self._warning_label.hide()

    def options(self) -> ProteinOptions:
        """Return the preparation choices currently shown in the panel."""
        chains = self.selected_chains()
        return ProteinOptions(
            remove_waters=self._remove_waters.isChecked(),
            remove_hetero=self._remove_hetero.isChecked(),
            add_hydrogens=self._add_hydrogens.isChecked(),
            keep_chains=chains if chains else None,
        )

    def selected_chains(self) -> list[str]:
        """Chains ticked in the list widget (empty when nothing is loaded)."""
        chains: list[str] = []
        for index in range(self._chain_list.count()):
            item = self._chain_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                chains.append(item.text())
        return chains

    def center_from_structure(self) -> tuple[float, float, float] | None:
        """Geometric centre of the loaded structure, if available."""
        return self._summary.center if self._summary else None

    def set_busy(self, busy: bool) -> None:
        """Disable the interactive controls while a job is running."""
        self._busy = busy
        self._refresh_enabled()

    def load_file(self, path: str | Path) -> bool:
        """Load and summarise ``path``; return ``True`` on success.

        Shows an error dialog (with technical details) when the file cannot be
        parsed, so callers can simply ignore the return value.
        """
        path = Path(path)
        try:
            summary = summarize_structure(path)
        except StructureParseError as exc:
            show_error(self, str(exc), exc, "Could not read the protein file")
            return False
        except Exception as exc:                      # pragma: no cover
            show_error(
                self,
                "An unexpected problem occurred while reading this protein file.",
                exc,
                "Could not read the protein file",
            )
            return False

        self._path = path
        self._summary = summary
        self._file_label.setText(path.name)
        self._file_label.setToolTip(str(path))
        self._info.set_rows(summary.to_rows())
        self._populate_chains(summary.chains)
        self._settings.remember_input_dir(path)
        self._refresh_enabled()

        log.info("protein loaded: %s (%d atoms, %d chains)",
                 path.name, summary.atom_count, summary.chain_count)
        self.status_message.emit(
            f"Loaded {path.name}: {summary.residue_count:,} residues, "
            f"{summary.atom_count:,} atoms, chains {summary.chain_text}"
        )
        self.protein_loaded.emit(path)
        return True

    # ------------------------------------------------------------- actions
    def select_file(self) -> None:
        """Open the file dialog and load the picked structure."""
        start_dir = str(self._settings.last_input_dir())
        path, _ = QFileDialog.getOpenFileName(self, "Select protein structure",
                                              start_dir, FILE_FILTER)
        if path:
            self.load_file(path)

    def request_prepare(self) -> None:
        """Emit :attr:`prepare_requested` after validating that a file exists."""
        if self._path is None:
            show_error(self, "Please select a protein structure file first.",
                       "protein_panel.request_prepare called without a file")
            return
        if self._summary and not self._summary.chains:
            log.warning("structure has no chain identifiers; preparing everything")
        self.prepare_requested.emit(self._path, self.options())

    # ------------------------------------------------------------ internals
    def _populate_chains(self, chains: Sequence[str]) -> None:
        """Fill the chain list, restoring the previous selection if possible."""
        previous = set(self.selected_chains())
        self._chain_list.blockSignals(True)
        self._chain_list.clear()
        for chain in chains:
            item = QListWidgetItem(chain or "?")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if (not previous or chain in previous)
                else Qt.CheckState.Unchecked
            )
            self._chain_list.addItem(item)
        self._chain_list.blockSignals(False)
        self._on_options_changed()

    def _set_all_chains(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._chain_list.blockSignals(True)
        for index in range(self._chain_list.count()):
            self._chain_list.item(index).setCheckState(state)
        self._chain_list.blockSignals(False)
        self._on_options_changed()

    def _refresh_enabled(self) -> None:
        self._select_btn.setEnabled(not self._busy)
        self._prepare_btn.setEnabled(not self._busy and self._path is not None)
        self._remove_waters.setEnabled(not self._busy)
        self._remove_hetero.setEnabled(not self._busy)
        self._add_hydrogens.setEnabled(not self._busy)
        self._chain_list.setEnabled(not self._busy and self._chain_list.count() > 0)


__all__ = ["ProteinPanel", "ProteinOptions", "FILE_FILTER", "SUPPORTED_SUFFIXES"]
