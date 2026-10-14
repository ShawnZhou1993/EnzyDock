"""Where things live inside a built EnzyDock bundle, per platform.

The Windows build is a ``onedir`` folder that flat-tops with ``EnzyDock.exe``
and keeps 500-odd files in ``_internal``.  The macOS build is an ``.app``
package whose payload is split between ``Contents/MacOS``,
``Contents/Frameworks`` and ``Contents/Resources``.

Three scripts need to agree on that layout - the packer, the splitter's
manifest writer and the bundle verifier - so it is described once, here.

**Convention.** ``bundle_dir`` is the folder the user opens, named relative
to the archive root.  Every *other* path field (``launcher``,
``payload_dir``, ``critical_files``) is relative to ``bundle_dir`` itself,
because ``--dist`` always points at the bundle:

    Windows   --dist dist/EnzyDock      -> launcher EnzyDock.exe
    macOS     --dist dist/EnzyDock.app  -> launcher Contents/MacOS/EnzyDock
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BundleLayout:
    """Everything the packaging scripts need to know about one bundle."""

    #: ``"Windows"`` / ``"macOS"``.
    system: str
    #: ``"x64"`` / ``"arm64"`` ...
    arch: str
    #: Suffix used in archive names, e.g. ``"win64"`` / ``"macos-arm64"``.
    tag: str
    #: The folder the user runs, *relative to the archive root*.
    bundle_dir: str
    #: The launcher inside that folder.
    launcher: str
    #: Root of the bundled Python payload (relative to ``bundle_dir``);
    #: ``""`` when it sits directly in the bundle (macOS).app.
    payload_dir: str
    #: Helper files copied in from ``packaging/``.
    extras: tuple[str, ...]
    #: Files whose hash is worth printing - a broken copy is caught by these.
    critical_files: tuple[str, ...]
    #: The receiver-side merge script.
    merge_script: str
    #: Minimum number of files expected in the payload folder.
    min_payload_files: int
    #: How to spell a SHA256 command in the notes.
    hash_command: str
    #: Extra notes about where the user should extract the archive.
    target_hint: str
    #: Optional extra lines describing platform specific traps.
    notes: tuple[str, ...] = field(default_factory=tuple)

    def critical_paths(self, bundle: Path) -> list[tuple[str, Path]]:
        """``(label, path)`` for every file the manifest fingerprints."""
        return [(name, bundle / name) for name in self.critical_files]

    def archive_stem(self, version: str) -> str:
        """Base name (no extension) of the transfer archive."""
        return f"EnzyDock-{version}-{self.tag}"


WINDOWS = BundleLayout(
    system="Windows",
    arch="x64",
    tag="win64",
    bundle_dir="EnzyDock",
    launcher="EnzyDock.exe",
    payload_dir="_internal",
    extras=("check-install.bat", "HOW-TO-RUN.txt"),
    critical_files=(
        "EnzyDock.exe",
        "_internal/python312.dll",
        "_internal/VCRUNTIME140.dll",
        "_internal/PySide6/Qt6WebEngineCore.dll",
        "_internal/app/resources/viewer/3Dmol.min.js",
        # The engine is the one file whose loss looks like "you must install
        # Vina yourself", so its digest belongs in the manifest.
        "_internal/app/resources/bin/vina.exe",
    ),
    merge_script="merge-parts.bat",
    min_payload_files=500,
    hash_command="certutil -hashfile",
    target_hint="C:\\EnzyDock",
    notes=(
        "Some antivirus programs delete the DLLs inside _internal while "
        "extracting; add the folder to their exceptions if EnzyDock reports "
        "\"Failed to load Python DLL\".",
    ),
)

MACOS_APP = "EnzyDock.app"
_MACOS_MACOS = "Contents/MacOS"
_MACOS_FRAMEWORKS = "Contents/Frameworks"
_MACOS_RESOURCES = "Contents/Resources"

MACOS = BundleLayout(
    system="macOS",
    arch="arm64",
    tag="macos-arm64",
    bundle_dir=MACOS_APP,
    launcher=f"{_MACOS_MACOS}/EnzyDock",
    payload_dir=_MACOS_FRAMEWORKS,
    extras=("check-install.sh", "HOW-TO-RUN-mac.txt"),
    critical_files=(
        f"{_MACOS_MACOS}/EnzyDock",
        f"{_MACOS_FRAMEWORKS}/libpython3.12.dylib",
        f"{_MACOS_FRAMEWORKS}/QtWebEngineCore.framework/Versions/A/QtWebEngineCore",
        f"{_MACOS_RESOURCES}/app/resources/viewer/3Dmol.min.js",
        f"{_MACOS_RESOURCES}/app/resources/bin/vina_mac_aarch64",
    ),
    merge_script="merge-parts.command",
    min_payload_files=300,
    hash_command="shasum -a 256",
    target_hint="/Applications",
    notes=(
        "macOS quarantines anything downloaded from the internet. If it "
        "refuses to open EnzyDock, run:",
        "    xattr -dr com.apple.quarantine /Applications/EnzyDock.app",
        "or right-click the app, choose Open, and confirm once.",
    ),
)


def current() -> BundleLayout:
    """The layout for the platform this interpreter runs on."""
    if sys.platform == "darwin":
        return MACOS
    return WINDOWS


def for_system(system: str) -> BundleLayout:
    """Look a layout up by name (``"macos"`` / ``"windows"``)."""
    key = system.strip().lower()
    if key in ("macos", "darwin", "mac", "osx"):
        return MACOS
    if key in ("windows", "win", "win32"):
        return WINDOWS
    raise ValueError(f"unknown platform: {system!r}")


def detect_layout(bundle: Path) -> BundleLayout:
    """Guess a layout from what is actually on disk.

    Keeps the scripts usable on a bundle built for the *other* platform -
    handy for cross-checking a colleague's build from a Windows checkout.
    """
    if (bundle / MACOS_APP).is_dir() or bundle.name.endswith(".app"):
        return MACOS
    return WINDOWS
