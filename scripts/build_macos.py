"""Build the EnzyDock macOS application bundle with PyInstaller.

**This script has to run on a Mac.**  PyInstaller is not a cross compiler: a
``.app`` can only be produced on macOS, by a Python interpreter of the same
architecture as the target.  Everything else in the repository is written so
that it behaves correctly on both platforms; this is the last step and it
needs the real thing.

Usage (from the project root, inside the virtual environment)::

    python scripts/build_macos.py                 # .app for this machine
    python scripts/build_macos.py --arch universal2
    python scripts/build_macos.py --console       # keep stderr visible
    python scripts/build_macos.py --zip           # also make the transfer ZIP

What it produces
----------------
``dist/EnzyDock.app`` - a normal macOS application.  Its payload lives in
``Contents/Frameworks`` (PyInstaller's binary directory) and
``Contents/Resources`` (the data files added by ``--add-data``), so the
``app/resources`` tree ends up under ``Contents/Resources``.

Notes that cost time to rediscover
----------------------------------
* ``--windowed`` is what turns the output into a ``.app``; without it you get
  a bare Mach-O executable in a folder.
* ``--osx-bundle-identifier`` is not optional in practice: an ``.app`` without
  a bundle id is refused by Gatekeeper on modern macOS.
* ``UPX`` must be **off**.  It corrupts Mach-O binaries, breaks the code
  signature, and does not support arm64 at all.
* QtWebEngine needs a JIT entitlement or its helper process is killed on
  launch.  We sign ad-hoc (``-``) since there is no Developer ID here; an
  ad-hoc signature with entitlements is enough for the app to run locally
  and for the receiver to open it with a right-click.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "scripts") not in sys.path:           # pragma: no cover
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

APP_NAME = "EnzyDock"
APP_BUNDLE = f"{APP_NAME}.app"
ENTRY_POINT = PROJECT_ROOT / "run.py"
BUNDLE_IDENTIFIER = "com.enzydock.app"
ICON = PROJECT_ROOT / "app" / "resources" / "icons" / "enzydock.icns"
ENTITLEMENTS = PROJECT_ROOT / "packaging" / "enzydock.entitlements"

#: Modules PyInstaller cannot detect because they are imported dynamically.
HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "rdkit",
    "rdkit.Chem",
    "rdkit.Chem.AllChem",
    "rdkit.Chem.rdForceField",
    "rdkit.Chem.rdMolDescriptors",
    "rdkit.Chem.Descriptors",
    "rdkit.Chem.Crippen",
    "rdkit.Chem.rdmolops",
    "rdkit.Chem.rdmolfiles",
    "rdkit.Geometry",
    "meeko",
    "meeko.polymer",
    "meeko.receptor_pdbqt",
    "meeko.molecule_pdbqt",
    "meeko.preparation",
    "scipy.spatial",
    "numpy",
]

COLLECT_DATA = ["meeko"]

DATAS = [(PROJECT_ROOT / "app" / "resources", "app/resources")]

EXCLUDES = [
    "tkinter",
    "matplotlib",
    # gemmi must NOT be excluded: meeko.polymer imports it at module level.
    "pandas",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
]


def os_sep() -> str:
    """PyInstaller's ``--add-data`` separator (``:`` on POSIX)."""
    return ";" if sys.platform.startswith("win") else ":"


def build_command(args: argparse.Namespace,
                  dist_path: Path | None = None) -> list[str]:
    """Assemble the PyInstaller command line."""
    target_arch = args.arch or "universal2"
    command: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", APP_NAME,
        "--onedir",
        "--windowed",                                  # -> .app bundle
        "--osx-bundle-identifier", BUNDLE_IDENTIFIER,
        "--target-architecture", target_arch,
        "--codesign-identity", args.codesign_identity,
    ]
    if ENTITLEMENTS.is_file():
        command += ["--entitlements", str(ENTITLEMENTS)]
    if ICON.is_file():
        command += ["--icon", str(ICON)]
    else:                                              # pragma: no cover
        print(f"note: {ICON.name} is missing; run scripts/make_icons.py",
              file=sys.stderr)

    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    for package in COLLECT_DATA:
        command += ["--collect-data", package]
    for source, destination in DATAS:
        if source.exists():
            command += ["--add-data", f"{source}{os_sep()}{destination}"]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    # UPX mangles Mach-O binaries and invalidates signatures.
    command += ["--noupx"]

    command += ["--distpath", str(dist_path or PROJECT_ROOT / "dist"),
                "--workpath", str(PROJECT_ROOT / "build" / "macos"),
                "--specpath", str(PROJECT_ROOT / "build"),
                str(ENTRY_POINT)]
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Package {APP_NAME} for macOS")
    parser.add_argument("--arch", choices=("arm64", "x86_64", "universal2"),
                        default=None,
                        help="target architecture (default: universal2)")
    parser.add_argument("--console", action="store_true",
                        help="keep the terminal attached (debugging builds)")
    parser.add_argument("--zip", action="store_true",
                        help="also pack the .app into a transfer-ready ZIP")
    parser.add_argument("--clean", action="store_true",
                        help="remove build/macos and dist/ first")
    parser.add_argument("--codesign-identity", default="-",
                        help="codesign identity (default '-', i.e. ad-hoc)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the PyInstaller command without running it")
    return parser


