"""Live log panel plus the logging <-> Qt bridge."""

from __future__ import annotations

import html
import logging
from typing import Iterable

from PySide6.QtCore import QObject, QSize, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

LEVEL_COLORS = {
    logging.DEBUG: "#93a1b1",
    logging.INFO: "#e6edf3",
    logging.WARNING: "#d29922",
    logging.ERROR: "#f85149",
    logging.CRITICAL: "#f85149",
}

_MAX_BLOCKS = 2000          # keep memory bounded during long virtual screens


class QtLogHandler(logging.Handler, QObject):
    """Forward :mod:`logging` records into a Qt widget thread-safely."""

    record_emitted = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        logging.Handler.__init__(self)
        QObject.__init__(self, parent)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.record_emitted.emit(record)
        except RuntimeError:            # widget already destroyed
            pass


class LogPanel(QWidget):
    """Read-only, auto-scrolling log view shown at the bottom of the window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._handler = QtLogHandler(self)
        self._handler.record_emitted.connect(self._append_record)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("Activity log")
        title.setObjectName("muted")
        header.addWidget(title)
        header.addStretch(1)

        self._auto_scroll = QCheckBox("Auto-scroll")
        self._auto_scroll.setChecked(True)
        header.addWidget(self._auto_scroll)

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(70)
        clear_btn.clicked.connect(self.clear)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(_MAX_BLOCKS)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._view.setPlaceholderText("Preparation and docking messages appear here ...")
        layout.addWidget(self._view, 1)

    # -- public API --------------------------------------------------------
    def handler(self) -> QtLogHandler:
        """Return the handler that must be attached to a logger."""
        return self._handler

    def attach(self, logger: logging.Logger) -> None:
        """Start mirroring ``logger`` (and its children) into this panel."""
        logger.addHandler(self._handler)

    def detach(self, logger: logging.Logger) -> None:
        logger.removeHandler(self._handler)

    def append_message(self, text: str, level: int = logging.INFO) -> None:
        """Append a plain message with ``level`` colouring."""
        color = LEVEL_COLORS.get(level, "#e6edf3")
        safe = html.escape(text)
        self._view.appendHtml(f'<span style="color:{color}">{safe}</span>')
        self._maybe_scroll()

    def append_lines(self, lines: Iterable[str], level: int = logging.INFO) -> None:
        for line in lines:
            self.append_message(line, level)

    def clear(self) -> None:
        self._view.clear()

    # -- internals ---------------------------------------------------------
    def _append_record(self, record: logging.LogRecord) -> None:
        color = LEVEL_COLORS.get(record.levelno, "#e6edf3")
        message = html.escape(record.getMessage())
        prefix = f"[{record.levelname.lower():>7}] " if record.levelno >= logging.WARNING else ""
        self._view.appendHtml(f'<span style="color:{color}">{prefix}{message}</span>')
        self._maybe_scroll()

    def _maybe_scroll(self) -> None:
        if self._auto_scroll.isChecked():
            self._view.verticalScrollBar().setValue(
                self._view.verticalScrollBar().maximum()
            )

    def sizeHint(self):  # noqa: D102 - Qt override
        return QSize(600, 150)
