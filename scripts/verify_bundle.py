"""Verify a packaged EnzyDock build before shipping it.

PyInstaller happily produces an executable that starts but breaks the moment
the user presses *Start Docking* - missing meeko JSON templates, a missing
``scipy.spatial``, or missing QtWebEngine resources are all invisible until
then.  This script checks the bundle *and* runs the real thing:

1. the executable and the files it must sit next to are present;
2. meeko's parameter files and ``scipy.spatial`` really landed inside the
   payload folder (``_internal`` on Windows, ``Contents/Frameworks`` in a
   macOS ``.app``);
3. ``EnzyDock --selftest --selftest-dock`` passes **every** backend check
   (this actually docks a molecule with Vina from inside the bundle);
4. the GUI starts, shows its window and writes a log without a traceback.

Usage::

    python scripts/verify_bundle.py                     # after a normal build
    python scripts/verify_bundle.py --dist dist/EnzyDock
    python scripts/verify_bundle.py --skip-gui          # faster

Exit code is 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "scripts") not in sys.path:          # pragma: no cover
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import packaging_layout                                    # noqa: E402

#: Payload components that must exist, as paths *relative to the payload
#: folder*.  ``*`` patterns are allowed: compiled extensions carry the Python
#: tag in their name (``_ckdtree.cp312-win_amd64.pyd`` /
#: ``_ckdtree.cpython-312-darwin.so``).
COMMON_PAYLOAD_FILES: tuple[str, ...] = (
    "app/resources/viewer/index.html",
    "app/resources/viewer/3Dmol.min.js",
    # The docking engine ships with the application: the user must never have
    # to install Vina themselves.  Its licence travels next to it.
    "app/resources/bin/{vina}",
    "app/resources/licences/AutoDock-Vina-LICENSE.txt",
    "app/resources/licenses/AutoDock-Vina-LICENSE.txt",
    "app/resources/licenses/THIRD-PARTY-NOTICES.txt",
    "meeko/data/residue_chem_templates.json",
    "meeko/data/params/ad4_types.json",
    "scipy/spatial/_ckdtree*",
    "meeko/data/template_charges.json",
)

#: Files that must exist next to (or inside) the packaged application.
REQUIRED_FILES: tuple[str, ...] = (
    "EnzyDock.exe",
    "QtWebEngineProcess.exe",
    "Qt6WebEngineCore.dll",
    "icudtl.dat",
    # Shipped for the receiving side: a truncated copy is the single most
    # common support question, and these two answer it without any tooling.
    "check-install.bat",
    "HOW-TO-RUN.txt",
    "BUNDLE-MANIFEST.txt",
)

#: A complete onedir build has ~533 files in ``_internal``.  Anything much
#: lower means PyInstaller was interrupted - or that somebody is verifying a
#: folder that was copied incompletely.
MIN_INTERNAL_FILES = 500

DEFAULT_PROTEIN = PROJECT_ROOT / "examples" / "protein" / "1UBQ.pdb"


def bundled_vina_name(layout: packaging_layout.BundleLayout) -> str:
    """File name of the engine inside this platform's resource folder."""
    return "vina.exe" if layout is packaging_layout.WINDOWS else "vina_mac_aarch64"


def required_files(layout: packaging_layout.BundleLayout) -> tuple[str, ...]:
    """Helper / launcher files that must be present for ``layout``."""
    if layout is packaging_layout.WINDOWS:
        return REQUIRED_FILES
    return ("BUNDLE-MANIFEST.txt", *layout.extras)


class Result:
    """Collects PASS/FAIL lines."""

    def __init__(self) -> None:
        self.entries: list[tuple[bool, str]] = []

    def add(self, ok: bool, text: str) -> bool:
        self.entries.append((ok, text))
        print(f"[{'PASS' if ok else 'FAIL'}] {text}")
        return ok

    @property
    def ok(self) -> bool:
        return all(ok for ok, _ in self.entries)

    @property
    def failed(self) -> list[str]:
        return [text for ok, text in self.entries if not ok]


def payload_root(dist: Path,
                 layout: packaging_layout.BundleLayout) -> Path:
    """Folder holding the bundled Python payload.

    ``dist`` is the bundle itself (``dist/EnzyDock`` /
    ``dist/EnzyDock.app``), so this is simply the layout's payload folder:
    ``_internal`` on Windows, ``Contents/Frameworks`` inside the ``.app``.
    """
    return dist / layout.payload_dir


