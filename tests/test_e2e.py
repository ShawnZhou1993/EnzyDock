"""End-to-end tests (Step 9): the real chain, the real engine, the real GUI.

Everything else in ``tests/`` mocks the expensive parts.  These tests do not:
they read ``examples/protein/1UBQ.pdb``, prepare the receptor, prepare a
ligand, run the **actual AutoDock Vina binary**, parse its output, build the
3D scene and export the results - exactly what a user triggers with one click.

They are marked ``e2e`` and skip themselves when no Vina executable can be
found, so a machine without the engine still gets a green suite::

    pytest -q                     # runs them when Vina is installed
    pytest -q -m "not e2e"        # fast suite only

The runs use a fixed ``seed`` so a real regression is distinguishable from
Vina's stochastic search.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.docking.base import DockingEngine
from app.docking.docking_config import BoxConfig, DockingParameters
from app.docking.pipeline import DockingRequest, run_docking
from app.visualization.interactions import KINDS

pytestmark = pytest.mark.e2e

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
PROTEIN_PDB = EXAMPLES / "protein" / "1UBQ.pdb"
PROTEIN_CIF = EXAMPLES / "protein" / "1UBQ.cif"
LIGAND_SMI = EXAMPLES / "ligand" / "aspirin.smi"

#: Ubiquitin's geometric centre - the box a user gets after "Detect Site".
BOX = BoxConfig((30.31, 29.93, 17.25), (22.0, 22.0, 22.0))

#: Small but realistic: fast enough for CI, deterministic through ``seed``.
PARAMETERS = DockingParameters(exhaustiveness=4, num_poses=5, seed=42)

#: Generous: preparation plus five poses take a few seconds on any machine.
GUI_TIMEOUT_MS = 180_000


# ------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def vina():
    """The real Vina engine, or a skip when the binary is not installed."""
    from app.docking import engines                     # noqa: F401 - registers

    engine = DockingEngine.create("AutoDock Vina")
    available, reason = engine.is_available()
    if not available:
        pytest.skip(f"AutoDock Vina is not installed ({reason})")
    return engine


@pytest.fixture()
def e2e_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the workspace root so a run never touches the real one."""
    target = tmp_path / "workspace"
    monkeypatch.setenv("ENZYDOCK_WORKSPACE", str(target))
    return target


def _run(structure: Path, engine, *, ligand: str | None = None) -> object:
    """Run the complete pipeline the way the GUI does."""
    return run_docking(DockingRequest(
        receptor_source=structure,
        ligand_source=ligand or LIGAND_SMI.read_text(encoding="utf-8").strip(),
        ligand_kind="smiles",
        box=BOX,
        parameters=PARAMETERS,
        receptor_name=structure.stem,
        ligand_name="aspirin",
        engine=engine,
    ))


# --------------------------------------------------------------- the pipeline
def test_pipeline_produces_poses_and_a_report(vina, e2e_workspace: Path) -> None:
    """The complete chain: prepare -> dock -> parse -> report."""
    result = _run(PROTEIN_PDB, vina)

    assert result.poses, "no pose was produced"
    assert result.best_affinity is not None and result.best_affinity < 0
    # Vina may return fewer poses than requested (energy_range filter).
    assert 1 <= len(result.poses) <= PARAMETERS.num_poses
    assert "Vina" in result.engine

    # Vina emits poses best-first; the GUI relies on that ordering.
    affinities = [pose.affinity for pose in result.poses]
    assert affinities == sorted(affinities)

    # Every pose must have a structure file the viewer and exporter can read.
    assert all(pose.pose_file and Path(pose.pose_file).is_file()
               for pose in result.poses)

    results_dir = Path(result.workspace) / "results"
    assert (results_dir / "pose_1.pdb").is_file()
    assert (results_dir / "pose_1.pdbqt").is_file()
    assert (results_dir / "summary.csv").is_file()

    pose_text = (results_dir / "pose_1.pdb").read_text(encoding="utf-8")
    assert pose_text.startswith("REMARK    EnzyDock pose 1")
    assert pose_text.rstrip().endswith("END")
    # The element column is what PyMOL / Chimera need; PDBQT has charges there.
    assert any(line[76:78].strip() in {"C", "N", "O"}
               for line in pose_text.splitlines() if line.startswith(("ATOM", "HETATM")))

    report = (results_dir / "report.md").read_text(encoding="utf-8")
    assert "AutoDock Vina" in report
    assert "1UBQ.pdb" in report                    # the *original* input name
    assert "SMILES:" in report
    assert f"{result.best_affinity:.3f} kcal/mol" in report
    assert "## Interactions of pose" in report