def preflight() -> list[str]:
    """Problems that would make the build fail, in plain language."""
    problems: list[str] = []
    if sys.platform != "darwin":
        problems.append(
            "this script must run on macOS - PyInstaller cannot build a .app "
            f"anywhere else (this is {platform.system()})"
        )
    if not ENTRY_POINT.is_file():                      # pragma: no cover
        problems.append(f"entry point not found: {ENTRY_POINT}")
    import importlib.util

    if importlib.util.find_spec("PyInstaller") is None:
        problems.append(
            "PyInstaller is not installed. Run:\n"
            "    .venv/bin/python -m pip install -r requirements-dev.txt"
        )
    return problems


def sign_bundle(bundle: Path, identity: str) -> bool:
    """(Re-)sign the finished bundle, including the QtWebEngine helper.

    PyInstaller signs everything it creates, but ``--deep`` on an already
    signed bundle is the reliable way to get a consistent signature over the
    framework helpers.  Ad-hoc signing never fails for lack of a certificate.
    """
    if shutil.which("codesign") is None:               # pragma: no cover
        print("note: codesign not found; skipping the signing step",
              file=sys.stderr)
        return False
    command = ["codesign", "--force", "--deep", "--sign", identity,
               "--options", "runtime"]
    if ENTITLEMENTS.is_file():
        command += ["--entitlements", str(ENTITLEMENTS)]
    command.append(str(bundle))

    completed = subprocess.run(command, capture_output=True, text=True,
                               errors="replace")
    if completed.returncode != 0:                      # pragma: no cover
        print("warning: codesign failed:\n" + (completed.stderr or "").strip(),
              file=sys.stderr)
        return False
    print(f"signed {bundle.name} ({identity})")
    return True


def strip_quarantine(bundle: Path) -> None:
    """Remove the quarantine flag from what we just built (harmless if none)."""
    if shutil.which("xattr") is None:                  # pragma: no cover
        return
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(bundle)],
                   capture_output=True)


def report(bundle: Path) -> None:
    """Print a short summary: size, architecture, signature."""
    files = sum(1 for path in bundle.rglob("*") if path.is_file())
    total = sum(path.stat().st_size for path in bundle.rglob("*")
                if path.is_file())
    print(f"\n  bundle : {bundle}")
    print(f"  files  : {files}")
    print(f"  size   : {total / (1024 * 1024):.1f} MiB")

    binary = bundle / "Contents" / "MacOS" / APP_NAME
    if shutil.which("file"):                           # pragma: no cover
        completed = subprocess.run(["file", str(binary)],
                                   capture_output=True, text=True, errors="replace")
        print(f"  arch   : {(completed.stdout or '').strip()}")
    if shutil.which("codesign"):                       # pragma: no cover
        completed = subprocess.run(["codesign", "-dv", str(bundle)],
                                   capture_output=True, text=True, errors="replace")
        text = ((completed.stdout or "") + (completed.stderr or "")).strip()
        for line in text.splitlines():
            if "Signature" in line or "Identifier" in line:
                print(f"  sign   : {line.strip()}")


def finish_distribution(with_zip: bool) -> None:
    """Copy the helper files in, and optionally build the transfer ZIP."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import pack_distribution
    except Exception as exc:                           # pragma: no cover
        print(f"warning: could not load the packaging helpers: {exc}",
              file=sys.stderr)
        return
    argv = ["--dist", str(PROJECT_ROOT / "dist" / APP_BUNDLE),
            "--platform", "macos"]
    if not with_zip:
        argv.append("--no-zip")
    pack_distribution.main(argv)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    problems = preflight()
    if problems:
        print("error: cannot build here:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nRun `python scripts/build_macos.py --dry-run` to see the "
              "command that would be used.", file=sys.stderr)
        return 2

    if args.clean:
        for folder in (PROJECT_ROOT / "build" / "macos",
                       PROJECT_ROOT / "dist" / APP_BUNDLE):
            if folder.exists():
                print(f"removing {folder}")
                shutil.rmtree(folder, ignore_errors=True)

    # A stale spec or workpath silently produces a bundle missing new code, so
    # retire them the same way the Windows script does.
    work = PROJECT_ROOT / "build" / "macos"
    if work.exists():
        retired = work.with_name(f"macos_previous_{datetime.now():%Y%m%d-%H%M%S}")
        try:
            work.rename(retired)
            print(f"previous workpath moved aside: {retired.name}")
        except OSError:                                # pragma: no cover
            print("note: could not move the workpath aside; continuing",
                  file=sys.stderr)

    target = PROJECT_ROOT / "dist" / APP_BUNDLE
    if target.exists():
        retired = target.with_name(f"{APP_BUNDLE}_previous")
        shutil.rmtree(retired, ignore_errors=True)
        try:
            target.rename(retired)
        except OSError as exc:                         # pragma: no cover
            print(f"error: could not move {target} aside ({exc})",
                  file=sys.stderr)
            return 1

    command = build_command(args)
    print(" ".join(command))

    if args.dry_run:
        return 0

    completed = subprocess.run(command, cwd=str(PROJECT_ROOT))
    if completed.returncode != 0:
        return completed.returncode

    if not target.is_dir():                            # pragma: no cover
        print(f"error: the build did not produce {target}", file=sys.stderr)
        return 1

    strip_quarantine(target)
    sign_bundle(target, args.codesign_identity)
    report(target)

    finish_distribution(with_zip=args.zip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