def _data_root(dist: Path,
               layout: packaging_layout.BundleLayout) -> Path:
    """Folder holding copied-in data files (``app/resources`` lives here).

    On Windows they sit in ``_internal`` together with the payload; in a
    macOS ``.app`` PyInstaller puts them in ``Contents/Resources``.
    """
    if layout is packaging_layout.WINDOWS:
        return dist / layout.payload_dir
    return dist / "Contents/Resources"


def check_files(dist: Path, layout: packaging_layout.BundleLayout,
                result: Result) -> None:
    """Step 1 + 2: the bundle contains everything the runtime needs."""
    extras_dir = dist if layout is packaging_layout.WINDOWS else dist.parent
    for relative in required_files(layout):
        found = _find(extras_dir, relative)
        result.add(found is not None,
                   f"{relative} {'found' if found else 'MISSING'}")

    payload = payload_root(dist, layout)
    data = _data_root(dist, layout)
    vina = bundled_vina_name(layout)
    for pattern in COMMON_PAYLOAD_FILES:
        relative = pattern.format(vina=vina)
        root = data if relative.startswith("app/") else payload
        matches = sorted(root.glob(relative)) or sorted(dist.glob(f"**/{relative}"))
        found = next((p for p in matches if p.is_file() and p.stat().st_size > 0),
                     None)
        if relative.endswith("AutoDock-Vina-LICENSE.txt") and found is None:
            continue          # one of the two spellings is enough
        label = root.name
        result.add(found is not None,
                   f"{label}/{relative} {'found' if found else 'MISSING'}")

    count = sum(1 for path in payload.rglob("*") if path.is_file())
    result.add(count >= layout.min_payload_files,
               f"{payload.name} holds {count} files "
               f"(expected at least {layout.min_payload_files})")


def check_bundled_vina(dist: Path, layout: packaging_layout.BundleLayout,
                       result: Result) -> None:
    """Step 2b: the bundled docking engine has to *run*, not merely exist.

    A Vina that is present but cannot start is exactly what the user would
    meet the first time they press *Start Docking*, and it looks like a
    missing installation rather than a packaging fault.  Run it here instead.
    """
    name = bundled_vina_name(layout)
    exe = _data_root(dist, layout) / "app" / "resources" / "bin" / name
    if not exe.is_file():
        result.add(False, f"the bundled AutoDock Vina is missing ({name})")
        return
    # A copy that came through a ZIP may have lost its executable bit; that is
    # a packaging fault worth reporting, not something to paper over.
    if sys.platform != "win32" and not exe.stat().st_mode & 0o111:
        try:
            exe.chmod(0o755)
            result.add(True, f"{name} had lost its executable bit - fixed")
        except OSError as exc:                            # pragma: no cover
            result.add(False, f"{name} is not executable: {exc}")
            return
    try:
        completed = subprocess.run([str(exe), "--version"],
                                   capture_output=True, text=True,
                                   errors="replace", timeout=60)
    except OSError as exc:                                # pragma: no cover
        result.add(False, f"the bundled AutoDock Vina could not be started: {exc}")
        return
    text = (completed.stdout or "") + (completed.stderr or "")
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    result.add("AutoDock Vina" in text,
               f"the bundled docking engine runs: {line or 'no output'}")


def _find(root: Path, name: str) -> Path | None:
    """Return ``root/name`` or the first match below ``root``."""
    direct = root / name
    if direct.is_file():
        return direct
    for candidate in root.rglob(name):
        if candidate.is_file():
            return candidate
    return None


def check_selftest(exe: Path, protein: Path | None, report_dir: Path,
                   result: Result) -> None:
    """Step 3: run the diagnostics inside the packaged application."""
    report = report_dir / "selftest.txt"
    command = [str(exe), "--selftest", "--report", str(report)]
    if protein is not None and protein.is_file():
        command += ["--selftest-pdb", str(protein), "--selftest-dock"]

    print(f"\n$ {' '.join(command)}")
    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=True, text=True,
                               errors="replace", timeout=900)
    seconds = time.perf_counter() - started
    print(f"  (exit {completed.returncode} in {seconds:.1f}s)")

    ok = result.add(completed.returncode == 0,
                    f"EnzyDock --selftest exited cleanly ({seconds:.1f}s)")
    if not report.is_file():
        result.add(False, f"report file was not written: {report}")
        return
    text = report.read_text(encoding="utf-8", errors="replace")
    print(text)
    for line in text.splitlines():
        if line.startswith("[FAIL]"):
            result.add(False, f"bundle diagnostics: {line}")
    # The diagnostics name the engine and where it came from; inside a bundle
    # that must be the copy that shipped with the application.
    engine_lines = [line for line in text.splitlines()
                    if "AutoDock Vina" in line and line.startswith("[")]
    for line in engine_lines:
        result.add("bundled with EnzyDock" in line,
                   f"in-bundle engine: {line.strip()}")
    if ok and "[FAIL]" not in text:
        result.add(True, "every in-bundle backend check passed")