def test_a_real_run_can_be_analysed_in_3d(vina, e2e_workspace: Path) -> None:
    """Regression: the result view used to show the docked pose only.

    The receptor has to reach the 3D scene, the pose has to be analysable and
    every interaction has to name real partners.
    """
    from app.visualization.viewer import build_scene

    result = _run(PROTEIN_PDB, vina)
    workspace = Path(result.workspace)
    scene = build_scene(workspace / "prepared" / "receptor_clean.pdb",
                        result.poses[0].pose_file)

    assert scene.has_protein is True
    assert scene.protein_name == "receptor_clean.pdb"
    assert scene.notes == []                       # nothing failed silently
    # The pose came out of a box centred on the protein, so it touches it.
    assert scene.pocket_residues
    assert scene.pocket_source.endswith("around the ligand")

    for item in scene.interactions:
        assert item.protein_atom and item.ligand_atom
        assert 0.0 < item.distance <= 5.5
        assert item.kind in KINDS
    # The payload the page draws must describe the very same contacts.
    payload = scene.payload({"contacts": True, "residues": True})
    assert payload["contacts"]["visible"] is True
    assert len(payload["contacts"]["items"]) == len(scene.interactions)
    assert payload["pocket"]["labels"]


def test_pipeline_input_is_archived_and_untouched(vina, e2e_workspace: Path) -> None:
    """The user's file is copied into the workspace, never modified."""
    before = PROTEIN_PDB.stat().st_mtime
    digest_before = PROTEIN_PDB.read_bytes()

    result = _run(PROTEIN_PDB, vina)

    inputs = sorted(p.name for p in (Path(result.workspace) / "input").iterdir())
    assert "1UBQ.pdb" in inputs
    assert "ligand.smi" in inputs
    assert PROTEIN_PDB.read_bytes() == digest_before
    assert PROTEIN_PDB.stat().st_mtime == before


def test_mmcif_input_reaches_the_same_result(vina, e2e_workspace: Path) -> None:
    """PDB and mmCIF describe the same molecule, so docking must agree."""
    from_pdb = _run(PROTEIN_PDB, vina)
    from_cif = _run(PROTEIN_CIF, vina)

    assert from_cif.poses, "the mmCIF receptor produced no pose"
    assert from_cif.best_affinity is not None and from_pdb.best_affinity is not None
    # Same seed, same receptor: Vina's stochastic search must land in the
    # same place.  A larger difference means the two readers disagree.
    assert abs(from_cif.best_affinity - from_pdb.best_affinity) < 0.05


