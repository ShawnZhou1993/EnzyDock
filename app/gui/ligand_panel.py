"""Ligand / substrate input panel.

Two input routes are supported:

1. **SMILES** - typed (or pasted) into the text field; parsed and described
   immediately so the user gets instant feedback about the molecule.
2. **File** - ``.sdf`` / ``.mol`` / ``.mol2`` / ``.pdb`` read with RDKit.

Like :class:`~app.gui.protein_panel.ProteinPanel`, this widget only collects
intent and emits signals; 3D embedding, optimisation and PDBQT conversion are
triggered by the controller in Step 3.
"""

from __future__ import annotations

from pathlib import Path

from rdkit import Chem

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..chemistry.ligand import (
    SUPPORTED_LIGAND_SUFFIXES,
    LigandError,
    LigandSummary,
    molecule_from_file,
    molecule_from_smiles,
    summarize_molecule,
)
from ..utils.logger import get_logger
from ..utils.settings import AppSettings
from .dialogs import show_error, show_warning
from .widgets import Divider, InfoGrid, section_hint

log = get_logger(__name__)

FILE_FILTER = (
    "Ligand files (*.sdf *.sd *.mol *.mol2 *.pdb);;"
    "MDL files (*.sdf *.sd *.mol);;Tripos mol2 (*.mol2);;PDB (*.pdb);;All files (*.*)"
)

EXAMPLE_SMILES = "CC(=O)Oc1ccccc1C(=O)O"       # aspirin, a friendly default