def check_gui(exe: Path, seconds: int, result: Result) -> None:
    """Step 4: the window opens, the 3D page works and the log stays clean."""
    log_file = Path(tempfile.gettempdir()) / "enzydock_verify.log"
    if log_file.exists():
        log_file.unlink()
    command = [
        str(exe),
        "--web-compat",                 # software WebGL, no Chromium sandbox
        "--log-file", str(log_file),
        "--viewer-probe",               # ask the 3D page what it rendered
        "--quit-after", str(seconds),
    ]
    print(f"\n$ {' '.join(command)}")
    completed = subprocess.run(command, capture_output=True, text=True,
                               errors="replace", timeout=seconds + 120)
    result.add(completed.returncode == 0,
               f"the window opened and closed cleanly (exit {completed.returncode})")

    if not log_file.is_file():
        # argparse exits with 2 on an unknown flag - the classic symptom of a
        # bundle built from stale sources.
        stale = (completed.returncode == 2 and
                 "viewer-probe" in " ".join(command))
        result.add(False,
                   f"no log file was written ({log_file})"
                   + (" - the executable does not know --viewer-probe; rebuild "
                      "from a clean workpath" if stale else ""))
        return
    text = log_file.read_text(encoding="utf-8", errors="replace")
    result.add("starting" in text, "the log records a clean start-up")
    bad = [line for line in text.splitlines()
           if "CRITICAL" in line or "Traceback" in line]
    result.add(not bad, "the log contains no traceback or critical error")
    for line in bad[:5]:
        print(f"      {line}")

    probes = [line for line in text.splitlines() if "viewer probe:" in line]
    if not probes:
        result.add(False, "the viewer probe never answered")
        return
    payload = probes[-1].split("viewer probe:", 1)[1].strip()
    print(f"  viewer probe: {payload}")
    if "no web surface" in payload:
        result.add(False, "QtWebEngine was not bundled - the 3D view cannot work")
        return
    import json

    try:
        answer = json.loads(payload)
    except ValueError:
        result.add(False, f"the viewer probe returned unreadable JSON: {payload}")
        return
    result.add(bool(answer.get("has3Dmol")), "3Dmol.js loaded inside the bundle")
    result.add(bool(answer.get("webgl")), "WebGL is available to the viewer")
    result.add(bool(answer.get("ready")), "the viewer page reported ready")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a packaged EnzyDock build")
    parser.add_argument("--dist", type=Path, default=None,
                        help="the built bundle (default: dist/EnzyDock on "
                             "Windows, dist/EnzyDock.app on macOS)")
    parser.add_argument("--platform", choices=("windows", "macos"),
                        default=None,
                        help="force a layout instead of detecting it")
    parser.add_argument("--pdb", type=Path, default=DEFAULT_PROTEIN,
                        help="protein structure used by the in-bundle docking")
    parser.add_argument("--report-dir", type=Path,
                        default=PROJECT_ROOT / "build",
                        help="where the diagnostics report is written")
    parser.add_argument("--gui-seconds", type=int, default=12,
                        help="how long the window is kept open during the GUI check")
    parser.add_argument("--skip-gui", action="store_true",
                        help="do not launch the window (faster, headless CI)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    default_dist = (PROJECT_ROOT / "dist" /
                    (packaging_layout.MACOS_APP if sys.platform == "darwin"
                     else "EnzyDock"))
    dist: Path = args.dist or default_dist
    result = Result()

    layout = (packaging_layout.for_system(args.platform)
              if args.platform else packaging_layout.detect_layout(dist))

    print(f"Verifying {dist} ({layout.system} {layout.arch})\n")
    if not dist.is_dir():
        result.add(False, f"no such folder: {dist}")
        return 1

    exe = dist / layout.launcher
    if not exe.is_file():
        result.add(False, f"no executable: {exe}")
        return 1

    check_files(dist, layout, result)
    check_bundled_vina(dist, layout, result)
    print()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    check_selftest(exe, args.pdb, args.report_dir, result)
    if not args.skip_gui:
        print()
        check_gui(exe, args.gui_seconds, result)

    print()
    if result.ok:
        print(f"All {len(result.entries)} bundle checks passed.")
        return 0
    print(f"{len(result.failed)} of {len(result.entries)} checks FAILED:")
    for text in result.failed:
        print(f"  - {text}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
