"""EnzyDock application entry point.

Run from a source checkout with::

    python -m app.main            # recommended
    python run.py                 # equivalent shortcut

or, after packaging, by double clicking ``EnzyDock.exe`` (Windows) or
``EnzyDock.app`` (macOS).

The module is intentionally small: it configures logging, creates the
``QApplication``, installs a global exception hook that turns unhandled
tracebacks into a friendly dialog, and shows :class:`MainWindow`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Sequence

# Make `python app/main.py` work as well as `python -m app.main`.
if __package__ in (None, ""):                          # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "app"

from app import __version__                            # noqa: E402
from app.utils.logger import get_logger, setup_logging  # noqa: E402
from app.utils.paths import is_frozen                   # noqa: E402

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Return the command line parser (a GUI app still accepts a few flags)."""
    parser = argparse.ArgumentParser(
        prog="EnzyDock",
        description="EnzyDock - molecular docking platform",
    )
    parser.add_argument("--version", action="version",
                        version=f"EnzyDock {__version__}")
    parser.add_argument("--debug", action="store_true",
                        help="verbose console logging (DEBUG level)")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="write the log to this file instead of the default")
    parser.add_argument("--no-webengine", action="store_true",
                        help="start without the embedded 3D viewer (diagnostics)")
    parser.add_argument("--web-compat", action="store_true",
                        help="start the embedded viewer in compatibility mode "
                             "(no Chromium sandbox, software WebGL); use this "
                             "if the 3D view stays blank in a VM or remote "
                             "desktop session")
    parser.add_argument("--selftest", action="store_true",
                        help="run the backend diagnostics and exit instead of "
                             "opening the window (writes a report with --report)")
    parser.add_argument("--selftest-pdb", type=Path, default=None,
                        metavar="FILE",
                        help="protein structure used by --selftest")
    parser.add_argument("--selftest-smiles", default=None, metavar="SMILES",
                        help="ligand used by --selftest (aspirin by default)")
    parser.add_argument("--selftest-dock", action="store_true",
                        help="with --selftest, also run one real docking")
    parser.add_argument("--report", type=Path, default=None, metavar="FILE",
                        help="with --selftest, write the report to this file "
                             "(the only readable output of a windowed build)")
    parser.add_argument("--viewer-probe", action="store_true",
                        help="ask the embedded 3D page what it rendered, write "
                             "the answer to the log and quit (packaging check)")
    parser.add_argument("--quit-after", type=float, default=None, metavar="SECONDS",
                        help="close the window after this many seconds")
    return parser


#: Seconds the window stays open for ``--viewer-probe`` without an explicit
#: ``--quit-after``: long enough for the Chromium render process to come up.
VIEWER_PROBE_SECONDS = 10.0

#: JavaScript that asks the embedded viewer page what it actually rendered.
#: Used by ``--viewer-probe`` to prove that 3Dmol.js and WebGL work inside a
#: packaged build, where there is no console to look at.
VIEWER_PROBE_JS = (
    "JSON.stringify({"
    " has3Dmol: (typeof $3Dmol !== 'undefined'),"
    " ready: !!(window.enzydockViewer && window.enzydockViewer.isReady()),"
    " webgl: (function(){try{var c=document.createElement('canvas');"
    "   return !!(c.getContext('webgl')||c.getContext('experimental-webgl'));}"
    "   catch(e){return false;}})(),"
    " overlayHidden: !!(document.getElementById('overlay') &&"
    "   document.getElementById('overlay').classList.contains('hidden')),"
    " overlay: (document.getElementById('overlay') || {}).innerText || ''"
    "});"
)


def _schedule_probe_and_quit(app, window, seconds: float, probe: bool) -> None:
    """Close the window after ``seconds``, probing the 3D page just before."""
    from PySide6.QtCore import QTimer

    if probe:
        when = max(0.0, seconds - 3.0)
        QTimer.singleShot(int(when * 1000), lambda: _probe_viewer(window))
    QTimer.singleShot(int(seconds * 1000), app.quit)


def _probe_viewer(window) -> None:
    """Ask the embedded page what it rendered and log the answer."""
    web = getattr(window.viewer, "_web", None)
    page = web.page() if web is not None else None
    if page is None:
        log.warning("viewer probe: no web surface (QtWebEngine disabled)")
        return
    page.runJavaScript(VIEWER_PROBE_JS,
                       lambda value: log.info("viewer probe: %s", value))


