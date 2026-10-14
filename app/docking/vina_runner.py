"""AutoDock Vina backend.

Vina is driven as an external process so that

* the GUI never blocks (the whole engine runs inside a worker thread and
  streams its output while it works),
* the user can cancel a run,
* the exact command line is reproducible - it is written to
  ``<workspace>/docking/config.txt`` and ``command.txt``.

The engine knows nothing about widgets: it exposes ``progress`` /
``log_line`` signals and returns a plain
:class:`app.docking.docking_config.DockingResult`.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtCore import QObject

from ..utils import paths
from ..utils.executables import Executable, describe_search, find_executable
from ..utils.logger import get_logger
from ..utils.platforms import (
    bundled_vina_name,
    quarantine_hint,
    runnable_copy,
    vina_names,
)
from .base import DockingEngine, EngineError
from .docking_config import BoxConfig, DockingParameters, DockingResult
from .result_parser import parse_log_table, parse_poses

log = get_logger(__name__)

#: File name of the engine shipped inside ``app/resources/bin`` for the
#: platform we are running on (``vina.exe``, ``vina_mac_aarch64``, ...).
VINA_BUNDLED_NAME: str = bundled_vina_name()

#: File names Vina is shipped under, best guess first.  Platform specific:
#: only names that can actually run here are listed, so a Mach-O binary
#: sitting next to a Windows install is never mistaken for a working engine.
VINA_NAMES: tuple[str, ...] = vina_names()
#: Environment variable that can point at a local installation.
VINA_ENV_VAR = "ENZYDOCK_VINA"
#: Settings key holding a path the user configured once.
VINA_SETTINGS_KEY = "paths/vina_executable"

#: Fallback number of ``*`` marks Vina prints for a complete progress bar.
_DEFAULT_STARS = 51


class VinaEngine(DockingEngine):
    """Run AutoDock Vina (1.2.x) as a subprocess."""

    name = "AutoDock Vina"

    def __init__(
        self,
        executable: str | Path | None = None,
        settings: object | None = None,
        parent: QObject | None = None,
        argv_prefix: Sequence[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._explicit = Path(executable) if executable else None
        self._settings = settings
        self._executable: Executable | None = None
        self._process: subprocess.Popen[bytes] | None = None
        #: Command inserted before the binary (``wsl``, ``conda run``, a test
        #: interpreter, ...).  Empty for a normal installation.
        self.argv_prefix: list[str] = list(argv_prefix or ())

    # ----------------------------------------------------------- discovery
    def locate(self) -> Executable | None:
        """Find the Vina binary (cached after the first successful call)."""
        if self._executable is not None:
            return self._executable
        if self._explicit is not None:
            if not self._explicit.is_file():
                return None
            from ..utils.executables import _probe

            usable = runnable_copy(self._explicit,
                                   paths.user_data_dir(create=False) / "bin")
            if usable is None:
                return None
            self._executable = _probe(usable, "the configuration",
                                      ("--version",), 8.0, self.argv_prefix)
            return self._executable
        self._executable = find_executable(
            VINA_NAMES,
            env_var=VINA_ENV_VAR,
            settings=self._settings,
            settings_key=VINA_SETTINGS_KEY,
            argv_prefix=self.argv_prefix,
        )
        return self._executable

    def is_available(self) -> tuple[bool, str]:
        """Return ``(ok, reason)`` for the GUI combo box / run button."""
        found = self.locate()
        if found is None:
            bundled = paths.resource("bin", VINA_BUNDLED_NAME)
            return False, (
                "The AutoDock Vina that ships with EnzyDock could not be "
                f"started. It should be here:\n    {bundled}\n"
                f"{quarantine_hint()}\n"
                "You can also point EnzyDock at another Vina with "
                "File > Set Docking Engine."
            )
        return True, f"{found.version or 'AutoDock Vina'} - found {found.source}."

    def version(self) -> str:
        """Version string reported by the binary, or ``""``."""
        found = self.locate()
        return "" if found is None else found.version

    def executable_path(self) -> Path | None:
        """Path of the Vina binary, or ``None`` when it cannot be found."""
        found = self.locate()
        return None if found is None else found.path

    # ------------------------------------------------------------- command
    def config_text(
        self,
        receptor: Path | str,
        ligand: Path | str,
        box: BoxConfig,
        parameters: DockingParameters,
    ) -> str:
        """Reproducible Vina configuration file content.

        Saved to ``docking/config.txt`` so a run can be repeated by hand.
        """
        center, size = box.center, box.size
        lines = [
            f"receptor = {Path(receptor).name}",
            f"ligand = {Path(ligand).name}",
            "",
            f"center_x = {center[0]:.3f}",
            f"center_y = {center[1]:.3f}",
            f"center_z = {center[2]:.3f}",
            "",
            f"size_x = {size[0]:.3f}",
            f"size_y = {size[1]:.3f}",
            f"size_z = {size[2]:.3f}",
            "",
            f"exhaustiveness = {parameters.exhaustiveness}",
            f"num_modes = {parameters.num_poses}",
            f"energy_range = {parameters.energy_range:g}",
        ]
        if parameters.cpu:
            lines.append(f"cpu = {parameters.cpu}")
        if parameters.seed is not None:
            lines.append(f"seed = {parameters.seed}")
        return "\n".join(lines) + "\n"

    def build_command(
        self,
        receptor: Path | str,
        ligand: Path | str,
        box: BoxConfig,
        parameters: DockingParameters,
        out_path: Path | str,
    ) -> list[str]:
        """Build the argument list for :class:`subprocess.Popen`.

        Paths are passed on the command line rather than through ``--config``
        because Vina's configuration parser does not quote paths containing
        spaces reliably on Windows.
        """
        center, size = box.center, box.size
        command = [
            *self.argv_prefix,
            str(self.executable_path()),
            "--receptor", str(Path(receptor)),
            "--ligand", str(Path(ligand)),
            "--center_x", f"{center[0]:.3f}",
            "--center_y", f"{center[1]:.3f}",
            "--center_z", f"{center[2]:.3f}",
            "--size_x", f"{size[0]:.3f}",
            "--size_y", f"{size[1]:.3f}",
            "--size_z", f"{size[2]:.3f}",
            "--exhaustiveness", str(int(parameters.exhaustiveness)),
            "--num_modes", str(int(parameters.num_poses)),
            "--energy_range", f"{parameters.energy_range:g}",
            "--cpu", str(int(parameters.cpu)),
            "--out", str(Path(out_path)),
        ]
        if parameters.seed is not None:
            command += ["--seed", str(int(parameters.seed))]
        return command

    # ----------------------------------------------------------------- run
    def run(
        self,
        receptor: Path | str,
        ligand: Path | str,
        box: BoxConfig,
        parameters: DockingParameters,
        output_dir: Path | str,
        progress: Callable[[int, str], None] | None = None,
    ) -> DockingResult:
        """Dock ``ligand`` into ``receptor`` and return the parsed result.

        Parameters
        ----------
        output_dir:
            ``<workspace>/docking``.  ``config.txt``, ``command.txt``,
            ``log.txt`` and ``output.pdbqt`` are written there.
        progress:
            Optional ``(percent, message)`` callback driven from Vina's
            progress bar.

        Raises
        ------
        EngineError
            With a message a bench scientist can act on.
        """
        self.reset()
        started = time.perf_counter()

        found = self.locate()
        if found is None:
            raise EngineError(
                "The AutoDock Vina that ships with EnzyDock could not be "
                "started.\n\nExtract the EnzyDock folder again (an antivirus "
                "often removes the program), or choose another engine in "
                "File > Set Docking Engine.",
                describe_search(VINA_NAMES, env_var=VINA_ENV_VAR,
                                settings_key=VINA_SETTINGS_KEY),
            )
        self._require_files(receptor, ligand)

        docking_dir = Path(output_dir)
        docking_dir.mkdir(parents=True, exist_ok=True)
        out_path = docking_dir / "output.pdbqt"
        log_path = docking_dir / "log.txt"
        (docking_dir / "config.txt").write_text(
            self.config_text(receptor, ligand, box, parameters), encoding="utf-8"
        )

        command = self.build_command(receptor, ligand, box, parameters, out_path)
        (docking_dir / "command.txt").write_text(
            " ".join(command) + "\n", encoding="utf-8"
        )

        text, returncode, cancelled = self._execute(command, progress)
        log_path.write_text(text, encoding="utf-8")

        if cancelled:
            raise EngineError(
                "The docking run was cancelled before it finished.",
                "Vina was terminated at the user's request.",
            )
        if returncode != 0:
            raise EngineError(*_explain_failure(text, returncode))
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise EngineError(
                "AutoDock Vina finished but did not write any poses. This "
                "usually means the ligand does not fit inside the docking box - "
                "try a larger box or a different binding site.",
                f"no output at {out_path}; Vina said:\n{text[-1500:]}",
            )

        poses = parse_poses(out_path)
        if not poses:
            poses = parse_log_table(text)
            for index, pose in enumerate(poses, start=1):
                pose.pose = index
        if not poses:
            raise EngineError(
                "Docking finished, but no binding poses could be read from the "
                "result file. Try increasing the box size or the exhaustiveness.",
                f"no 'REMARK VINA RESULT' rows and no score table in:\n"
                f"{text[-1500:]}",
            )

        result = DockingResult(
            receptor=Path(receptor),
            ligand=Path(ligand),
            engine=_label(found.version),
            box=box,
            parameters=parameters,
            poses=poses,
            log_file=log_path,
            output_file=out_path,
            workspace=docking_dir.parent,
            elapsed_seconds=time.perf_counter() - started,
        )
        best = result.best_affinity
        log.info("vina produced %d poses, best %.3f kcal/mol in %.1fs",
                 len(poses), best if best is not None else float("nan"),
                 result.elapsed_seconds)
        return result

    def cancel(self) -> None:
        """Stop the running Vina process (cooperative, next read loop tick)."""
        self._cancelled_flag = True
        process = self._process
        if process is not None and process.poll() is None:
            log.info("terminating Vina (pid %s)", process.pid)
            try:
                process.terminate()
            except Exception as exc:                   # noqa: BLE001
                log.debug("terminate failed: %s", exc)

    # ------------------------------------------------------------ internals
    def _execute(
        self,
        command: Sequence[str],
        progress: Callable[[int, str], None] | None,
    ) -> tuple[str, int, bool]:
        """Stream Vina's output, tracking progress and cancellation.

        Returns ``(captured_text, returncode, cancelled)``.
        """
        creation = 0
        if sys.platform == "win32":                    # pragma: no cover - Windows
            creation = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

        self._emit(0, "Starting AutoDock Vina ...", progress)
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creation,
        )
        self._process = process

        captured: list[str] = []
        stars = 0
        expected = _DEFAULT_STARS
        pending = ""
        cancelled = False

        def handle(chunk: str) -> None:
            """Consume one chunk of engine output."""
            nonlocal stars, expected, pending
            pending += chunk
            while True:
                newline = pending.find("\n")
                if newline < 0:
                    break
                line = pending[:newline]
                pending = pending[newline + 1:]
                captured.append(line)
                self.log_line.emit(line.rstrip())
                expected = _update_expected(line, expected)
                stars += line.count("*")
                self._emit(_percent(stars, expected), "Running AutoDock Vina ...",
                           progress)
            # Vina prints the progress marks without a trailing newline; the
            # partial line has to be inspected as well.
            extra = pending.count("*")
            if extra:
                stars += extra
                pending = pending.replace("*", "")
                self._emit(_percent(stars, expected), "Running AutoDock Vina ...",
                           progress)

        assert process.stdout is not None               # noqa: S101 - set above
        while True:
            chunk = process.stdout.read1(1024)
            if chunk:
                handle(chunk.decode("utf-8", errors="replace"))
                continue
            # Cancellation is checked *before* the exit status: a process that
            # we just terminated must be reported as cancelled, not as failed.
            if self._cancelled():
                cancelled = True
                self._kill(process)
                break
            if process.poll() is not None:
                break
            time.sleep(0.02)

        if pending.strip():
            captured.append(pending)
            self.log_line.emit(pending.rstrip())

        # Drain whatever is left after the process exited.
        try:
            remainder = process.stdout.read()
        except Exception:                              # pragma: no cover
            remainder = b""
        if remainder:
            handle(remainder.decode("utf-8", errors="replace"))

        returncode = process.wait()
        self._process = None
        self._emit(100, "Docking finished.", progress)
        return "\n".join(captured), returncode, cancelled

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        """Terminate, then kill, a process that ignores the first request."""
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:                              # noqa: BLE001
            try:
                process.kill()
            except Exception:                          # pragma: no cover
                log.debug("kill failed; the process had already exited")

    def _emit(
        self,
        percent: int,
        message: str,
        progress: Callable[[int, str], None] | None,
    ) -> None:
        """Report progress through both the Qt signal and the callback."""
        self.progress.emit(int(percent), message)
        if progress is not None:
            progress(int(percent), message)


def _label(version: str) -> str:
    """Display name of this engine, without repeating "AutoDock Vina".

    ``vina --version`` already prints ``AutoDock Vina v1.2.3``, so appending
    it to the engine name would read "AutoDock Vina AutoDock Vina v1.2.3".
    """
    version = (version or "").strip()
    if not version:
        return VinaEngine.name
    if VinaEngine.name.lower() in version.lower():
        return version
    return f"{VinaEngine.name} {version}"


def _update_expected(line: str, current: int) -> int:
    """Derive the number of ``*`` marks in Vina's progress bar.

    The bar looks like ``|----|----|...|``: eleven ``|`` means ten segments,
    and Vina prints one mark every two percent -> ``10 * 5 + 1`` marks.
    """
    if line.startswith("|") and line.count("|") > 2:
        segments = line.count("|") - 1
        return max(segments * 5 + 1, 1)
    return current


def _percent(stars: int, expected: int) -> int:
    """Convert a number of progress marks into a 0-100 percentage."""
    if expected <= 0:
        return 0
    return max(0, min(100, int(round(stars / expected * 100))))


def _explain_failure(text: str, returncode: int) -> tuple[str, str]:
    """Turn a non-zero exit into ``(user_message, technical_details)``."""
    tail = text[-2000:]
    lowered = text.lower()

    if "outside the grid" in lowered or "outside the box" in lowered:
        message = (
            "The ligand does not fit inside the docking box, so AutoDock Vina "
            "had no space to search. Move the box onto the binding site or "
            "increase its size (22-26 A per edge is a good starting point)."
        )
    elif "parse error" in lowered or "cannot parse" in lowered or "atom type" in lowered:
        message = (
            "AutoDock Vina could not read one of the prepared structures. This "
            "usually happens when the protein contains an unusual residue. "
            "Try preparing the protein again, or remove problematic "
            "hetero atoms and metal ions."
        )
    elif "no such file" in lowered or "cannot open" in lowered or "not found" in lowered:
        message = (
            "AutoDock Vina could not open one of the prepared files. Please "
            "prepare the protein and the ligand again and retry."
        )
    elif "memory" in lowered:
        message = (
            "AutoDock Vina ran out of memory. Reduce the box size or the "
            "number of CPUs used for the search."
        )
    else:
        message = (
            "AutoDock Vina stopped with an error and no docking result was "
            "produced. The most common causes are a docking box that is too "
            "small, or a protein structure Vina cannot read."
        )
    return message, f"exit code {returncode}\n\n{tail}"


# Make the engine discoverable through the registry.
DockingEngine.register(VinaEngine)

__all__ = [
    "VINA_ENV_VAR",
    "VINA_NAMES",
    "VINA_SETTINGS_KEY",
    "VinaEngine",
]
