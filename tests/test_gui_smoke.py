"""Smoke tests for the GUI skeleton (Step 2).

These tests create real widgets on the offscreen platform.  They exist to
prove that the window can be built, that all four functional areas are
present and that the panels round-trip user input into plain data objects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.docking.docking_config import BoxConfig, DockingParameters
from app.gui.docking_panel import DockingPanel
from app.gui.ligand_panel import LigandPanel
from app.gui.main_window import MainWindow
from app.gui.protein_panel import ProteinPanel
from app.gui.result_panel import ResultPanel
from app.gui.viewer import MoleculeViewer
from app.utils.settings import AppSettings


# ------------------------------------------------------------------- main window
def test_main_window_builds(qapp) -> None:
    window = MainWindow(AppSettings())
    assert window.windowTitle().startswith("EnzyDock")
    assert window.protein_panel is not None
    assert window.ligand_panel is not None
    assert window.docking_panel is not None
    assert window.run_bar is not None
    assert window.result_panel is not None
    assert window.viewer is not None
    assert window.log_panel is not None


def test_main_window_has_all_four_sections(qapp) -> None:
    window = MainWindow(AppSettings())
    titles = {
        window.protein_panel.title(),
        window.ligand_panel.title(),
        window.docking_panel.title(),
        window.result_panel.title(),
    }
    assert titles == {"Protein", "Ligand / Substrate", "Docking", "Result"}


def test_main_window_close_persists_geometry(qapp, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ENZYDOCK_DATA_DIR", str(tmp_path))
    settings = AppSettings()
    window = MainWindow(settings)
    window.close()
    assert settings.value("window/geometry") is not None


# ---------------------------------------------------------------- protein panel
def test_protein_panel_loads_and_summarises(qapp, mini_pdb: Path) -> None:
    panel = ProteinPanel(AppSettings())
    assert panel.load_file(mini_pdb) is True
    assert panel.path == mini_pdb
    assert panel.summary is not None
    assert panel.summary.atom_count == 14
    assert panel.selected_chains() == ["A"]
    assert panel._prepare_btn.isEnabled() is True


def test_protein_panel_reports_bad_file(qapp, tmp_path: Path, monkeypatch) -> None:
    panel = ProteinPanel(AppSettings())
    # A file that exists but is not a structure -> returns False (dialog suppressed)
    monkeypatch.setattr("app.gui.protein_panel.show_error", lambda *a, **k: None)
    bad = tmp_path / "bad.pdb"
    bad.write_text("nonsense\n", encoding="utf-8")
    assert panel.load_file(bad) is False
    assert panel.path is None


def test_protein_options_round_trip(qapp) -> None:
    panel = ProteinPanel(AppSettings())
    panel._remove_waters.setChecked(False)
    panel._remove_hetero.setChecked(True)
    options = panel.options()
    assert options.remove_waters is False
    assert options.remove_hetero is True


# ----------------------------------------------------------------- ligand panel
def test_ligand_panel_parses_smiles(qapp) -> None:
    panel = LigandPanel(AppSettings())
    assert panel.set_smiles("CC(=O)Oc1ccccc1C(=O)O") is True
    assert panel.summary is not None
    assert panel.summary.formula == "C9H8O4"
    assert panel.smiles.startswith("CC(=O)Oc1")


def test_ligand_panel_rejects_invalid_smiles(qapp, monkeypatch) -> None:
    panel = LigandPanel(AppSettings())
    monkeypatch.setattr("app.gui.ligand_panel.show_error", lambda *a, **k: None)
    assert panel.set_smiles("C(C") is False
    assert panel.summary is None


def test_ligand_panel_loads_file(qapp, aspirin_mol: Path) -> None:
    panel = LigandPanel(AppSettings())
    assert panel.load_file(aspirin_mol) is True
    assert panel.summary is not None
    assert panel.summary.name == "aspirin"


# ---------------------------------------------------------------- docking panel
def test_docking_panel_defaults(qapp) -> None:
    panel = DockingPanel(AppSettings())
    assert panel.parameters().exhaustiveness == 8
    assert panel.parameters().num_poses == 10
    assert panel.parameters().energy_range == pytest.approx(3.0)
    assert panel.parameters().seed is None
    assert panel.box().size == (22.0, 22.0, 22.0)


def test_docking_panel_round_trips_box_and_params(qapp) -> None:
    panel = DockingPanel(AppSettings())
    panel.set_center((10.5, -2.0, 3.25))
    panel._size[0].setValue(30.0)
    panel._exhaustiveness.setValue(16)
    panel._seed.setValue(1234)

    box = panel.box()
    assert box.center == pytest.approx((10.5, -2.0, 3.25))
    assert box.size[0] == pytest.approx(30.0)
    params = panel.parameters()
    assert params.exhaustiveness == 16
    assert params.seed == 1234
    params.validate()


def test_docking_panel_reset(qapp) -> None:
    panel = DockingPanel(AppSettings())
    panel.set_center((5, 5, 5))
    panel.reset_box()
    assert panel.box().center == (0.0, 0.0, 0.0)
    assert panel.box().size == (22.0, 22.0, 22.0)


def test_docking_panel_emits_run(qapp) -> None:
    panel = DockingPanel(AppSettings())
    received: list[tuple[BoxConfig, DockingParameters]] = []
    panel.run_requested.connect(lambda box, params: received.append((box, params)))
    panel.request_run()
    assert len(received) == 1
    box, params = received[0]
    assert isinstance(box, BoxConfig)
    assert isinstance(params, DockingParameters)


def test_run_bar_progress(qapp) -> None:
    from app.gui.docking_panel import RunBar

    bar = RunBar()
    bar.set_running(True)
    assert bar._cancel_btn.isHidden() is False      # shown
    assert bar._run_btn.isHidden() is True          # hidden while running
    bar.set_progress(40, "Running AutoDock Vina ...")
    assert bar._progress.value() == 40
    bar.set_progress(-1, "Busy")
    assert bar._progress.maximum() == 0             # indeterminate
    bar.set_running(False)
    bar.reset_progress()
    assert bar._progress.value() == 0


def test_run_bar_wires_to_the_docking_panel(qapp, monkeypatch) -> None:
    """The pinned RUN button must go through the panel's validation.

    The controller answers with a modal *"implemented in Step 5"* dialog for
    now, so it is stubbed out: tests must never open a modal window.
    """
    monkeypatch.setattr("app.gui.main_window.show_info", lambda *a, **k: None)
    window = MainWindow(AppSettings())
    received: list[tuple[BoxConfig, DockingParameters]] = []
    window.docking_panel.run_requested.connect(
        lambda box, params: received.append((box, params))
    )
    window.run_bar._run_btn.click()
    assert len(received) == 1


def test_main_window_busy_state(qapp) -> None:
    window = MainWindow(AppSettings())
    window.set_busy(True)
    assert window.run_bar._cancel_btn.isHidden() is False
    assert window.run_bar._run_btn.isHidden() is True
    window.set_progress(35, "Preparing ligand ...")
    assert window.run_bar._progress.value() == 35
    window.set_busy(False)
    assert window.run_bar._run_btn.isHidden() is False


# ----------------------------------------------------------------- result panel
def test_result_panel_populates_table(qapp) -> None:
    from app.docking.docking_config import DockingResult, PoseResult

    panel = ResultPanel()
    result = DockingResult(
        engine="AutoDock Vina",
        poses=[
            PoseResult(1, -8.7, 0.0, 0.0),
            PoseResult(2, -8.1, 1.2, 2.4),
        ],
    )
    panel.set_result(result)
    assert panel._table.rowCount() == 2
    assert panel._affinity_value.text().startswith("-8.70")
    assert panel.selected_pose() is not None
    assert panel.selected_pose().pose == 1


def test_result_panel_selection_signal(qapp) -> None:
    from app.docking.docking_config import DockingResult, PoseResult

    panel = ResultPanel()
    panel.set_result(DockingResult(poses=[PoseResult(1, -9.0), PoseResult(2, -8.0)]))
    seen: list[int] = []
    panel.pose_selected.connect(seen.append)
    panel.select_pose(2)
    assert seen == [2]


def test_result_panel_clear(qapp) -> None:
    from app.docking.docking_config import DockingResult, PoseResult

    panel = ResultPanel()
    panel.set_result(DockingResult(poses=[PoseResult(1, -9.0)]))
    panel.clear()
    assert panel._table.rowCount() == 0
    assert panel.result is None


# ---------------------------------------------------------------------- viewer
def test_viewer_constructs_without_webengine(qapp) -> None:
    viewer = MoleculeViewer()
    assert viewer.is_web_available() is False      # disabled in the test env
    assert viewer.is_checked("protein") is True
    assert viewer.is_checked("pocket") is False


def test_viewer_toggle_signal(qapp) -> None:
    viewer = MoleculeViewer()
    seen: list[tuple[str, bool]] = []
    viewer.display_toggled.connect(lambda name, state: seen.append((name, state)))
    viewer._checks["hbonds"].setChecked(True)
    assert seen == [("hbonds", True)]


# ------------------------------------------------------- Step 3: preparation
def test_ligand_panel_keeps_the_parsed_molecule(qapp) -> None:
    panel = LigandPanel(AppSettings())
    assert panel.molecule is None
    assert panel.set_smiles("CCO") is True
    assert panel.molecule is not None
    assert panel.molecule.GetNumAtoms() == 3


def test_ligand_panel_shows_a_prepared_ligand(qapp) -> None:
    from app.chemistry.preparation import PreparedLigand

    panel = LigandPanel(AppSettings())
    prepared = PreparedLigand(name="ethanol", smiles="CCO",
                              rotatable_bonds=1, converter="meeko",
                              warnings=["a friendly warning"])
    panel.show_prepared(prepared)
    assert panel.prepared is prepared
    assert panel._warning_label.isHidden() is False
    assert "friendly warning" in panel._warning_label.text()


def test_main_window_creates_one_workspace_per_session(qapp, tmp_path) -> None:
    window = MainWindow(AppSettings())
    first = window._ensure_workspace("aspirin")
    second = window._ensure_workspace("aspirin")
    assert first == second
    assert (first / "prepared").is_dir()
    assert (first / "input").is_dir()


def test_main_window_archives_the_ligand_input(qapp, tmp_path) -> None:
    window = MainWindow(AppSettings())
    workspace = window._ensure_workspace("ligand")
    window._archive_ligand_input("smiles", "CCO", workspace / "input")
    assert (workspace / "input" / "ligand.smi").read_text(encoding="utf-8") == "CCO"


def test_worker_describes_enzydock_errors() -> None:
    from app.chemistry.ligand import LigandError

    from app.gui.workers import describe_exception

    message, details = describe_exception(
        LigandError("This SMILES could not be read.", "MolFromSmiles -> None"),
        "Ligand preparation",
    )
    assert message == "This SMILES could not be read."
    assert details == "MolFromSmiles -> None"


def test_worker_wraps_unexpected_errors() -> None:
    from app.gui.workers import describe_exception

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        message, details = describe_exception(exc, "Ligand preparation")

    assert "Ligand preparation failed" in message
    assert "RuntimeError: boom" in details


def test_worker_runs_the_callable_inline() -> None:
    from app.gui.workers import FunctionWorker

    worker = FunctionWorker(lambda value: value * 2, 21)
    assert worker.run_synchronously() == 42


# ------------------------------------------------------- Step 4: receptor
def test_protein_panel_shows_a_prepared_receptor(qapp) -> None:
    from app.chemistry.receptor import PreparedReceptor

    panel = ProteinPanel(AppSettings())
    assert panel.prepared is None
    prepared = PreparedReceptor(name="1UBQ", residue_count=76, atom_count=744,
                                hydrogen_count=142, protonation="meeko (polar)",
                                converter="meeko", chains=["A"],
                                pdbqt_path=Path("prepared/receptor.pdbqt"),
                                warnings=["Removed 58 water molecules."])
    panel.show_prepared(prepared)
    assert panel.prepared is prepared
    assert panel._warning_label.isHidden() is False
    assert "58 water" in panel._warning_label.text()
    assert "receptor.pdbqt" in panel._warning_label.text()


def test_protein_panel_hides_the_note_when_nothing_happened(qapp) -> None:
    from app.chemistry.receptor import PreparedReceptor

    panel = ProteinPanel(AppSettings())
    panel.show_prepared(PreparedReceptor(name="clean"))
    assert panel._warning_label.isHidden() is True


def test_main_window_archives_the_protein_input(qapp) -> None:
    window = MainWindow(AppSettings())
    workspace = window._ensure_workspace("protein")
    source = workspace / "input" / "structure.pdb"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("ATOM\n", encoding="utf-8")
    window._archive_protein_input(source, workspace / "input")
    copies = sorted(p.name for p in (workspace / "input").iterdir())
    assert "structure.pdb" in copies


def test_main_window_starts_protein_preparation(qapp, mini_pdb) -> None:
    """Prepare Protein must go through the controller, not the panel."""
    window = MainWindow(AppSettings())
    window.protein_panel.load_file(mini_pdb)
    received: list[object] = []
    window._on_prepare_protein = lambda path, options: received.append((path, options))
    window.protein_panel.prepare_requested.disconnect()
    window.protein_panel.prepare_requested.connect(window._on_prepare_protein)
    window.protein_panel.request_prepare()
    assert len(received) == 1
    path, options = received[0]
    assert Path(path) == mini_pdb
    assert options.remove_waters is True


def test_prepared_receptor_recentres_the_docking_box(qapp) -> None:
    """After preparation the box centre follows the cleaned receptor."""
    from app.chemistry.receptor import PreparedReceptor

    window = MainWindow(AppSettings())
    prepared = PreparedReceptor(name="x", pdbqt_path=None)
    # no PDBQT on disk -> centre is None and the box must be left alone
    before = tuple(spin.value() for spin in window.docking_panel._center)
    window._on_protein_prepared(prepared)
    after = tuple(spin.value() for spin in window.docking_panel._center)
    assert before == after


# ------------------------------------------------------- Step 5: docking run
def _make_docking_result(workspace: Path | None = None) -> object:
    """A small result with two poses for GUI assertions."""
    from app.docking.docking_config import DockingResult, PoseResult

    return DockingResult(
        engine="AutoDock Vina v1.2.3",
        poses=[PoseResult(1, -8.5, 0.0, 0.0), PoseResult(2, -7.1, 1.9, 4.5)],
        workspace=workspace,
    )


def test_main_window_builds_a_docking_request(qapp, mini_pdb, monkeypatch) -> None:
    """START DOCKING must assemble a complete request for the backend."""
    import app.gui.main_window as mw

    window = MainWindow(AppSettings())
    window.protein_panel.load_file(mini_pdb)
    window.ligand_panel.set_smiles("CCO")

    captured: dict[str, object] = {}

    def fake_run(request, progress=None):
        captured["request"] = request
        return _make_docking_result()

    def fake_start(worker, title):
        captured["worker"] = worker
        worker.succeeded.emit(worker.run_synchronously())
        return True

    monkeypatch.setattr(mw, "run_docking", fake_run)
    window._start_worker = fake_start

    window._on_run_docking(BoxConfig(center=(1.0, 2.0, 3.0), size=(22.0, 22.0, 22.0)),
                           DockingParameters(num_poses=3))

    request = captured.get("request")
    assert request is not None
    assert Path(request.receptor_source) == mini_pdb
    assert request.ligand_kind == "smiles"
    assert request.ligand_source == "CCO"
    assert request.box.center == (1.0, 2.0, 3.0)
    assert request.parameters.num_poses == 3
    assert request.engine is not None          # so Cancel can reach the process


def test_main_window_reports_a_missing_protein(qapp, monkeypatch) -> None:
    """Without a receptor the run is refused with an explanation, not a crash."""
    import app.gui.main_window as mw

    window = MainWindow(AppSettings())
    shown: list[str] = []
    monkeypatch.setattr(mw, "show_info",
                        lambda *args, **kwargs: shown.append(str(args)))
    window._on_run_docking(BoxConfig(center=(0, 0, 0)), DockingParameters())
    assert shown
    assert "protein" in shown[0].lower()


def test_main_window_reports_a_missing_ligand(qapp, mini_pdb, monkeypatch) -> None:
    import app.gui.main_window as mw

    window = MainWindow(AppSettings())
    window.protein_panel.load_file(mini_pdb)
    shown: list[str] = []
    monkeypatch.setattr(mw, "show_info",
                        lambda *args, **kwargs: shown.append(str(args)))
    window._on_run_docking(BoxConfig(center=(0, 0, 0)), DockingParameters())
    assert shown
    assert "smiles" in shown[0].lower()


def test_docking_result_fills_the_pose_table(qapp) -> None:
    window = MainWindow(AppSettings())
    window._on_docking_finished(_make_docking_result())   # type: ignore[arg-type]
    assert window.result_panel.result is not None
    assert window.result_panel._table.rowCount() == 2
    assert "-8.50" in window.result_panel._affinity_value.text()


def test_selected_pose_is_passed_to_the_viewer(qapp, tmp_path) -> None:
    """Clicking a row must load that pose, not always the best one."""
    window = MainWindow(AppSettings())
    seen: list[object] = []
    window.viewer.set_protein = lambda path, style="cartoon": seen.append(("protein", path))
    window.viewer.set_ligand = lambda path, style="stick": seen.append(("ligand", path))

    workspace = tmp_path / "run"
    (workspace / "prepared").mkdir(parents=True)
    (workspace / "prepared" / "receptor_clean.pdb").write_text("ATOM\n", encoding="utf-8")
    (workspace / "results").mkdir()
    pose = workspace / "results" / "pose_2.pdb"
    pose.write_text("ATOM\n", encoding="utf-8")

    result = _make_docking_result(workspace)
    result.poses[1].pose_file = pose                   # type: ignore[union-attr]
    window._on_docking_finished(result)
    window.result_panel.pose_selected.emit(2)

    assert ("protein", workspace / "prepared" / "receptor_clean.pdb") in seen
    assert ("ligand", pose) in seen


def test_cancel_reaches_the_running_engine(qapp) -> None:
    """Cancel must be forwarded to the engine of the current job."""
    from app.docking.vina_runner import VinaEngine

    window = MainWindow(AppSettings())
    engine = VinaEngine(executable=Path("does-not-matter"))
    cancelled = []
    engine.cancel = lambda: cancelled.append(True)
    window._engine = engine
    window._on_cancel_docking()
    assert cancelled == [True]


def test_cancel_without_a_run_is_harmless(qapp) -> None:
    window = MainWindow(AppSettings())
    window._engine = None
    window._on_cancel_docking()                        # must not raise


# ------------------------------------------------------- Step 5: docking run
def _make_docking_result(workspace: Path | None = None) -> object:
    """A small result with two poses for GUI assertions."""
    from app.docking.docking_config import DockingResult, PoseResult

    return DockingResult(
        engine="AutoDock Vina v1.2.3",
        poses=[PoseResult(1, -8.5, 0.0, 0.0), PoseResult(2, -7.1, 1.9, 4.5)],
        workspace=workspace,
    )


def test_main_window_builds_a_docking_request(qapp, mini_pdb, monkeypatch) -> None:
    """START DOCKING must assemble a complete request for the backend."""
    import app.gui.main_window as mw

    window = MainWindow(AppSettings())
    window.protein_panel.load_file(mini_pdb)
    window.ligand_panel.set_smiles("CCO")

    captured: dict[str, object] = {}

    def fake_run(request, progress=None):
        captured["request"] = request
        return _make_docking_result()

    def fake_start(worker, title):
        captured["worker"] = worker
        worker.succeeded.emit(worker.run_synchronously())
        return True

    monkeypatch.setattr(mw, "run_docking", fake_run)
    window._start_worker = fake_start

    window._on_run_docking(BoxConfig(center=(1.0, 2.0, 3.0), size=(22.0, 22.0, 22.0)),
                           DockingParameters(num_poses=3))

    request = captured.get("request")
    assert request is not None
    assert Path(request.receptor_source) == mini_pdb
    assert request.ligand_kind == "smiles"
    assert request.ligand_source == "CCO"
    assert request.box.center == (1.0, 2.0, 3.0)
    assert request.parameters.num_poses == 3
    assert request.engine is not None          # so Cancel can reach the process


def test_main_window_reports_a_missing_protein(qapp, monkeypatch) -> None:
    """Without a receptor the run is refused with an explanation, not a crash."""
    import app.gui.main_window as mw

    window = MainWindow(AppSettings())
    shown: list[str] = []
    monkeypatch.setattr(mw, "show_info",
                        lambda *args, **kwargs: shown.append(str(args)))
    window._on_run_docking(BoxConfig(center=(0, 0, 0)), DockingParameters())
    assert shown
    assert "protein" in shown[0].lower()


def test_main_window_reports_a_missing_ligand(qapp, mini_pdb, monkeypatch) -> None:
    import app.gui.main_window as mw

    window = MainWindow(AppSettings())
    window.protein_panel.load_file(mini_pdb)
    shown: list[str] = []
    monkeypatch.setattr(mw, "show_info",
                        lambda *args, **kwargs: shown.append(str(args)))
    window._on_run_docking(BoxConfig(center=(0, 0, 0)), DockingParameters())
    assert shown
    assert "smiles" in shown[0].lower()


def test_docking_result_fills_the_pose_table(qapp) -> None:
    window = MainWindow(AppSettings())
    window._on_docking_finished(_make_docking_result())   # type: ignore[arg-type]
    assert window.result_panel.result is not None
    assert window.result_panel._table.rowCount() == 2
    assert "-8.50" in window.result_panel._affinity_value.text()


def test_selected_pose_is_passed_to_the_viewer(qapp, tmp_path) -> None:
    """Clicking a row must load that pose, not always the best one."""
    window = MainWindow(AppSettings())
    seen: list[object] = []
    window.viewer.set_protein = lambda path, style="cartoon": seen.append(("protein", path))
    window.viewer.set_ligand = lambda path, style="stick": seen.append(("ligand", path))

    workspace = tmp_path / "run"
    (workspace / "prepared").mkdir(parents=True)
    (workspace / "prepared" / "receptor_clean.pdb").write_text("ATOM\n", encoding="utf-8")
    (workspace / "results").mkdir()
    pose = workspace / "results" / "pose_2.pdb"
    pose.write_text("ATOM\n", encoding="utf-8")

    result = _make_docking_result(workspace)
    result.poses[1].pose_file = pose                   # type: ignore[union-attr]
    window._on_docking_finished(result)
    window.result_panel.pose_selected.emit(2)

    assert ("protein", workspace / "prepared" / "receptor_clean.pdb") in seen
    assert ("ligand", pose) in seen


def test_cancel_reaches_the_running_engine(qapp) -> None:
    """Cancel must be forwarded to the engine of the current job."""
    from app.docking.vina_runner import VinaEngine

    window = MainWindow(AppSettings())
    engine = VinaEngine(executable=Path("does-not-matter"))
    cancelled = []
    engine.cancel = lambda: cancelled.append(True)
    window._engine = engine
    window._on_cancel_docking()
    assert cancelled == [True]


def test_cancel_without_a_run_is_harmless(qapp) -> None:
    window = MainWindow(AppSettings())
    window._engine = None
    window._on_cancel_docking()                        # must not raise


def test_status_bar_reports_the_engine(qapp) -> None:
    """The status bar must say whether Vina is actually usable."""
    window = MainWindow(AppSettings())
    window._refresh_engine_status()
    text = window._engine_status.text()
    assert text.startswith("Engine: ")
    assert "checking" not in text


def test_status_bar_reports_the_engine(qapp) -> None:
    """The status bar must say whether Vina is actually usable."""
    window = MainWindow(AppSettings())
    window._refresh_engine_status()
    text = window._engine_status.text()
    assert text.startswith("Engine: ")
    assert "checking" not in text


# ------------------------------------------------------- Step 7: 3D viewer
def _write_pdb(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")
    return path


def _receptor_lines() -> list[str]:
    """Three residues: one touching the ligand, one polar, one far away."""
    return [
        "ATOM      1  CA  ALA A   1     -20.000   0.000   0.000  1.00 0.00           C",
        "ATOM      2  OG  SER A   2       2.000   0.000   0.000  1.00 0.00           O",
        "ATOM      3  CA  GLY A  50      40.000   0.000   0.000  1.00 0.00           C",
    ]


def _ligand_lines() -> list[str]:
    return ["HETATM    1  N1  LIG B   1       3.000   0.000   0.000  1.00 0.00           N"]


def test_viewer_builds_a_scene_from_a_structure(qapp, tmp_path) -> None:
    from app.gui.viewer import MoleculeViewer

    viewer = MoleculeViewer()
    receptor = _write_pdb(tmp_path / "receptor_clean.pdb", _receptor_lines())
    viewer.set_protein(receptor)
    assert viewer.scene.has_protein is True
    assert viewer.scene.has_ligand is False


def test_viewer_derives_the_pocket_from_the_pose(qapp, tmp_path) -> None:
    from app.gui.viewer import MoleculeViewer

    viewer = MoleculeViewer()
    viewer.set_protein(_write_pdb(tmp_path / "receptor_clean.pdb", _receptor_lines()))
    viewer.set_ligand(_write_pdb(tmp_path / "pose_1.pdb", _ligand_lines()))
    assert [r.resseq for r in viewer.scene.pocket_residues] == [2]
    assert viewer.scene.pocket_text.startswith("REMARK")
    assert len(viewer.scene.hbonds) == 1


def test_viewer_sends_json_to_the_page(qapp, tmp_path) -> None:
    from app.gui.viewer import MoleculeViewer

    viewer = MoleculeViewer()
    viewer._page_ready = True
    sent: list[str] = []
    viewer._run_javascript = sent.append                 # type: ignore[method-assign]
    viewer.set_protein(_write_pdb(tmp_path / "receptor_clean.pdb", _receptor_lines()))
    assert len(sent) == 1
    assert sent[0].startswith("if (window.enzydockViewer) window.enzydockViewer.setScene({")
    assert sent[0].endswith("});")


def test_viewer_queues_the_scene_until_the_page_is_ready(qapp, tmp_path) -> None:
    """A pose clicked before Chromium finished loading must still appear."""
    from app.gui.viewer import MoleculeViewer

    viewer = MoleculeViewer()
    viewer._page_ready = False
    sent: list[str] = []
    viewer._run_javascript = sent.append                 # type: ignore[method-assign]
    viewer.set_protein(_write_pdb(tmp_path / "receptor_clean.pdb", _receptor_lines()))
    assert sent == []
    assert viewer._pending_payload is not None

    viewer._on_page_loaded(True)
    assert viewer._pending_payload is None
    assert len(sent) == 1


def test_viewer_toggle_rebuilds_the_display(qapp, tmp_path) -> None:
    from app.gui.viewer import MoleculeViewer

    viewer = MoleculeViewer()
    viewer.set_protein(_write_pdb(tmp_path / "receptor_clean.pdb", _receptor_lines()))
    toggled: list[tuple[str, bool]] = []
    viewer.display_toggled.connect(lambda name, state: toggled.append((name, state)))
    viewer._checks["hbonds"].setChecked(True)
    assert toggled == [("hbonds", True)]
    assert viewer.display_flags()["hbonds"] is True


def test_viewer_clear_scene(qapp, tmp_path) -> None:
    from app.gui.viewer import MoleculeViewer

    viewer = MoleculeViewer()
    viewer.set_protein(_write_pdb(tmp_path / "receptor_clean.pdb", _receptor_lines()))
    viewer.clear_scene()
    assert viewer.scene.is_empty
    assert viewer.pose_number is None


def test_viewer_sets_the_box_for_a_receptor_only_scene(qapp, tmp_path) -> None:
    from app.docking.docking_config import BoxConfig
    from app.gui.viewer import MoleculeViewer

    viewer = MoleculeViewer()
    viewer.set_protein(_write_pdb(tmp_path / "receptor_clean.pdb", _receptor_lines()))
    box = BoxConfig(center=(0.0, 0.0, 0.0), size=(6.0, 6.0, 6.0))
    viewer.set_box(box)
    assert viewer.scene.pocket_source == "inside the docking box"
    assert [r.resseq for r in viewer.scene.pocket_residues] == [2]


def test_viewer_reset_view_is_harmless_without_webengine(qapp) -> None:
    from app.gui.viewer import MoleculeViewer

    viewer = MoleculeViewer()
    assert viewer.is_web_available() is False       # ENZYDOCK_NO_WEBENGINE=1
    viewer.reset_view()                             # must not raise
    viewer.clear_scene()


# ------------------------------------------------- interactions in the GUI
def _interaction_items():
    from app.visualization.interactions import KIND_HBOND, KIND_HYDROPHOBIC
    from app.visualization.interactions import Interaction

    return [
        Interaction("SER2:OG", "LIG1:N1", 3.0, (0.0, 0.0, 0.0), (3.0, 0.0, 0.0),
                    KIND_HBOND, 1),
        Interaction("LEU8:CD1", "LIG1:C1", 3.9, (0.0, 6.0, 0.0), (0.0, 9.0, 0.0),
                    KIND_HYDROPHOBIC, 4),
    ]


def test_result_panel_lists_the_interactions(qapp) -> None:
    from app.docking.docking_config import DockingResult, PoseResult

    panel = ResultPanel()
    panel.set_result(DockingResult(poses=[PoseResult(1, -8.0)]))
    panel.set_interactions(_interaction_items(), 1)

    assert panel._interaction_table.rowCount() == 2
    assert panel._tabs.tabText(1) == "Interactions (2)"
    hint = panel._interaction_hint.text()
    assert "pose 1" in hint
    assert "1x H-bond" in hint
    assert "1x Hydrophobic" in hint
    assert panel.interactions[0].kind_label == "H-bond"


def test_the_interaction_table_shows_the_distance_and_the_partners(qapp) -> None:
    panel = ResultPanel()
    panel.set_interactions(_interaction_items(), 1)
    table = panel._interaction_table
    assert table.item(0, 0).text() == "H-bond"
    assert table.item(0, 1).text() == "SER2:OG"
    assert table.item(0, 2).text() == "LIG1:N1"
    assert table.item(0, 3).text() == "3.00"


def test_result_panel_says_when_there_is_nothing_to_show(qapp) -> None:
    from app.docking.docking_config import DockingResult, PoseResult

    panel = ResultPanel()
    panel.set_result(DockingResult(poses=[PoseResult(1, -8.0)]))
    panel.set_interactions([], 3)
    assert panel._interaction_table.rowCount() == 0
    assert panel._tabs.tabText(1) == "Interactions"
    assert "4.5 A" in panel._interaction_hint.text()
    assert "pose 3" in panel._interaction_hint.text()


def test_clearing_the_result_clears_the_interactions(qapp) -> None:
    from app.docking.docking_config import DockingResult, PoseResult

    panel = ResultPanel()
    panel.set_result(DockingResult(poses=[PoseResult(1, -8.0)]))
    panel.set_interactions(_interaction_items(), 1)
    panel.clear()
    assert panel.interactions == []
    assert panel._interaction_table.rowCount() == 0


def test_the_panel_can_be_switched_to_the_interaction_tab(qapp) -> None:
    panel = ResultPanel()
    assert panel.current_tab() == 0
    panel.show_interactions_tab(True)
    assert panel.current_tab() == 1
    panel.show_interactions_tab(False)
    assert panel.current_tab() == 0


def test_the_viewer_offers_a_contacts_and_a_surface_toggle(qapp) -> None:
    from app.gui.viewer import DISPLAY_TOGGLES, TOGGLE_LAYOUT, MoleculeViewer

    assert "contacts" in DISPLAY_TOGGLES
    assert "surface" in DISPLAY_TOGGLES
    assert [name for name, _text, _checked in TOGGLE_LAYOUT] == list(DISPLAY_TOGGLES)

    viewer = MoleculeViewer()
    assert viewer.is_checked("contacts") is False
    assert viewer.is_checked("surface") is False
    assert set(viewer.display_flags()) == set(DISPLAY_TOGGLES)


def test_the_payload_of_the_viewer_draws_the_contacts(qapp, tmp_path) -> None:
    from app.gui.viewer import MoleculeViewer

    viewer = MoleculeViewer()
    viewer.set_protein(_write_pdb(tmp_path / "receptor_clean.pdb", _receptor_lines()))
    viewer.set_ligand(_write_pdb(tmp_path / "pose_1.pdb", _ligand_lines()))

    hidden = viewer.scene.payload(viewer.display_flags())["contacts"]
    assert hidden["visible"] is False               # off by default

    viewer._checks["contacts"].setChecked(True)
    shown = viewer.scene.payload(viewer.display_flags())["contacts"]
    assert shown["visible"] is True
    assert len(shown["items"]) == len(viewer.scene.interactions) == 1


def test_selecting_a_pose_fills_the_interaction_list(qapp, tmp_path) -> None:
    """The 3D view and the interaction table must always describe one pose."""
    window = MainWindow(AppSettings())
    workspace = tmp_path / "run"
    (workspace / "prepared").mkdir(parents=True)
    (workspace / "prepared" / "receptor_clean.pdb").write_text(
        "\n".join(_receptor_lines()) + "\nEND\n", encoding="utf-8")
    (workspace / "results").mkdir()
    pose = _write_pdb(workspace / "results" / "pose_1.pdb", _ligand_lines())

    result = _make_docking_result(workspace)
    # _on_docking_finished selects the best pose (row 0), so that is the one
    # that has to carry a structure file.
    result.poses[0].pose_file = pose                   # type: ignore[union-attr]
    window._on_docking_finished(result)

    assert window.result_panel.interactions
    assert window.result_panel.interactions[0].kind == "hbond"
    assert window.result_panel._interaction_table.rowCount() == len(
        window.result_panel.interactions
    )


def test_switching_contacts_on_lists_them_and_tells_the_user(qapp, tmp_path) -> None:
    from app.visualization.interactions import KIND_HBOND

    window = MainWindow(AppSettings())
    workspace = tmp_path / "run"
    (workspace / "prepared").mkdir(parents=True)
    (workspace / "prepared" / "receptor_clean.pdb").write_text(
        "\n".join(_receptor_lines()) + "\nEND\n", encoding="utf-8")
    (workspace / "results").mkdir()
    result = _make_docking_result(workspace)
    result.poses[0].pose_file = _write_pdb(
        workspace / "results" / "pose_1.pdb", _ligand_lines())
    window._on_docking_finished(result)

    window._on_display_toggled("contacts", True)
    assert window.result_panel.current_tab() == 1
    assert window.result_panel.interactions
    assert any(
        item.kind == KIND_HBOND for item in window.result_panel.interactions
    )


def test_a_missing_receptor_is_reported_loudly(qapp, tmp_path) -> None:
    """Showing only a floating molecule is the bug this guards against."""
    window = MainWindow(AppSettings())
    workspace = tmp_path / "run"
    (workspace / "prepared").mkdir(parents=True)
    (workspace / "results").mkdir()
    pose = _write_pdb(workspace / "results" / "pose_1.pdb", _ligand_lines())

    result = _make_docking_result(workspace)
    result.poses[0].pose_file = pose                   # type: ignore[union-attr]
    window._on_docking_finished(result)

    assert "missing" in window.statusBar().currentMessage().lower()
    assert window.viewer.scene.has_ligand is True


def test_the_viewer_falls_back_to_the_original_input_receptor(
    qapp, tmp_path
) -> None:
    """Preparation output may have been deleted; the input is still archived."""
    window = MainWindow(AppSettings())
    workspace = tmp_path / "run"
    (workspace / "input").mkdir(parents=True)
    (workspace / "results").mkdir()
    (workspace / "input" / "1UBQ.pdb").write_text(
        "\n".join(_receptor_lines()) + "\nEND\n", encoding="utf-8")
    pose = _write_pdb(workspace / "results" / "pose_1.pdb", _ligand_lines())

    from app.visualization.viewer import build_scene

    result = _make_docking_result(workspace)
    result.receptor = workspace / "prepared" / "receptor.pdbqt"
    result.poses[0].pose_file = pose                   # type: ignore[union-attr]
    window._on_docking_finished(result)

    assert window.viewer.scene.has_protein is True
    assert window.viewer.scene.protein_name == "1UBQ.pdb"
    _ = build_scene                                      # keep the import used


def test_the_notes_of_a_scene_reach_the_status_bar(qapp, tmp_path) -> None:
    """Whatever could not be analysed must be visible, not only logged."""
    window = MainWindow(AppSettings())
    workspace = tmp_path / "run"
    (workspace / "prepared").mkdir(parents=True)
    (workspace / "prepared" / "receptor.pdbqt").write_text(
        "REMARK  AutoDock receptor\n"
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00     0.000 C \n",
        encoding="utf-8",
    )
    (workspace / "results").mkdir()
    pose = _write_pdb(workspace / "results" / "pose_1.pdb", _ligand_lines())
    result = _make_docking_result(workspace)
    result.poses[0].pose_file = pose                   # type: ignore[union-attr]
    window._on_docking_finished(result)

    # PDBQT is not analysable, but it is still drawn.
    assert window.viewer.scene.has_protein is True
    assert window.viewer.scene.notes
