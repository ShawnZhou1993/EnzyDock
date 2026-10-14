"""Abstract docking engine interface.

The GUI must never talk to a concrete engine.  It only knows this interface,
so adding QVina2 / Smina / GNINA / an AI rescoring backend later means adding
a new subclass and registering it -- no widget has to change.

Contract
--------
A backend must

1. subclass :class:`DockingEngine`,
2. expose :attr:`name`, :meth:`is_available` and :meth:`run`,
3. emit ``progress`` signals (0-100 plus a human readable message),
4. support cooperative cancellation through :meth:`cancel`,
5. raise :class:`EngineError` with a **user readable** message.
"""

from __future__ import annotations

import abc
import inspect
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtCore import QObject, Signal

from .docking_config import BoxConfig, DockingParameters, DockingResult


class EngineError(RuntimeError):
    """Raised for any backend failure that the user must be told about.

    ``message`` is written for a bench scientist, ``details`` carries the
    technical cause (traceback, raw stderr) for the "Show Technical Details"
    dialog.
    """

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class _EngineMeta(type(QObject), type(abc.ABC)):
    """Combine Shiboken's metaclass with :class:`abc.ABCMeta`.

    PySide6's ``QObject`` uses its own metaclass, which conflicts with
    ``ABCMeta``; deriving from both requires an explicitly merged metaclass.
    """


class DockingEngine(QObject, abc.ABC, metaclass=_EngineMeta):
    """Base class for every docking backend.

    Subclasses are ``QObject`` based so that they can expose Qt signals and be
    moved onto a worker thread without blocking the GUI.
    """

    #: Emitted with ``(percent, message)``.  ``percent`` may be ``-1`` for an
    #: indeterminate step.
    progress = Signal(int, str)
    #: Emitted for every line of raw engine output (goes to the log panel).
    log_line = Signal(str)
    #: Emitted once the run finished successfully.
    finished = Signal(object)      # DockingResult
    #: Emitted when the run failed or was cancelled.
    failed = Signal(str, str)      # user message, technical details

    #: Registry of concrete engines, filled by :meth:`register`.
    _registry: dict[str, type["DockingEngine"]] = {}

    # -- registry ----------------------------------------------------------
    @classmethod
    def register(cls, engine_cls: type["DockingEngine"]) -> type["DockingEngine"]:
        """Register a concrete engine class under its :attr:`name`."""
        cls._registry[engine_cls.name] = engine_cls
        return engine_cls

    @classmethod
    def available_engines(cls) -> list[str]:
        """Names of all registered engines."""
        return sorted(cls._registry)

    @classmethod
    def create(cls, name: str, **kwargs: object) -> "DockingEngine":
        """Instantiate a registered engine by name.

        Keyword arguments are forwarded to the engine constructor when it
        accepts them (``settings=``, ``executable=``, ...).  Engines that do
        not know a keyword simply ignore it, so the GUI can pass the same
        arguments for every backend.

        Raises
        ------
        EngineError
            When ``name`` is unknown.
        """
        try:
            engine_cls = cls._registry[name]
        except KeyError:
            raise EngineError(
                f"Docking engine '{name}' is not available.",
                f"Registered engines: {', '.join(cls.available_engines()) or 'none'}",
            ) from None
        return engine_cls(**cls._accepted_kwargs(engine_cls, kwargs))

    @staticmethod
    def _accepted_kwargs(engine_cls: type, kwargs: dict[str, object]) -> dict[str, object]:
        """Drop keywords the engine's ``__init__`` cannot accept.

        Without this, adding an argument for one backend would break every
        other backend.  ``**kwargs`` in the signature means "forward all".
        """
        if not kwargs:                                     # nothing to filter
            return {}
        try:
            parameters = inspect.signature(engine_cls.__init__).parameters
        except (TypeError, ValueError):                    # pragma: no cover
            return {}
        for parameter in parameters.values():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return dict(kwargs)
        return {key: value for key, value in kwargs.items() if key in parameters}

    # -- interface ---------------------------------------------------------
    #: Human readable engine name shown in the GUI combo box.
    name: str = "abstract"

    @abc.abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return ``(ok, reason)``.

        ``reason`` is a short, user readable explanation when the engine
        binary cannot be found, e.g. "the bundled Vina was not found next
        to the application".
        """

    @abc.abstractmethod
    def run(
        self,
        receptor: Path | str,
        ligand: Path | str,
        box: BoxConfig,
        parameters: DockingParameters,
        output_dir: Path | str,
        progress: Callable[[int, str], None] | None = None,
    ) -> DockingResult:
        """Run docking and return a :class:`DockingResult`.

        Parameters
        ----------
        output_dir:
            ``<workspace>/docking`` - the engine writes its configuration,
            log and output PDBQT there.
        progress:
            Optional ``(percent, message)`` callback.  Implementations should
            drive it while working so the GUI can show a live progress bar.

        Implementations must poll :meth:`_cancelled` between steps and raise
        :class:`EngineError` with a friendly message on failure.  They should
        emit :attr:`progress` and :attr:`log_line` while working.
        """

    @abc.abstractmethod
    def cancel(self) -> None:
        """Request cooperative cancellation of the running job."""

    # -- shared helpers ----------------------------------------------------
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cancelled_flag = False

    def reset(self) -> None:
        """Clear the cancellation flag before starting a new run."""
        self._cancelled_flag = False

    def _cancelled(self) -> bool:
        return self._cancelled_flag

    @staticmethod
    def _require_files(*paths: Path | str, kinds: Sequence[str] = ()) -> None:
        """Validate that input files exist and are non-empty.

        Raises
        ------
        EngineError
            With a user readable message naming the missing file.
        """
        for path in paths:
            candidate = Path(path)
            if not candidate.is_file():
                raise EngineError(
                    f"Required input file is missing: {candidate.name}",
                    f"Looked for {candidate}",
                )
            if candidate.stat().st_size == 0:
                raise EngineError(
                    f"The file {candidate.name} is empty.",
                    f"{candidate} has size 0 bytes.",
                )
