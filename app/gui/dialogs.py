"""User facing message dialogs.

Requirement #11 of the specification: never show a bare traceback.  Every
error dialog leads with a sentence a bench scientist can act on and offers a
``Show Technical Details`` button with the full traceback / stderr.
"""

from __future__ import annotations

import traceback
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..utils.logger import get_logger

log = get_logger(__name__)


def _format_details(details: str | BaseException | None) -> str:
    """Turn an exception (or string) into readable technical text."""
    if details is None:
        return ""
    if isinstance(details, BaseException):
        return "".join(
            traceback.format_exception(type(details), details, details.__traceback__)
        ).strip()
    return str(details)


class TechnicalDetailsDialog(QDialog):
    """Small dialog that reveals the full technical cause of an error."""

    def __init__(self, title: str, details: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Technical details (for bug reports and debugging):"))

        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(details)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(view, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = QPushButton("Copy to clipboard")
        copy.clicked.connect(self._copy)
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _copy(self) -> None:
        view = self.findChild(QPlainTextEdit)
        if view is not None:
            QApplication.clipboard().setText(view.toPlainText())


def show_error(
    parent: QWidget | None,
    message: str,
    details: str | BaseException | None = None,
    title: str = "EnzyDock",
) -> None:
    """Show a friendly error box with an optional technical-details dialog."""
    technical = _format_details(details)
    log.error("%s | %s", message, technical.replace("\n", " | ") if technical else "")

    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setText(message)

    if technical:
        box.setDetailedText(technical)
        details_button = box.addButton("Show Technical Details",
                                       QMessageBox.ButtonRole.ActionRole)
        details_button.clicked.connect(
            lambda: TechnicalDetailsDialog(title, technical, parent).exec()
        )
    box.addButton(QMessageBox.StandardButton.Ok)
    box.exec()


class ReportDialog(QDialog):
    """Read-only text report with a *Copy to clipboard* button.

    Used by the diagnostics so a user can paste the whole report into a bug
    report instead of retyping it.
    """

    def __init__(self, title: str, text: str, intro: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        if intro:
            label = QLabel(intro)
            label.setWordWrap(True)
            layout.addWidget(label)

        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(view, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = QPushButton("Copy to clipboard")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def show_report(parent: QWidget | None, title: str, text: str,
                intro: str = "") -> None:
    """Show a copyable plain-text report."""
    ReportDialog(title, text, intro, parent).exec()


def show_warning(parent: QWidget | None, message: str, title: str = "EnzyDock") -> None:
    """Show a non-fatal warning box."""
    log.warning(message)
    QMessageBox.warning(parent, title, message)


def show_info(parent: QWidget | None, message: str, title: str = "EnzyDock") -> None:
    """Show an informational box."""
    log.info(message)
    QMessageBox.information(parent, title, message)


def ask_confirmation(
    parent: QWidget | None,
    message: str,
    title: str = "Please confirm",
) -> bool:
    """Return ``True`` when the user clicks Yes."""
    answer = QMessageBox.question(
        parent,
        title,
        message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def join_messages(messages: Iterable[str]) -> str:
    """Join validation messages into one readable paragraph."""
    items = [m.strip() for m in messages if m and m.strip()]
    return "\n".join(f"- {item}" for item in items)