class LigandPanel(QGroupBox):
    """Left-hand 'Ligand / Substrate' section of the main window."""

    #: ``(smiles)`` - user asked for a 3D structure from SMILES.
    generate_3d_requested = Signal(str)
    #: ``(path)`` - a ligand file was loaded and described successfully.
    file_loaded = Signal(object)
    #: ``(summary)`` - a molecule is available (from either route).
    ligand_changed = Signal(object)
    #: ``(source, payload)`` - user pressed *Prepare Ligand*.
    prepare_requested = Signal(str, object)
    status_message = Signal(str)

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__("Ligand / Substrate", parent)
        self.setObjectName("panel")
        self._settings = settings
        self._summary: LigandSummary | None = None
        self._mol: Chem.Mol | None = None
        self._prepared: object | None = None
        self._busy = False
        #: Where the current molecule came from: ``("smiles"|"file", value)``.
        self._source: tuple[str, str] = ("smiles", "")

        self._build_ui()
        self._refresh_enabled()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- route 1: SMILES -------------------------------------------------
        smiles_label = QLabel("SMILES")
        smiles_label.setObjectName("muted")
        layout.addWidget(smiles_label)

        smiles_row = QHBoxLayout()
        self._smiles_edit = QLineEdit()
        self._smiles_edit.setPlaceholderText(f"e.g. {EXAMPLE_SMILES}")
        self._smiles_edit.returnPressed.connect(self._on_generate_3d)
        smiles_row.addWidget(self._smiles_edit, 1)

        self._generate_btn = QPushButton("Generate 3D")
        self._generate_btn.clicked.connect(self._on_generate_3d)
        smiles_row.addWidget(self._generate_btn)
        layout.addLayout(smiles_row)

        self._paste_example = QPushButton("Use example (aspirin)")
        self._paste_example.setFlat(True)
        self._paste_example.setStyleSheet("text-align: left; padding: 0px;")
        self._paste_example.clicked.connect(self._fill_example)
        layout.addWidget(self._paste_example)

        layout.addWidget(Divider())

        # --- route 2: file ----------------------------------------------------
        # (no caption row: the button label already says what it does)
        file_row = QHBoxLayout()
        self._load_btn = QPushButton("Load Ligand File")
        self._load_btn.clicked.connect(self.select_file)
        file_row.addWidget(self._load_btn)

        self._file_name = QLabel("No file loaded")
        self._file_name.setObjectName("muted")
        self._file_name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        file_row.addWidget(self._file_name, 1)
        layout.addLayout(file_row)

        layout.addWidget(section_hint(
            "Or load a file: .sdf, .mol, .mol2 or .pdb."
        ))

        self._info = InfoGrid(self)
        self._info.show_placeholder("Enter a SMILES string or load a file.")
        layout.addWidget(self._info)

        self._warning_label = QLabel("")
        self._warning_label.setObjectName("sectionHint")
        self._warning_label.setWordWrap(True)
        self._warning_label.hide()
        layout.addWidget(self._warning_label)

        layout.addWidget(Divider())

        self._prepare_btn = QPushButton("Prepare Ligand")
        self._prepare_btn.clicked.connect(self._on_prepare)
        layout.addWidget(self._prepare_btn)

    # ---------------------------------------------------------- public API
    @property
    def summary(self) -> LigandSummary | None:
        """Description of the current molecule, or ``None``."""
        return self._summary

    @property
    def molecule(self) -> Chem.Mol | None:
        """The RDKit molecule currently selected (never mutated by the GUI)."""
        return self._mol

    @property
    def smiles(self) -> str:
        """SMILES currently typed by the user (stripped)."""
        return self._smiles_edit.text().strip()

    @property
    def source(self) -> tuple[str, str]:
        """``(kind, value)`` describing where the molecule came from.

        ``kind`` is ``"smiles"`` for a typed string or ``"file"`` for a loaded
        structure file; the docking pipeline uses it to know what to prepare.
        """
        return self._source

    def set_source(self, kind: str, value: str) -> None:
        """Record the origin of the current molecule."""
        self._source = (kind, value)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_enabled()

    def set_smiles(self, smiles: str) -> bool:
        """Parse ``smiles`` and display its properties. ``True`` on success."""
        try:
            mol = molecule_from_smiles(smiles)
        except LigandError as exc:
            show_error(self, exc.message, exc.details, "Invalid SMILES")
            return False

        try:
            summary = summarize_molecule(mol, source="SMILES", smiles=smiles)
        except LigandError as exc:
            show_error(self, exc.message, exc.details, "Could not analyse molecule")
            return False

        self._mol = mol
        self._source = ("smiles", smiles)
        self._apply_summary(summary)
        self._smiles_edit.setText(smiles)          # keep field and state in sync
        self._file_name.setText("No file loaded")
        log.info("ligand from SMILES: %s (%s)", summary.smiles, summary.formula)
        self.status_message.emit(f"Ligand: {summary.formula}, {summary.molecular_weight:.1f} g/mol")
        self.ligand_changed.emit(summary)
        return True

    def load_file(self, path: str | Path) -> bool:
        """Read a ligand file and display its properties."""
        path = Path(path)
        try:
            mol = molecule_from_file(path)
        except LigandError as exc:
            show_error(self, exc.message, exc.details, "Could not read the ligand file")
            return False
        except Exception as exc:                       # pragma: no cover
            show_error(self, "An unexpected problem occurred while reading this ligand file.",
                       exc, "Could not read the ligand file")
            return False

        try:
            summary = summarize_molecule(mol, name=path.stem, source=path.name)
        except LigandError as exc:
            show_error(self, exc.message, exc.details, "Could not analyse molecule")
            return False

        self._mol = mol
        self._source = ("file", str(path))
        self._apply_summary(summary)
        self._file_name.setText(path.name)
        self._file_name.setToolTip(str(path))
        self._smiles_edit.blockSignals(True)
        self._smiles_edit.setText(summary.smiles)
        self._smiles_edit.blockSignals(False)
        self._settings.remember_input_dir(path)

        log.info("ligand file loaded: %s (%s)", path.name, summary.formula)
        self.status_message.emit(
            f"Loaded {path.name}: {summary.formula}, {summary.molecular_weight:.1f} g/mol"
        )
        self.file_loaded.emit(path)
        self.ligand_changed.emit(summary)
        return True

    @property
    def prepared(self) -> object | None:
        """The :class:`PreparedLigand` produced by the last preparation run."""
        return self._prepared

    def apply_molecule(self, mol: Chem.Mol, name: str | None = None,
                       source: str | None = None) -> bool:
        """Replace the displayed molecule (used after 3D generation)."""
        try:
            summary = summarize_molecule(
                mol,
                name=name or (self._summary.name if self._summary else "ligand"),
                source=source or (self._summary.source if self._summary else "SMILES"),
            )
        except LigandError as exc:
            show_error(self, exc.message, exc.details, "Could not analyse molecule")
            return False
        self._mol = mol
        self._apply_summary(summary)
        return True

    def show_prepared(self, prepared: object) -> None:
        """Present the outcome of ligand preparation (torsions, PDBQT path ...).

        ``prepared`` is a :class:`~app.chemistry.preparation.PreparedLigand`;
        it is typed loosely here so the GUI module does not have to import the
        chemistry layer just for a type annotation.
        """
        self._prepared = prepared
        if getattr(prepared, "mol", None) is not None:
            self.apply_molecule(prepared.mol)
        self._info.set_rows(prepared.to_rows())

        warnings = list(getattr(prepared, "warnings", []))
        pdbqt = getattr(prepared, "pdbqt_path", None)
        suffix = f"  Docking input: {pdbqt.name}" if pdbqt else ""
        if warnings:
            self._warning_label.setText("Note: " + " ".join(warnings) + suffix)
            self._warning_label.show()
        elif suffix:
            self._warning_label.setText(suffix.strip())
            self._warning_label.show()
        else:
            self._warning_label.hide()

    # ------------------------------------------------------------- actions
    def select_file(self) -> None:
        start_dir = str(self._settings.last_input_dir())
        path, _ = QFileDialog.getOpenFileName(self, "Select ligand file",
                                              start_dir, FILE_FILTER)
        if path:
            self.load_file(path)

    def _on_generate_3d(self) -> None:
        """Parse the SMILES, show its properties and request a 3D structure."""
        if not self.smiles:
            show_warning(self, "Please type a SMILES string first.")
            return
        if not self.set_smiles(self.smiles):
            return
        self.generate_3d_requested.emit(self.smiles)

    def _on_prepare(self) -> None:
        """Ask the controller to prepare whichever ligand is active."""
        if self._summary is None:
            show_warning(self, "Please provide a ligand first (SMILES or file).")
            return
        source = "file" if self._summary.source != "SMILES" else "smiles"
        payload = self.smiles if source == "smiles" else self._file_name.text()
        self.prepare_requested.emit(source, payload)

    def _fill_example(self) -> None:
        self._smiles_edit.setText(EXAMPLE_SMILES)
        self.set_smiles(EXAMPLE_SMILES)

    # ------------------------------------------------------------ internals
    def _apply_summary(self, summary: LigandSummary) -> None:
        self._summary = summary
        self._info.set_rows(summary.to_rows())
        if summary.warnings:
            self._warning_label.setText("Note: " + " ".join(summary.warnings))
            self._warning_label.show()
        else:
            self._warning_label.hide()
        self._refresh_enabled()

    def _refresh_enabled(self) -> None:
        self._smiles_edit.setEnabled(not self._busy)
        self._generate_btn.setEnabled(not self._busy)
        self._load_btn.setEnabled(not self._busy)
        self._prepare_btn.setEnabled(not self._busy and self._summary is not None)
        self._paste_example.setEnabled(not self._busy)


__all__ = ["LigandPanel", "FILE_FILTER", "SUPPORTED_LIGAND_SUFFIXES", "EXAMPLE_SMILES"]
