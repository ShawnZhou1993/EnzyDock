"""Central logging configuration for EnzyDock.

The module is dependency-free (standard library only) so it can be imported
from the computational backend as well as the GUI.  A Qt bridge that forwards
records into a :class:`~PySide6.QtWidgets.QWidget` lives in
:mod:`app.gui.log_widget` to keep this module free of Qt imports.

Usage::

    from app.utils.logger import get_logger

    log = get_logger(__name__)
    log.info("receptor prepared: %s", path)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import paths

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"
DATE_FORMAT = "%H:%M:%S"

_CONFIGURED = False
_log_file: Path | None = None


def default_log_file() -> Path:
    """Return the rotating log file location (created lazily)."""
    today = datetime.now().strftime("%Y-%m-%d")
    return paths.user_data_dir(create=False) / "logs" / f"enzydock_{today}.log"


def setup_logging(level: int = logging.INFO, log_file: Path | str | None = None) -> Path:
    """Configure the root ``enzydock`` logger.

    Parameters
    ----------
    level:
        Console verbosity.  The file handler always records ``DEBUG``.
    log_file:
        Override the log file path.  ``None`` uses :func:`default_log_file`.

    Returns
    -------
    Path
        The log file that is being written to.
    """
    global _CONFIGURED, _log_file

    target = Path(log_file) if log_file else default_log_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    _log_file = target

    root = logging.getLogger("enzydock")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    # Remove handlers from a previous configuration (tests, reloads).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        target, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(file_handler)

    _CONFIGURED = True
    return target


def get_logger(name: str) -> logging.Logger:
    """Return a logger nested under the ``enzydock`` namespace.

    ``app.gui.main_window`` becomes ``enzydock.gui.main_window`` so a single
    root logger controls formatting and handlers for the whole application.
    """
    if not _CONFIGURED:
        setup_logging()

    if name.startswith("enzydock"):
        full = name
    elif name.startswith("app."):
        full = "enzydock." + name.split(".", 1)[1]
    elif name == "app":
        full = "enzydock"
    else:
        full = f"enzydock.{name}"
    return logging.getLogger(full)


def current_log_file() -> Path | None:
    """Return the active log file, or ``None`` before :func:`setup_logging`."""
    return _log_file
