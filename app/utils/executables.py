"""Discovery of external command line tools (AutoDock Vina, QVina2, ...).

EnzyDock drives docking engines as separate processes.  Those binaries are
not pip-installable, so the application has to find them on the user's
machine — and, when it cannot, explain *in plain language* what to do.

Search order (first hit wins)
-----------------------------
1. ``settings``           – a path the user typed once and we remembered,
2. ``environment``        – ``ENZYDOCK_VINA`` and friends (handy for CI),
3. ``bundled``            – ``app/resources/bin`` inside the install,
4. ``next to the app``    – for portable / frozen installations,
5. ``project tools``      – ``<checkout>/tools`` while developing,
6. ``PATH``               – the normal case for a scientific workstation,
7. ``standard locations`` – ``C:\\apps`` / ``/opt/homebrew/bin`` ...

Which file names and directories those mean is decided by
:mod:`app.utils.platforms`, so the same code finds ``vina.exe`` on Windows
and ``vina_mac_aarch64`` on an Apple Silicon Mac.

Every function resolves paths **at call time** so the same code works from a
source checkout and from a PyInstaller bundle.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import paths
from .logger import get_logger
from .platforms import app_display_name, common_tool_dirs, runnable_copy

log = get_logger(__name__)

#: Directories that commonly hold scientific binaries on Windows.
#: Kept for backwards compatibility - :func:`common_tool_dirs` is platform
#: aware and should be preferred.
WINDOWS_COMMON_DIRS: tuple[str, ...] = (
    r"C:\apps",
    r"C:\Program Files\AutoDock Vina",
    r"C:\Program Files\AutoDock Vina 1.2.3",
    r"C:\Program Files (x86)\AutoDock Vina",
    r"C:\Program Files\MGLTools",
)


@dataclass(frozen=True)
class Executable:
    """A program found on this machine."""

    path: Path
    #: How it was found - shown to the user in diagnostics.
    source: str
    #: Output of ``--version`` (empty when the probe failed).
    version: str = ""

    @property
    def name(self) -> str:
        """File name of the executable."""
        return self.path.name

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.path} ({self.source})"


class ExecutableError(RuntimeError):
    """Raised when a required external program cannot be used."""

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details


def _creation_flags() -> int:
    """Hide the console window that a Windows subsystem binary would open."""
    if sys.platform != "win32":                        # pragma: no cover
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _settings_value(settings: object | None, key: str | None) -> str:
    """Read a stored path from :class:`app.utils.settings.AppSettings`."""
    if settings is None or not key:
        return ""
    getter = getattr(settings, "get_str", None)
    if getter is None:                                 # pragma: no cover
        return ""
    try:
        return getter(key).strip()
    except Exception:                                  # pragma: no cover
        return ""


def _candidate_directories(extra_dirs: Iterable[str | Path]) -> list[tuple[Path, str]]:
    """Return ``(directory, source_label)`` pairs to search, in priority order."""
    candidates: list[tuple[Path, str]] = []

    bundled = paths.resource("bin")
    if bundled.is_dir():
        candidates.append((bundled, "bundled with EnzyDock"))

    if paths.is_frozen():                              # pragma: no cover
        beside = Path(sys.executable).resolve().parent
        candidates.append((beside, f"next to {app_display_name()}"))
        # A macOS .app keeps its payload in Contents/{Frameworks,Resources},
        # so look in both - the layout differs between PyInstaller versions.
        meipass = Path(getattr(sys, "_MEIPASS", beside))
        for relative in (
            meipass / "app" / "resources" / "bin",
            meipass.parent / "Resources" / "app" / "resources" / "bin",
            meipass / ".." / "Resources" / "app" / "resources" / "bin",
        ):
            resolved = relative.resolve()
            if resolved not in [item[0] for item in candidates]:
                candidates.append((resolved, "bundled with EnzyDock"))
    else:
        candidates.append((paths.project_root() / "tools", "the EnzyDock tools folder"))

    for directory in extra_dirs:
        candidates.append((Path(directory), "a standard install location"))

    for directory in common_tool_dirs():
        candidates.append((Path(directory), "a standard install location"))

    return candidates


def find_executable(
    names: Sequence[str],
    *,
    env_var: str | None = None,
    settings: object | None = None,
    settings_key: str | None = None,
    extra_dirs: Iterable[str | Path] = (),
    version_args: Sequence[str] = ("--version",),
    version_timeout: float = 8.0,
    argv_prefix: Sequence[str] = (),
) -> Executable | None:
    """Locate one of ``names`` and return an :class:`Executable`.

    Parameters
    ----------
    names:
        File names to try, most likely first (``"vina.exe"`` on Windows,
        ``"vina_mac_aarch64"`` on an Apple Silicon Mac, ...).
    env_var:
        Environment variable holding a full path or a directory.
    settings, settings_key:
        User preference that overrides everything else.
    extra_dirs:
        Additional directories to search before the standard ones.
    version_args:
        Arguments used to probe the program version.
    argv_prefix:
        Command inserted before the binary when probing (``wsl``, an
        interpreter, ...).

    Returns
    -------
    Executable or None
        ``None`` when the program is nowhere to be found.
    """
    checked: list[Path] = []

    def usable(candidate: Path) -> Path | None:
        """A path we can actually run, or ``None`` (see :func:`runnable_copy`)."""
        if not candidate.is_file():
            return None
        return runnable_copy(candidate, paths.user_data_dir(create=False) / "bin")

    # 1. remembered preference ------------------------------------------------
    stored = _settings_value(settings, settings_key)
    for candidate in _expand(stored, names):
        checked.append(candidate)
        found = usable(candidate)
        if found is not None:
            return _probe(found, "your settings", version_args,
                          version_timeout, argv_prefix)

    # 2. environment variable -------------------------------------------------
    env_value = os.environ.get(env_var, "").strip() if env_var else ""
    for candidate in _expand(env_value, names):
        checked.append(candidate)
        found = usable(candidate)
        if found is not None:
            return _probe(found, f"the {env_var} environment variable",
                          version_args, version_timeout, argv_prefix)

    # 3-5. bundled / next to the app / project tools --------------------------
    for directory, label in _candidate_directories(extra_dirs):
        if not directory.is_dir():
            continue
        for name in names:
            candidate = directory / name
            checked.append(candidate)
            found = usable(candidate)
            if found is not None:
                return _probe(found, label, version_args, version_timeout,
                              argv_prefix)

    # 6. PATH -----------------------------------------------------------------
    for name in names:
        located = shutil.which(name)
        if located:
            candidate = Path(located)
            checked.append(candidate)
            found = usable(candidate)
            if found is not None:
                return _probe(found, "the system PATH", version_args,
                              version_timeout, argv_prefix)

    log.debug("executable %s not found (checked %d paths)", list(names), len(checked))
    return None


def _expand(value: str, names: Sequence[str]) -> list[Path]:
    """Turn a setting / environment value into concrete file candidates.

    The value may be a file, a directory, or be empty - all are handled.
    """
    if not value:
        return []
    base = Path(value)
    if base.is_dir():
        return [base / name for name in names]
    if base.suffix:                                     # looks like a file
        return [base]
    return [base / name for name in names]


def _probe(
    path: Path,
    source: str,
    version_args: Sequence[str],
    timeout: float,
    argv_prefix: Sequence[str] = (),
) -> Executable:
    """Build an :class:`Executable`, probing for a version string."""
    version = ""
    try:
        completed = subprocess.run(
            [*argv_prefix, str(path), *version_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_creation_flags(),
            errors="replace",
        )
        for line in completed.stdout.splitlines():
            if line.strip():
                version = line.strip()
                break
    except Exception as exc:                            # noqa: BLE001
        log.debug("version probe failed for %s: %s", path, exc)
    log.info("found %s: %s (%s)", path.name, path, source)
    return Executable(path=path, source=source, version=version)


def probe_executable(
    path: str | Path,
    *,
    source: str = "your settings",
    version_args: Sequence[str] = ("--version",),
    timeout: float = 8.0,
) -> Executable:
    """Probe one specific file, e.g. one the user just picked in a dialog.

    Returns an :class:`Executable` whose ``version`` is empty when the probe
    failed - the caller decides whether that is fatal.
    """
    return _probe(Path(path), source, version_args, timeout)


def find_openbabel() -> str | None:
    """Path to the OpenBabel command line tool (``obabel``), or ``None``.

    Neither Windows installers nor Homebrew's ``open-babel`` keg put the
    binary on ``PATH`` by default, so both need a directory search:

    * Windows: ``C:\\Program Files\\OpenBabel-<version>\\obabel.exe``
    * macOS:   ``/opt/homebrew/bin/obabel`` (also ``/usr/local``, MacPorts)
    """
    from .platforms import obabel_names, obabel_search_roots

    for name in obabel_names():
        found = shutil.which(name)
        if found:
            return found

    for root in obabel_search_roots():
        if not root.is_dir():
            continue
        # Homebrew / MacPorts keep binaries directly in <root>/bin.
        for directory in (root / "bin", *sorted(root.glob("OpenBabel*"), reverse=True)):
            for name in obabel_names():
                candidate = directory / name
                if candidate.is_file():                 # pragma: no cover
                    return str(candidate)
    return None


def describe_search(
    names: Sequence[str],
    *,
    env_var: str | None = None,
    settings_key: str | None = None,
    extra_dirs: Iterable[str | Path] = (),
) -> str:
    """A short, user readable list of the places that were searched.

    Used as the "technical details" of a "Vina was not found" dialog.
    """
    names_text = " or ".join(names)
    locations = ["the program chosen in File > Set Docking Engine"]
    if env_var:
        locations.append(f"the {env_var} environment variable")
    locations.append("the EnzyDock installation folder")
    if not paths.is_frozen():
        locations.append("<project>/tools")
    locations.append("the system PATH")
    for directory in (*extra_dirs, *common_tool_dirs()):
        locations.append(str(directory))
    return f"Looked for {names_text} in:\n  - " + "\n  - ".join(locations)


__all__ = [
    "Executable",
    "ExecutableError",
    "WINDOWS_COMMON_DIRS",
    "describe_search",
    "find_executable",
    "probe_executable",
]
