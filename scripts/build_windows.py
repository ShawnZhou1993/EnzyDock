"""Build a Windows executable with PyInstaller.

Usage (from the project root, inside the virtual environment)::

    python scripts/build_windows.py              # onedir build (recommended)
    python scripts/build_windows.py --onefile    # single .exe
    python scripts/build_windows.py --clean      # remove build/ and dist/ first

Why *onedir* is the default
---------------------------
QtWebEngine ships ~150 MB of resources (ANGLE, ICU, translations).  In
``--onefile`` mode those are unpacked into a temporary directory on every
launch, which adds several seconds to the start-up time.  ``--onedir`` keeps
them next to the executable and starts instantly; the folder is shipped inside
an installer or a ZIP archive.

The resulting binary lives in ``dist/EnzyDock/EnzyDock.exe`` (onedir) or
``dist/EnzyDock.exe`` (onefile).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "EnzyDock"
ENTRY_POINT = PROJECT_ROOT / "run.py"

#: Modules PyInstaller cannot detect because they are imported dynamically.
#:
#: Every adapter in ``app/chemistry`` imports its backend *inside* a function
#: (that is how EnzyDock falls back from meeko to OpenBabel), so the static
#: analyser cannot see them and they have to be named here.
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
    # PDBQT writers: meeko first choice, OpenBabel the optional fallback.
    "meeko",
    "meeko.polymer",
    "meeko.receptor_pdbqt",
    "meeko.molecule_pdbqt",
    "meeko.preparation",
    # meeko imports scipy.spatial *at module level*; without it the packaged
    # app writes receptor PDBQT files only after crashing.
    "scipy.spatial",
    "numpy",
]

#: Packages whose *data* files must be bundled (JSON templates, parameters).
#:
#: ``meeko`` reads ``data/residue_chem_templates.json`` and ``data/params/*.json``
#: through ``importlib.resources``.  A build without ``--collect-data meeko``
#: imports fine and only fails much later, deep inside a docking run.
COLLECT_DATA = [
    "meeko",
]

#: Data files that must sit next to the executable.
DATAS = [
    (PROJECT_ROOT / "app" / "resources", "app/resources"),
]

EXCLUDES = [
    "tkinter",
    "matplotlib",
    # meeko only needs pandas inside a try/except, so it is safe to drop.
    # gemmi must NOT be excluded: meeko.polymer imports chemtempgen, which
    # imports gemmi at module level, so dropping it kills the PDBQT writer
    # and EnzyDock silently falls back to OpenBabel.
    "pandas",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Package {APP_NAME} for Windows")
    parser.add_argument("--onefile", action="store_true",
                        help="produce a single executable (slower start-up)")
    parser.add_argument("--clean", action="store_true",
                        help="delete build/ and dist/ before building")
    parser.add_argument("--console", action="store_true",
                        help="keep a console window open (debugging builds)")
    parser.add_argument("--zip", action="store_true",
                        help="also pack the onedir build into a single, "
                             "hash-verifiable ZIP for sending to someone else")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the PyInstaller command without running it")
    return parser


def build_command(args: argparse.Namespace,
                  dist_path: Path | None = None) -> list[str]:
    """Assemble the PyInstaller command line.

    ``dist_path`` overrides where the bundle is written; it is used when the
    usual ``dist`` folder cannot be replaced in place (see :func:`main`).
    """
    command: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", APP_NAME,
        "--onedir" if not args.onefile else "--onefile",
        "--windowed" if not args.console else "--console",
    ]
    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    for package in COLLECT_DATA:
        command += ["--collect-data", package]
    for source, destination in DATAS:
        if source.exists():
            command += ["--add-data", f"{source}{os_sep()}{destination}"]
    for module in EXCLUDES:
        command += ["--exclude-module", module]

    command += ["--distpath", str(dist_path or PROJECT_ROOT / "dist"),
                "--workpath", str(PROJECT_ROOT / "build"),
                "--specpath", str(PROJECT_ROOT / "build"),
                str(ENTRY_POINT)]
    return command


def _retire(target: Path) -> bool:
    """Move ``target`` out of the way instead of deleting it.

    Deleting a 600 MB folder with thousands of files can be refused by
    managed/sandboxed environments; a rename is a single metadata operation.

    Returns ``True`` when ``target`` no longer exists, i.e. when PyInstaller
    is free to create it again.
    """
    if not target.exists():
        return True
    retired = target.with_name(f"{target.name}_previous")
    if retired.exists():
        try:
            shutil.rmtree(retired)
        except OSError:                                   # pragma: no cover
            retired = target.with_name(
                f"{target.name}_previous_{datetime.now():%Y%m%d-%H%M%S}")
    try:
        target.rename(retired)
        print(f"previous build moved aside: {retired}")
        return True
    except OSError as exc:
        # Renaming can fail too (a file inside is open, a policy forbids it).
        # The caller falls back to building elsewhere and copying the result.
        print(f"note: could not move {target} aside ({exc.strerror or exc})",
              file=sys.stderr)
        return False


def sync_tree(source: Path, target: Path) -> int:
    """Make ``target`` identical to ``source`` *without deleting* ``target``.

    Used when the previous bundle cannot be removed: PyInstaller insists on
    recreating its output folder from scratch, but copying files over the old
    ones is always allowed.  Files that only exist in ``target`` are removed
    one by one, which is what makes the result byte-identical to ``source``.

    Returns the number of files copied or removed.
    """
    import filecmp

    changed = 0
    wanted: set[Path] = set()
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        wanted.add(relative)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or not filecmp.cmp(path, destination,
                                                       shallow=False):
            shutil.copy2(path, destination)
            changed += 1

    for path in sorted(target.rglob("*"), reverse=True):
        relative = path.relative_to(target)
        if path.is_file() and relative not in wanted:
            path.unlink()
            changed += 1
        elif path.is_dir() and not any(path.iterdir()):
            if relative not in wanted:
                path.rmdir()
    return changed


def discard(path: Path) -> bool:
    """Best-effort removal of a staging folder.  Never fatal.

    Some managed environments route deletions through a recycle bin that
    refuses large trees, and the refusal can end the process outright - which
    would skip everything that still has to happen after a build (copying the
    helper files in, writing the manifest).  Returns ``True`` when the folder
    is gone.
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except BaseException as exc:                          # noqa: BLE001
        print(f"note: {path.name} could not be removed ({exc}); "
              "delete it by hand.", file=sys.stderr)
    if path.exists():
        print(f"note: {path.name} is still there - delete it by hand to "
              "reclaim the space.", file=sys.stderr)
        return False
    return True


