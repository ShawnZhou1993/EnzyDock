"""Generic background worker for long running computational steps.

Requirement #7 of the specification is "never block the GUI".  Rather than
writing a bespoke ``QThread`` per step, EnzyDock uses one small wrapper: the
GUI hands it a *plain callable* from the chemistry / docking layer, and gets
its result back on the main thread through Qt signals.

That keeps the layering intact: Qt lives here, science lives in
:mod:`app.chemistry` and :mod:`app.docking`, and neither knows about the other.

Typical use::

    worker = FunctionWorker(prepare_from_smiles, smiles,
                            output_dir=workspace / "prepared",
                            title="Ligand preparation")
    worker.progress.connect(self.set_progress)
    worker.succeeded.connect(self._on_ligand_ready)
    worker.failed.connect(lambda msg, det: show_error(self, msg, det))
    worker.finished.connect(lambda: self.set_busy(False))
    worker.start()
"""

from __future__ import annotations

import inspect
import traceback
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from ..utils.logger import get_logger

log = get_logger(__name__)

#: Signature of the progress callback accepted by backend callables.
ProgressFn = Callable[[int, str], None]


def describe_exception(exc: BaseException, title: str) -> tuple[str, str]:
    """Split an exception into ``(user_message, technical_details)``.

    EnzyDock's own error classes (``LigandError``, ``ConversionError``,
    ...) already carry a friendly ``message`` and technical ``details``; those
    are passed through untouched.  Anything else - a genuine bug - is wrapped
    so the user never sees a raw traceback as the primary text.
    """
    message = getattr(exc, "message", None)
    details = getattr(exc, "details", None)
    if isinstance(message, str) and message:
        technical = details if isinstance(details, str) and details else str(exc)
        return message, technical
    return (
        f"{title} failed because of an unexpected error. "
        "Please use 'Show Technical Details' when reporting this.",
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip(),
    )


def _accepts_keyword(func: Callable[..., Any], keyword: str) -> bool:
    """Return ``True`` when ``func`` can take ``keyword``."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):                    # pragma: no cover
        return False
    if keyword in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


class FunctionWorker(QThread):
    """Run ``func(*args, **kwargs)`` on a worker thread and report back.

    Signals
    -------
    progress(percent, message)
        Emitted whenever the callable calls its ``progress`` callback.
    succeeded(result)
        Emitted with the callable's return value.
    failed(message, details)
        Emitted with a user friendly message and the technical cause.
    """

    progress = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(
        self,
        func: Callable[..., Any],
        *args: Any,
        title: str = "Background task",
        pass_progress: bool = True,
        parent: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent)
        self._func = func
        self._args = args
        self._kwargs = kwargs
        self._title = title
        self._pass_progress = pass_progress
        self.result: Any = None
        self.error: BaseException | None = None

    # ------------------------------------------------------------ QThread
    def run(self) -> None:                             # noqa: D102 - Qt override
        log.debug("worker starting: %s", self._title)
        try:
            kwargs = dict(self._kwargs)
            if self._pass_progress and _accepts_keyword(self._func, "progress"):
                kwargs["progress"] = self._report_progress
            self.result = self._func(*self._args, **kwargs)
        except BaseException as exc:                   # noqa: BLE001 - reported
            self.error = exc
            log.error("%s failed: %s", self._title, exc)
            message, details = describe_exception(exc, self._title)
            self.failed.emit(message, details)
            return
        log.debug("worker finished: %s", self._title)
        self.succeeded.emit(self.result)

    # ------------------------------------------------------------ helpers
    def _report_progress(self, percent: int, message: str = "") -> None:
        """Forward a backend progress callback onto the GUI thread."""
        self.progress.emit(int(percent), str(message))

    def run_synchronously(self) -> Any:
        """Execute the callable inline (used by tests and the CLI).

        Raises the original exception so tests can assert on the real type.
        """
        return self._func(*self._args, **self._kwargs)


__all__ = ["FunctionWorker", "describe_exception", "ProgressFn"]
