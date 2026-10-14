"""Everything EnzyDock needs to know about the machine it is running on.

The rest of the code base must never guess: file suffixes, the bundled
docking engine for *this* platform, where third party tools tend to live,
and how to hand a file to the desktop's own viewer all differ between
Windows, macOS and Linux.  They live here so each caller stays a one-liner.

This module deliberately imports nothing from EnzyDock (not even
:mod:`app.utils.paths`) so it can be used - and unit tested - anywhere.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------- basics


def is_windows() -> bool:
    """True on Windows."""
    return sys.platform == "win32"


def is_macos() -> bool:
    """True on macOS (``darwin``)."""
    return sys.platform == "darwin"


def is_linux() -> bool:
    """True on Linux."""
    return sys.platform.startswith("linux")


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


#: ``"arm64"``, ``"x86_64"``, or whatever the interpreter reports.
def host_arch() -> str:
    """Normalised CPU architecture of the *running interpreter*.

    On an Apple Silicon Mac this is ``arm64``; when the app runs under
    Rosetta it is ``x86_64``, which is exactly what we want because the
    engine we pick has to match the interpreter's own architecture.
    """
    machine = platform.machine().strip().lower()
    return {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}.get(
        machine, machine
    )


def platform_label() -> str:
    """Short human readable platform name for reports (``"macOS arm64"``)."""
    system = "macOS" if is_macos() else "Windows" if is_windows() else "Linux"
    return f"{system} {host_arch()}"


# ------------------------------------------------------------ file name rules


def exe_suffix() -> str:
    """``".exe"`` on Windows, ``""`` everywhere else."""
    return ".exe" if is_windows() else ""


def exe_name(stem: str) -> str:
    """``stem`` plus the platform's executable suffix."""
    return f"{stem}{exe_suffix()}"


def app_display_name() -> str:
    """How the EnzyDock launcher is named on this platform."""
    return exe_name("EnzyDock")


# ------------------------------------------------------- bundled docking engine

#: Bundled Vina file names, keyed by (platform, architecture).
_BUNDLED_VINA = {
    ("win32", "arm64"): "vina.exe",
    ("win32", "x86_64"): "vina.exe",
    ("darwin", "arm64"): "vina_mac_aarch64",
    ("darwin", "x86_64"): "vina_mac_x86_64",
    ("linux", "arm64"): "vina_linux_aarch64",
    ("linux", "x86_64"): "vina_linux_x86_64",
}


def bundled_vina_name(arch: str | None = None) -> str:
    """File name of the docking engine shipped inside ``app/resources/bin``."""
    key = (sys.platform, (arch or host_arch()))
    if key in _BUNDLED_VINA:
        return _BUNDLED_VINA[key]
    # Unknown architecture: fall back to whatever this platform ships.
    for (system, _arch), name in _BUNDLED_VINA.items():
        if system == sys.platform:
            return name
    return exe_name("vina")


#: Release file names Vina is distributed under, per platform.  These are the
#: names a user who downloaded Vina by hand would end up with.
_VINA_RELEASE_NAMES: dict[str, tuple[str, ...]] = {
    "win32": (
        "vina.exe",
        "vina",
        "vina_1.2.7_win.exe",
        "vina_1.2.5_win.exe",
        "vina_1.2.3.exe",
    ),
    "darwin": (
        "vina",
        "vina_1.2.7_mac_aarch64",
        "vina_1.2.7_mac_x86_64",
        "vina_1.2.5_mac_aarch64",
        "vina_1.2.5_mac_x86_64",
    ),
    "linux": (
        "vina",
        "vina_1.2.7_linux_aarch64",
        "vina_1.2.7_linux_x86_64",
    ),
}


def vina_names(arch: str | None = None) -> tuple[str, ...]:
    """All Vina file names worth looking for, best guess first.

    The bundled copy comes first, then the plain ``vina`` (what Homebrew,
    conda and every scientific workstation install as), then the release
    names a user may have downloaded by hand.

    Names from *other* platforms are never included: a Mach-O binary sitting
    in the bundle on Windows has the right name but cannot run, and picking
    it would hide the real problem.
    """
    names = [bundled_vina_name(arch)]
    for candidate in _VINA_RELEASE_NAMES.get(sys.platform, ("vina",)):
        if candidate not in names:
            names.append(candidate)
    # On Apple Silicon the Intel build still works through Rosetta, so keep it
    # as a last resort - but never the other way round.
    if is_macos() and host_arch() == "arm64":
        for candidate in ("vina_mac_x86_64", "vina_1.2.7_mac_x86_64"):
            if candidate not in names:
                names.append(candidate)
    return tuple(names)


def vina_file_filter() -> str:
    """File dialog filter for picking a docking engine by hand.

    macOS and Linux binaries usually have **no extension at all**, so a
    ``*.exe`` filter would grey out every real candidate.  Windows keeps it
    because that is what users expect there.
    """
    if is_windows():
        return "Programs (*.exe);;All files (*)"
    return "All files (*)"


#: Kept as a module constant so callers can import it without a call.
VINA_FILE_FILTER = vina_file_filter()