def os_sep() -> str:
    """PyInstaller's ``--add-data`` separator is ``;`` on Windows, ``:`` else."""
    return ";" if sys.platform.startswith("win") else ":"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not sys.platform.startswith("win"):
        print("warning: this script targets Windows; continuing anyway.",
              file=sys.stderr)

    if args.clean:
        for folder in (PROJECT_ROOT / "build", PROJECT_ROOT / "dist"):
            if folder.exists():
                print(f"removing {folder}")
                try:
                    shutil.rmtree(folder)
                except OSError as exc:
                    print(f"warning: could not remove {folder}: {exc}",
                          file=sys.stderr)

    # 1. Move the previous *analysis* out of the way.  PyInstaller decides
    #    whether to rebuild the PYZ by comparing source mtimes with the cached
    #    archive, and a file edited *while a build was running* can end up
    #    older than the cache - the result is a silently stale executable.
    #    A full analysis costs ~90 s; shipping a stale binary costs much more.
    _retire(PROJECT_ROOT / "build" / APP_NAME)

    # 2. PyInstaller recreates its output folder from scratch, so an existing
    #    one has to go first.  Moving it aside is the cheapest way - but if
    #    even that is refused (a file inside is open, a policy blocks it), the
    #    build is written to a staging folder and copied into place at the end.
    dist_root = PROJECT_ROOT / "dist"
    target_dir = dist_root / APP_NAME
    staging: Path | None = None
    if not _retire(target_dir):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        staging = dist_root / f"_staging_{stamp}"
        print(f"building into {staging.name} and syncing it over "
              f"{target_dir.name} afterwards")

    command = build_command(args, dist_path=staging or dist_root)
    print(" ".join(command))

    if args.dry_run:
        return 0

    if shutil.which("pyinstaller") is None and _module_missing("PyInstaller"):
        print("error: PyInstaller is not installed. Run:\n"
              "    .venv\\Scripts\\python.exe -m pip install -r requirements-dev.txt",
              file=sys.stderr)
        return 1

    completed = subprocess.run(command, cwd=str(PROJECT_ROOT))
    if completed.returncode != 0:
        return completed.returncode

    if args.onefile:                                      # pragma: no cover
        print(f"\nBuild succeeded: {dist_root / f'{APP_NAME}.exe'}")
        return 0

    if staging is not None:
        fresh = staging / APP_NAME
        if not fresh.is_dir():                            # pragma: no cover
            print(f"error: the build did not produce {fresh}", file=sys.stderr)
            return 1
        changed = sync_tree(fresh, target_dir)
        print(f"synced {changed} file(s) from {fresh.name} into {target_dir}")
        discard(staging)

    print(f"\nBuild succeeded: {target_dir / f'{APP_NAME}.exe'}")
    finish_distribution(with_zip=args.zip)
    return 0


def finish_distribution(with_zip: bool) -> None:
    """Add the helper files, and optionally a transfer-ready ZIP.

    Every onedir build ships ``check-install.bat`` and ``HOW-TO-RUN.txt``.  The
    most common support question by far is "it says it cannot load
    python312.dll" - i.e. a folder that was copied incompletely.  Both files
    exist to make that visible on the receiving computer without any tooling.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import pack_distribution
    except Exception as exc:                             # pragma: no cover
        print(f"warning: could not load the packaging helpers: {exc}",
              file=sys.stderr)
        return
    argv = ["--dist", str(PROJECT_ROOT / "dist" / APP_NAME)]
    if not with_zip:
        argv.append("--no-zip")
    pack_distribution.main(argv)


def _module_missing(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is None


if __name__ == "__main__":
    raise SystemExit(main())