def test_exports_from_a_real_run(vina, e2e_workspace: Path, tmp_path: Path) -> None:
    """Export on top of a real run - the files a user actually takes away."""
    from app.export import export_all_poses, export_best_pose

    result = _run(PROTEIN_PDB, vina)

    folder = tmp_path / "all"
    written = export_all_poses(result, folder)
    assert (folder / "pose_1.pdb").is_file()
    assert (folder / "summary.csv").is_file()
    assert (folder / "report.md").is_file()
    assert len(written) >= 2 * len(result.poses) + 2

    best = tmp_path / "best" / "best_pose.pdb"
    files = export_best_pose(result, best)
    assert best.is_file()
    assert any(path.suffix == ".pdbqt" for path in files)

    # The exported best pose is the lowest-energy one ...
    assert result.best_pose is not None
    # ... and the CSV agrees with the table the GUI shows.
    csv_rows = (folder / "summary.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(csv_rows) == len(result.poses) + 1
    assert csv_rows[1].startswith(f"{result.best_pose.pose},")


# ----------------------------------------------------------------- the GUI
def test_gui_journey_docks_and_exports(qapp, monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path, vina) -> None:
    """One click through the real window: load -> dock -> show -> export."""
    from PySide6.QtCore import QEventLoop, QSettings, QTimer

    from app.gui.main_window import MainWindow
    from app.utils.settings import AppSettings

    monkeypatch.setenv("ENZYDOCK_WORKSPACE", str(tmp_path / "workspace"))
    ini = tmp_path / "settings.ini"
    window = MainWindow(AppSettings(QSettings(str(ini), QSettings.Format.IniFormat)))

    assert window.protein_panel.load_file(PROTEIN_PDB), "the example protein failed to load"
    assert window.ligand_panel.set_smiles("CC(=O)Oc1ccccc1C(=O)O")

    summary = window.protein_panel.summary
    assert summary is not None and summary.center is not None
    window.docking_panel.set_center(tuple(round(value, 2) for value in summary.center))
    window.docking_panel.set_exhaustiveness(PARAMETERS.exhaustiveness)

    window.docking_panel.request_run()
    worker = window._worker
    assert worker is not None, "START DOCKING did not dispatch a worker"

    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    QTimer.singleShot(GUI_TIMEOUT_MS, loop.quit)
    loop.exec()

    result = window._docking_result
    assert result is not None, "the docking run did not finish in time"
    assert result.poses, "the run produced no poses"

    # The result panel and the viewer were fed from the run.
    assert window.result_panel.result is result
    assert window.result_panel._table.rowCount() == len(result.poses)

    # ... and the 3D view holds the receptor, not just a floating pose.
    assert window.viewer.scene.has_protein is True
    assert window.viewer.scene.protein_name == "receptor_clean.pdb"
    assert window.result_panel._interaction_table.rowCount() == len(
        window.result_panel.interactions
    )

    # Export through the real handlers, with the file dialog answered for us.
    folder = tmp_path / "exported"
    monkeypatch.setattr("app.gui.main_window.QFileDialog.getExistingDirectory",
                        lambda *args, **kwargs: str(folder))
    monkeypatch.setattr("app.gui.main_window.QFileDialog.getSaveFileName",
                        lambda *args, **kwargs: (str(tmp_path / "best.pdb"), ""))
    monkeypatch.setattr("app.gui.main_window.show_info", lambda *args, **kwargs: None)

    window._on_export_all()
    assert (folder / "pose_1.pdb").is_file()
    assert (folder / "summary.csv").is_file()
    assert (folder / "report.md").is_file()

    window._on_export_best()
    assert (tmp_path / "best.pdb").is_file()
    assert (tmp_path / "best.pdbqt").is_file()

    window.close()


def test_cancel_leaves_the_ui_usable(qapp, monkeypatch: pytest.MonkeyPatch,
                                     tmp_path: Path, vina) -> None:
    """Cancelling a real run must never leave the UI stuck or half-disabled.

    The run is deliberately slow (high exhaustiveness) so the cancel request
    lands while Vina is searching.  Whether Vina stops early or was already
    done does not matter - what matters is that the window comes back.
    """
    from PySide6.QtCore import QEventLoop, QSettings, QTimer

    from app.gui.main_window import MainWindow
    from app.utils.settings import AppSettings

    monkeypatch.setenv("ENZYDOCK_WORKSPACE", str(tmp_path / "workspace"))
    ini = tmp_path / "settings.ini"
    window = MainWindow(AppSettings(QSettings(str(ini), QSettings.Format.IniFormat)))

    assert window.protein_panel.load_file(PROTEIN_PDB)
    assert window.ligand_panel.set_smiles("CC(=O)Oc1ccccc1C(=O)O")
    window.docking_panel.set_center((30.31, 29.93, 17.25))
    window.docking_panel.set_exhaustiveness(16)

    # A cancelled run reports through show_error(); a modal dialog must never
    # open inside a test (the offscreen platform cannot run one).
    monkeypatch.setattr("app.gui.main_window.show_error",
                        lambda *args, **kwargs: None)

    window.docking_panel.request_run()
    worker = window._worker
    assert worker is not None
    assert window._worker is worker                      # the job is registered

    # Cancel whenever the timer fires - during preparation or during the
    # search; both paths must end with a usable window.
    QTimer.singleShot(1500, window.docking_panel.request_cancel)
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    QTimer.singleShot(GUI_TIMEOUT_MS, loop.quit)
    loop.exec()

    assert not worker.isRunning()
    assert window._worker is None                        # reset by the worker
    window.close()