def common_tool_dirs() -> tuple[str, ...]:
    """Directories where scientific binaries are conventionally installed."""
    if is_windows():
        return (
            r"C:\apps",
            r"C:\Program Files\AutoDock Vina",
            r"C:\Program Files\AutoDock Vina 1.2.3",
            r"C:\Program Files (x86)\AutoDock Vina",
            r"C:\Program Files\MGLTools",
        )
    if is_macos():
        return (
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/opt/local/bin",
            "/Applications/AutoDock Vina",
            "/Applications/MGLTools",
        )
    return ("/usr/local/bin", "/usr/bin", "/opt/bin")


# ------------------------------------------------------------- OpenBabel


def obabel_names() -> tuple[str, ...]:
    """Candidate file names for the OpenBabel command line tool."""
    return ("obabel.exe", "babel.exe") if is_windows() else ("obabel", "babel")


def obabel_search_roots() -> tuple[Path, ...]:
    """Roots that may contain an OpenBabel installation."""
    if is_windows():
        return (Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)"))
    if is_macos():
        return (Path("/opt/homebrew"), Path("/usr/local"), Path("/opt/local"))
    return (Path("/usr"), Path("/usr/local"))


# ----------------------------------------------------- desktop file handling


def _popen(command: list[str]) -> bool:
    try:
        subprocess.Popen(command)
    except OSError:
        return False
    return True


def open_path(target: str | Path) -> bool:
    """Open a file or folder with the desktop's default handler."""
    text = str(target)
    if is_windows():
        return _popen(["explorer", text])
    if is_macos():
        return _popen(["open", text])
    return _popen(["xdg-open", text])                   # pragma: no cover


def reveal_path(target: str | Path) -> bool:
    """Show a file in the desktop's file manager."""
    text = str(target)
    if is_windows():
        return _popen(["explorer", f"/select,{text}"])
    if is_macos():
        # ``open -R`` selects the file in Finder.
        return _popen(["open", "-R", text])
    parent = str(Path(text).parent)                     # pragma: no cover
    return _popen(["xdg-open", parent])                 # pragma: no cover


def open_in_editor(target: str | Path) -> bool:
    """Open a text file in something that can display it."""
    text = str(target)
    if is_windows():
        return _popen(["notepad", text])
    if is_macos():
        # ``open -t`` opens in the default text editor (TextEdit by default).
        return _popen(["open", "-t", text])
    return _popen(["xdg-open", text])                   # pragma: no cover


# --------------------------------------------------------- per user locations


def default_user_data_dir() -> Path:
    """Where settings and logs belong on this platform."""
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "EnzyDock"
    return Path.home() / ".enzydock"


def default_workspace_root() -> Path:
    """Where per-run workspaces belong on a packaged installation."""
    if is_macos():
        return Path.home() / "Documents" / "EnzyDock"
    return Path.home() / "EnzyDock" / "workspace"


# --------------------------------------------------------- executable bit


def ensure_executable(path: str | Path) -> bool:
    """Make sure ``path`` can be executed; fix the permission bits if not.

    Zip archives on macOS routinely drop the ``+x`` bit, and a file that came
    from another machine also carries the ``com.apple.quarantine`` attribute
    that makes Gatekeeper refuse to run it.  We cannot remove the attribute
    from inside the app, but we *can* restore the permission bits, which is
    the half users trip over most often.
    """
    target = Path(path)
    if is_windows():                                    # pragma: no cover
        return target.is_file()
    if not target.is_file():                            # pragma: no cover
        return False
    if os.access(target, os.X_OK):
        return True
    try:
        target.chmod(0o755)
    except OSError:                                     # pragma: no cover
        return False
    return os.access(target, os.X_OK)


def runnable_copy(path: str | Path, cache_dir: str | Path | None = None) -> Path | None:
    """Return a copy of ``path`` that is guaranteed to be executable.

    Zip archives (on every platform) routinely drop the ``+x`` bit, so the
    bundled engine can arrive on the user's disk as a perfectly good binary
    that simply refuses to start.  When that happens we copy it somewhere we
    own and fix the bits there.

    We never chmod inside the installed application: on macOS that would
    invalidate the code signature and make Gatekeeper refuse to launch the
    whole app - a much worse failure than the one we are fixing.

    Returns ``None`` when the file does not exist or cannot be fixed.
    """
    target = Path(path)
    if not target.is_file():                            # pragma: no cover
        return None
    if is_windows():
        return target
    if os.access(target, os.X_OK):
        return target

    destination_dir = Path(cache_dir) if cache_dir else default_user_data_dir() / "bin"
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        import shutil

        copy = destination_dir / target.name
        shutil.copy2(target, copy)
        copy.chmod(0o755)
    except OSError:                                     # pragma: no cover
        return None
    return copy if os.access(copy, os.X_OK) else None


def quarantine_hint() -> str:
    """Platform specific advice for a binary that refuses to start.

    Windows: antivirus software.  macOS: the quarantine attribute Apple
    attaches to anything that arrived over the network.
    """
    if is_macos():
        return (
            "If macOS says the file cannot be opened, it is almost always the "
            "quarantine flag Apple attaches to downloaded files.  In Terminal "
            "run:\n"
            "    xattr -dr com.apple.quarantine \"EnzyDock.app\"\n"
            "or right-click EnzyDock.app, choose Open, and confirm once."
        )
    if is_windows():
        return (
            "This normally means an antivirus removed the file, or the folder "
            "you received is incomplete.  Extract EnzyDock again and allow the "
            "file in your antivirus."
        )
    return (                                            # pragma: no cover
        "Check that the file has its executable bit set "
        "(chmod +x <file>)."
    )