#: Chromium switches used by ``--web-compat``.
#:
#: ``--disable-gpu`` was removed deliberately: it switches WebGL off entirely,
#: which is exactly what the 3D viewer needs.  Instead the GPU sandbox is
#: dropped and SwiftShader is allowed as a software fallback, so the viewer
#: also works on machines without a usable GPU (most VMs and RDP sessions).
WEB_COMPAT_FLAGS = (
    "--no-sandbox --disable-gpu-sandbox --enable-unsafe-swiftshader "
    "--ignore-gpu-blocklist"
)


def configure_webengine_environment(web_compat: bool, no_webengine: bool) -> None:
    """Set the QtWebEngine environment variables *before* Qt is imported.

    QtWebEngine reads these when the Chromium engine starts, so this has to
    happen before ``QApplication`` is constructed.
    """
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if no_webengine:
        os.environ["ENZYDOCK_NO_WEBENGINE"] = "1"
        return
    if web_compat:
        existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(
            part for part in (existing, WEB_COMPAT_FLAGS) if part
        )
        os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
        log.warning("3D viewer running in compatibility mode "
                    "(GPU and Chromium sandbox disabled)")


def _install_excepthook() -> None:
    """Replace the default excepthook with a friendly error dialog.

    A bare traceback on a dialog-less console is useless for a bench
    scientist; we log the traceback and show the readable message instead.
    """
    def hook(exc_type, exc_value, exc_tb):              # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.critical("unhandled exception:\n%s", text)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                box = QMessageBox()
                box.setIcon(QMessageBox.Icon.Critical)
                box.setWindowTitle("EnzyDock - unexpected error")
                box.setText(
                    "EnzyDock hit an unexpected problem and may be in an "
                    "inconsistent state.\n\n"
                    f"{exc_type.__name__}: {exc_value}"
                )
                box.setDetailedText(text)
                box.exec()
        except Exception:                                # pragma: no cover
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def run_selftest_and_exit(args: argparse.Namespace) -> int:
    """Run the backend diagnostics, report, and return a process exit code.

    Diagnostics never touch the GUI, so this works in a packaged ``--windowed``
    build where ``stdout`` goes nowhere: the report is written to ``--report``
    (and to the log file) instead of being printed.
    """
    from app.selftest import DEFAULT_SMILES, SelftestOptions, format_report, run_checks
    from app.utils.logger import current_log_file

    options = SelftestOptions(
        pdb=args.selftest_pdb,
        smiles=args.selftest_smiles or DEFAULT_SMILES,
        dock=args.selftest_dock,
    )
    checks = run_checks(options)
    report = format_report(checks)

    if args.report is not None:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
        log.info("diagnostics report written to %s", target)
    print(report)

    failed = [check.name for check in checks if not check.ok]
    if failed:
        log.error("diagnostics failed: %s", ", ".join(failed))
        return 1
    log.info("diagnostics passed (%d checks); log file: %s",
             len(checks), current_log_file())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Start the GUI and return the process exit code."""
    args = build_parser().parse_args(argv)

    log_file = setup_logging(
        level=logging.DEBUG if args.debug else logging.INFO,
        log_file=args.log_file,
    )
    log.info("EnzyDock %s starting (python %s, frozen=%s)",
             __version__, sys.version.split()[0], is_frozen())

    if args.selftest:
        return run_selftest_and_exit(args)

    configure_webengine_environment(args.web_compat, args.no_webengine)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # QtWebEngine works best with a shared OpenGL context on Windows.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("EnzyDock")
    app.setApplicationDisplayName("EnzyDock")
    app.setOrganizationName("EnzyDock")
    app.setApplicationVersion(__version__)

    from app.gui.main_window import MainWindow
    from app.gui.styles import apply_theme
    from app.utils.settings import settings

    apply_theme(app)
    _install_excepthook()

    window = MainWindow(settings())
    window.show()

    log.info("log file: %s", log_file)

    seconds = args.quit_after
    if seconds is None and args.viewer_probe:
        seconds = VIEWER_PROBE_SECONDS
    if seconds:
        _schedule_probe_and_quit(app, window, seconds, args.viewer_probe)

    exit_code = app.exec()

    log.info("EnzyDock exited with code %s", exit_code)
    return int(exit_code)


if __name__ == "__main__":                               # pragma: no cover
    raise SystemExit(main())
