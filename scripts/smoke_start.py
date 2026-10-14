"""Development helper: start the GUI, keep it open for a few seconds, then quit.

Used to smoke-test that a real (non-offscreen) Qt application - including the
QtWebEngine based viewer - starts without exceptions::

    .venv\\Scripts\\python.exe scripts\\smoke_start.py [--seconds 4] [--no-web]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _grab(window, path: Path) -> None:
    """Render the window into a PNG (used to eyeball the layout in CI)."""
    pixmap = window.grab()
    pixmap.save(str(path), "PNG")
    print(f"[smoke] screenshot saved: {path} ({pixmap.width()}x{pixmap.height()})")


_PROBE_JS = (
    "JSON.stringify({"
    " has3Dmol: (typeof $3Dmol !== 'undefined'),"
    " ready: !!(window.enzydockViewer && window.enzydockViewer.isReady()),"
    " viewer: (window.enzydockViewer ? window.enzydockViewer.describe() : null),"
    # The viewport the page really got: a canvas wider than this is why a
    # structure ends up small and pushed into a corner.
    " viewport: {w: window.innerWidth, h: window.innerHeight,"
    "   dpr: window.devicePixelRatio || 1,"
    "   bodyW: document.body.scrollWidth, bodyH: document.body.scrollHeight},"
    " legend: (document.getElementById('legend') || {}).innerHTML || '',"
    " hud: (document.getElementById('hud') || {}).innerText || '',"
    " overlay: (document.getElementById('overlay') || {}).innerText || '',"
    " overlayHidden: !!(document.getElementById('overlay') &&"
    "   document.getElementById('overlay').classList.contains('hidden'))"
    "});"
)


def _probe_viewer(window, store: dict | None = None, after=None) -> None:
    """Ask the embedded page what it actually rendered (Step 7 diagnostics)."""
    page = window.viewer._web.page() if window.viewer._web is not None else None
    if page is None:
        print("[smoke] viewer probe: no web surface (QtWebEngine disabled)")
        return

    def report(value):
        print(f"[smoke] viewer probe: {value}")
        if store is not None:
            store["value"] = value
        if after is not None:
            # Grab at exactly the moment that was measured, so the PNG and the
            # numbers describe the same layout.
            after()

    page.runJavaScript(_PROBE_JS, report)


def _check_viewer_render(store: dict) -> bool:
    """The 3D scene must contain the receptor *and* the pose.

    A ligand-only scene is the classic silent failure: the cartoon never made
    it to the canvas and the user sees a floating molecule with no protein.
    """
    raw = store.get("value")
    if not raw:
        print("[smoke] FAILED: the 3D viewer did not answer the probe")
        return False
    import json

    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        print(f"[smoke] FAILED: unreadable probe answer ({exc}): {raw!r}")
        return False

    viewer = data.get("viewer") or {}
    print(
        "[smoke] 3D scene: models={models} atoms={atoms} surfaces={surfaces} "
        "protein={protein}({chars} chars) ligand={ligand} chars, "
        "contacts={contacts}".format(
            models=viewer.get("models"),
            atoms=viewer.get("atoms"),
            surfaces=viewer.get("surfaces"),
            protein=viewer.get("proteinVisible"),
            chars=viewer.get("proteinChars"),
            ligand=viewer.get("ligandChars"),
            contacts=viewer.get("contacts"),
        )
    )
    problems = []
    if not viewer.get("ready"):
        problems.append("the 3D canvas is not ready")
    if not viewer.get("hasProtein") or not viewer.get("proteinChars"):
        problems.append("the receptor is not in the scene")
    if (viewer.get("models") or 0) < 2:
        problems.append("fewer than two models were drawn (receptor + pose)")
    if not viewer.get("overlayHidden"):
        problems.append("the 'nothing loaded' overlay is still on screen")
    if problems:
        print("[smoke] FAILED: " + "; ".join(problems))
        return False
    print("[smoke] 3D scene holds the receptor and the docked pose - OK")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--web-compat", action="store_true",
                        help="Chromium compatibility flags (VMs / remote sessions)")
    parser.add_argument("--screenshot", type=Path, default=None,
                        help="save a PNG of the window before quitting")
    parser.add_argument("--prepare-smiles", default=None,
                        help="run the real ligand preparation pipeline on a SMILES "
                             "string (worker thread + progress bar) before quitting")
    parser.add_argument("--protein", type=Path, default=None,
                        help="load this structure and run 'Prepare Protein' "
                             "(worker thread, Step 4) before quitting")
    parser.add_argument("--dock", type=Path, default=None,
                        help="load this structure and run a complete docking "
                             "(Steps 3-6: prepare, dock, parse) before quitting")
    parser.add_argument("--ligand", default="CC(=O)Oc1ccccc1C(=O)O",
                        help="SMILES docked when --dock is used")
    parser.add_argument("--exhaustiveness", type=int, default=8,
                        help="exhaustiveness used with --dock")
    parser.add_argument("--export", type=Path, default=None,
                        help="with --dock: export every pose, the CSV table and "
                             "the report into this folder (Step 8)")
    parser.add_argument("--viewer-probe", action="store_true",
                        help="ask the embedded page whether 3Dmol.js really "
                             "loaded and the scene was applied")
    parser.add_argument("--viewer-all", action="store_true",
                        help="switch on Pocket, Residues, Hbonds and Contacts "
                             "before the screenshot")
    args = parser.parse_args(argv)

    from app.main import configure_webengine_environment

    configure_webengine_environment(args.web_compat, args.no_web)

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    app.setApplicationName("EnzyDock")

    from app.gui.main_window import MainWindow
    from app.gui.styles import apply_theme
    from app.utils.logger import setup_logging
    from app.utils.settings import settings

    setup_logging()
    apply_theme(app)

    window = MainWindow(settings())
    window.show()

    print(f"[smoke] window shown: {window.windowTitle()!r}")
    print(f"[smoke] web viewer available: {window.viewer.is_web_available()}")

    pipeline = bool(args.prepare_smiles or args.protein or args.dock)

    if args.dock:
        # Steps 3-6 in one go: load the protein, set a ligand, centre the box
        # on the receptor and press START DOCKING.
        if not window.protein_panel.load_file(args.dock):
            print(f"[smoke] FAILED: could not read {args.dock}")
            return 1
        if not window.ligand_panel.set_smiles(args.ligand):
            print(f"[smoke] FAILED: could not parse the ligand {args.ligand}")
            return 1
        summary = window.protein_panel.summary
        if summary is not None and summary.center is not None:
            window.docking_panel.set_center(
                tuple(round(value, 2) for value in summary.center)
            )
        window.docking_panel.set_exhaustiveness(args.exhaustiveness)
        window.docking_panel.request_run()

    if args.prepare_smiles:
        # Exercise the whole Step 3 chain the way a user would: type a SMILES,
        # press "Prepare Ligand", watch the worker thread finish.
        window.ligand_panel.set_smiles(args.prepare_smiles)
        window.ligand_panel._on_prepare()
    if args.protein:
        # Step 4: load a structure, press "Prepare Protein", let the worker run.
        if not window.protein_panel.load_file(args.protein):
            print(f"[smoke] FAILED: could not read {args.protein}")
            return 1
        window.protein_panel.request_prepare()
    if pipeline and window._worker is not None:
        window._worker.finished.connect(app.quit)

    if args.viewer_all:
        for name in ("pocket", "residues", "hbonds", "contacts"):
            box = window.viewer._checks.get(name)
            if box is not None:
                box.setChecked(True)

    probe_store: dict = {}

    def grab() -> None:
        if args.screenshot:
            _grab(window, args.screenshot)

    if args.screenshot:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

    def describe_widget() -> None:
        """Qt-side geometry of the web surface (1:1 with the screenshot)."""
        web = window.viewer._web
        if web is None:
            print("[smoke] viewer widget: no web surface")
            return
        origin = web.mapTo(window, web.rect().topLeft())
        print(f"[smoke] viewer widget: {web.width()}x{web.height()} px at "
              f"({origin.x()},{origin.y()}) in a "
              f"{window.width()}x{window.height()} window, "
              f"dpr={web.devicePixelRatioF():g}")
        grab()

    if args.viewer_probe:
        # Give the render process time to come up, then ask it what it drew.
        # The screenshot is taken in the same callback so that the PNG and the
        # measurements cannot describe two different layouts.
        QTimer.singleShot(
            max(0, int(args.seconds * 1000) - 2500),
            lambda: _probe_viewer(window, probe_store, after=describe_widget),
        )
    elif args.screenshot:
        QTimer.singleShot(int(args.seconds * 1000) // 2, grab)

    if pipeline:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
    else:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
    code = app.exec()

    if args.prepare_smiles:
        prepared = window._prepared_ligand
        if prepared is None or not prepared.is_ready:
            print("[smoke] FAILED: the ligand was not prepared")
            return 1
        print(f"[smoke] ligand ready: {prepared.name}, "
              f"{prepared.rotatable_bonds} torsions, writer={prepared.converter}")
        print(f"[smoke] PDBQT: {prepared.pdbqt_path}")

    if args.protein:
        receptor = window._prepared_receptor
        if receptor is None or not receptor.is_ready:
            print("[smoke] FAILED: the receptor was not prepared")
            return 1
        print(f"[smoke] receptor ready: {receptor.name}, "
              f"{receptor.residue_count} residues, {receptor.atom_count} atoms, "
              f"{receptor.hydrogen_count} H ({receptor.protonation}), "
              f"writer={receptor.converter}")
        print(f"[smoke] PDBQT: {receptor.pdbqt_path}")

    if args.dock:
        result = window._docking_result
        if result is None or not result.poses:
            print("[smoke] FAILED: docking produced no poses")
            return 1
        print(f"[smoke] docking finished: {result.engine}, "
              f"{len(result.poses)} poses, best {result.best_affinity:.3f} kcal/mol, "
              f"{result.elapsed_seconds:.1f}s")
        print(f"[smoke] workspace: {result.workspace}")
        print(f"[smoke] best pose file: {result.poses[0].pose_file}")
        print(f"[smoke] log: {result.log_file}")
        from app.visualization.interactions import describe

        scene = window.viewer.scene
        print(f"[smoke] viewer scene: {len(scene.pocket_residues)} pocket residues "
              f"({scene.pocket_source}), {len(scene.interactions)} interactions, "
              f"{len(scene.hbonds)} H-bonds, {len(scene.labels)} labels")
        print(f"[smoke] interactions: {describe(scene.interactions)}")
        print(f"[smoke] listed in the panel: "
              f"{len(window.result_panel.interactions)} interactions")

        auto_report = Path(result.workspace) / "results" / "report.md"
        if not auto_report.is_file():
            print("[smoke] FAILED: the run did not produce results/report.md")
            return 1
        report_text = auto_report.read_text(encoding="utf-8", errors="replace")
        if "## Interactions of pose" not in report_text:
            print("[smoke] FAILED: report.md has no interaction section")
            return 1
        print(f"[smoke] auto report: {auto_report} "
              f"({auto_report.stat().st_size:,} bytes, interactions included)")

    if args.viewer_probe and args.dock:
        if not _check_viewer_render(probe_store):
            return 1

    if args.export:
        if window._docking_result is None:
            print("[smoke] FAILED: --export needs a finished run (use --dock)")
            return 1
        from app.export import ExportError, export_all_poses, export_best_pose

        try:
            written = export_all_poses(window._docking_result, args.export)
            written += export_best_pose(window._docking_result,
                                        args.export / "best_pose.pdb")
        except ExportError as exc:
            print(f"[smoke] FAILED export: {exc.message}")
            print(f"[smoke]   details: {exc.details}")
            return 1
        print(f"[smoke] exported {len(written)} file(s) to {args.export}:")
        for path in written:
            print(f"[smoke]   - {path.name} ({path.stat().st_size:,} bytes)")

    print(f"[smoke] exited cleanly with code {code}")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
