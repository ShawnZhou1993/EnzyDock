"""Small reusable widgets shared by the input panels.

(Not part of the original file list, but both the protein and the ligand panel
need an identical "label: value" grid, so it lives here instead of being
duplicated.)
"""

from __future__ import annotations

from typing import Iterable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .styles import COLOR_BORDER, COLOR_TEXT_MUTED


class InfoGrid(QWidget):
    """Two column ``label: value`` grid used for structure statistics."""

    def __init__(self, parent: QWidget | None = None, columns: int = 2) -> None:
        super().__init__(parent)
        self._columns = max(1, columns)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(3)
        self._grid.setColumnStretch(1, 1)
        self._labels: dict[str, QLabel] = {}
        self._placeholder: QLabel | None = None

    def set_rows(self, rows: Sequence[tuple[str, str]]) -> None:
        """Replace the whole grid with ``(label, value)`` pairs."""
        self.clear_rows()
        for index, (label, value) in enumerate(rows):
            row, column = divmod(index, self._columns)
            column *= 2
            key_label = QLabel(f"{label}:")
            key_label.setObjectName("muted")
            key_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            value_label = QLabel(value)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            value_label.setWordWrap(False)
            self._grid.addWidget(key_label, row, column)
            self._grid.addWidget(value_label, row, column + 1)
            self._labels[label] = value_label
        self._placeholder = None

    def set_value(self, label: str, value: str) -> None:
        """Update a single row (no-op when the row does not exist)."""
        widget = self._labels.get(label)
        if widget is not None:
            widget.setText(value)

    def clear_rows(self) -> None:
        """Remove every row from the grid."""
        for label in list(self._labels):
            widget = self._labels.pop(label)
            self._grid.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def show_placeholder(self, text: str) -> None:
        """Show a single muted line, used before anything is loaded."""
        self.clear_rows()
        placeholder = QLabel(text)
        placeholder.setObjectName("muted")
        self._grid.addWidget(placeholder, 0, 0)
        self._placeholder = placeholder


class Divider(QFrame):
    """Thin horizontal separator."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.HLine)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setStyleSheet(f"color: {COLOR_BORDER};")
        self.setFixedHeight(1)


class LabeledBlock(QWidget):
    """A titled sub-section: bold caption plus muted hint line."""

    def __init__(self, title: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        caption = QLabel(title)
        caption.setStyleSheet(f"font-weight: 600; color: {COLOR_TEXT_MUTED};")
        layout.addWidget(caption)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("sectionHint")
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)


def section_hint(text: str) -> QLabel:
    """Return a muted, wrapped hint label."""
    label = QLabel(text)
    label.setObjectName("sectionHint")
    label.setWordWrap(True)
    return label


def set_enabled(widgets: Iterable[QWidget], enabled: bool) -> None:
    """Enable/disable a group of widgets in one call."""
    for widget in widgets:
        widget.setEnabled(enabled)
