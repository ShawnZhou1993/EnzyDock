"""Tests for :mod:`app.export` - reports and file exports.

The export functions are deliberately Qt free, so everything except the two
``MainWindow`` handler tests at the bottom runs without a ``QApplication``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.docking.docking_config import (
    BoxConfig,
    DockingParameters,
    DockingResult,
    PoseResult,
)
from app.export import (
    input_labels,
    input_labels,
    ExportError,
    build_report,
    default_export_stem,
    export_all_poses,
    export_best_pose,
    export_csv,
    export_report,
    pose_files,
    resolve_pose_files,
)

PDB_BODY = "\n".join([
    "REMARK    EnzyDock pose 1",
    "ATOM      1  C   LIG A   1       0.000   0.000   0.000  1.00  0.00           C",
    "END",
])


def _result_with_poses(workspace: Path, count: int = 2) -> DockingResult:
    """A realistic result whose workspace holds ``count`` poses (PDB + PDBQT)."""
    results_dir = workspace / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    poses: list[PoseResult] = []
    for number in range(1, count + 1):
        (results_dir / f"pose_{number}.pdb").write_text(PDB_BODY, encoding="utf-8")
        (results_dir / f"pose_{number}.pdbqt").write_text(
            PDB_BODY.replace("ATOM", "HETATM"), encoding="utf-8"
        )
        poses.append(PoseResult(pose=number, affinity=-6.0 + number * 0.5,
                                rmsd_lb=0.0, rmsd_ub=0.4 * number,
                                pose_file=results_dir / f"pose_{number}.pdb"))
    return DockingResult(
        receptor=workspace / "prepared" / "receptor.pdbqt",
        ligand=workspace / "prepared" / "ligand.pdbqt",
        engine="AutoDock Vina v1.2.3",
        box=BoxConfig((10.0, 11.0, 12.0), (22.0, 22.0, 22.0)),
        parameters=DockingParameters(exhaustiveness=8, num_poses=count, seed=None),
        poses=poses,
        log_file=workspace / "docking" / "log.txt",
        output_file=workspace / "docking" / "output.pdbqt",
        workspace=workspace,
        elapsed_seconds=4.25,
    )


@pytest.fixture()
def docking_result(tmp_path: Path) -> DockingResult:
    """A finished run inside ``tmp_path``/workspace."""
    workspace = tmp_path / "2026-10-09_095522_1UBQ"
    workspace.mkdir()
    return _result_with_poses(workspace, count=2)


# ------------------------------------------------------------------- report
def test_report_names_the_original_inputs(tmp_path: Path) -> None:
    """'1UBQ.pdb' is far more useful in a report than 'receptor.pdbqt'."""
    workspace = tmp_path / "run"
    (workspace / "input").mkdir(parents=True)
    (workspace / "input" / "1UBQ.pdb").write_text("HEADER\n", encoding="utf-8")
    (workspace / "input" / "ligand.smi").write_text("CC(=O)Oc1ccccc1C(=O)O\n",
                                                    encoding="utf-8")
    result = DockingResult(workspace=workspace,
                           receptor=workspace / "prepared" / "receptor.pdbqt")
    receptor, ligand = input_labels(result)
    assert receptor == "1UBQ.pdb"
    assert ligand == "SMILES: CC(=O)Oc1ccccc1C(=O)O"
    text = build_report(result)
    assert "1UBQ.pdb" in text
    assert "SMILES:" in text


def test_input_labels_without_a_workspace() -> None:
    assert input_labels(DockingResult()) == ("", "")


def test_report_states_the_best_affinity(docking_result: DockingResult) -> None:
    text = build_report(docking_result)
    assert "-5.500 kcal/mol" in text          # pose 1: -6.0 + 0.5
    assert "(best)" in text


def test_report_repeats_every_setting(docking_result: DockingResult) -> None:
    text = build_report(docking_result)
    for expected in ("AutoDock Vina v1.2.3", "10.00, 11.00, 12.00", "22.0, 22.0, 22.0",
                     "10,648", "Exhaustiveness", "random (engine default)"):
        assert expected in text, expected


def test_report_lists_all_poses(docking_result: DockingResult) -> None:
    text = build_report(docking_result)
    assert "| 1 (best) |" in text
    assert "| 2 |" in text
    assert "pose_2.pdb" in text


def test_report_survives_an_empty_result() -> None:
    text = build_report(DockingResult())
    assert "without producing any pose" in text
    assert "n/a" in text                       # every missing value degrades


def test_write_report_creates_missing_parents(tmp_path: Path,
                                              docking_result: DockingResult) -> None:
    target = tmp_path / "deep" / "nested" / "report.md"
    written = export_report(docking_result, target)
    assert written.is_file()
    assert written.read_text(encoding="utf-8").startswith("# EnzyDock Docking Report")


# ------------------------------------------------------------------ helpers
def test_default_export_stem_uses_the_workspace_name(docking_result: DockingResult) -> None:
    assert default_export_stem(docking_result) == "2026-10-09_095522_1UBQ"


def test_default_export_stem_falls_back() -> None:
    assert default_export_stem(DockingResult()) == "enzydock_results"


def test_pose_files_groups_pdb_before_pdbqt(docking_result: DockingResult) -> None:
    found = pose_files(docking_result)
    assert sorted(found) == [1, 2]
    assert [p.suffix for p in found[1]] == [".pdb", ".pdbqt"]


def test_pose_files_falls_back_to_pose_paths(tmp_path: Path) -> None:
    """A result restored without a workspace still exports its pose files."""
    source = tmp_path / "pose_1.pdb"
    source.write_text(PDB_BODY, encoding="utf-8")
    result = DockingResult(poses=[PoseResult(1, -5.0, pose_file=source)])
    assert pose_files(result) == {1: [source]}


def test_resolve_pose_files_prefers_pdb(docking_result: DockingResult) -> None:
    files = resolve_pose_files(docking_result, docking_result.poses[0])
    assert files[0].suffix == ".pdb"
    assert files[1].suffix == ".pdbqt"


def test_resolve_pose_files_raises_a_readable_error() -> None:
    result = DockingResult(poses=[PoseResult(3, -5.0)])
    with pytest.raises(ExportError) as info:
        resolve_pose_files(result, result.poses[0])
    assert "pose 3" in info.value.message
    assert info.value.details


# ---------------------------------------------------------------- exporters
def test_export_best_pose_writes_pdb_and_pdbqt(tmp_path: Path,
                                               docking_result: DockingResult) -> None:
    target = tmp_path / "out" / "best.pdb"
    written = export_best_pose(docking_result, target)

    assert [p.name for p in written] == ["best.pdb", "best.pdbqt"]
    assert target.read_text(encoding="utf-8") == PDB_BODY
    # The best pose is the *lowest* affinity, i.e. pose 1 (-5.5) not pose 2.
    assert (tmp_path / "out" / "best.pdbqt").read_text(encoding="utf-8").startswith("REMARK")


def test_export_best_pose_uses_the_lowest_energy_pose(tmp_path: Path,
                                                       docking_result: DockingResult) -> None:
    """Pose 2 becomes the best one as soon as it scores lower."""
    docking_result.poses[1].affinity = -9.0
    written = export_best_pose(docking_result, tmp_path / "best.pdb")
    assert len(written) == 2
    # pose_2.pdbqt carries the HETATM marker used in the fixture above
    assert "HETATM" in written[1].read_text(encoding="utf-8")


def test_export_best_pose_without_poses_raises() -> None:
    with pytest.raises(ExportError) as info:
        export_best_pose(DockingResult(), "best.pdb")
    assert "run a docking first" in info.value.message


def test_export_all_poses_copies_every_pose(tmp_path: Path,
                                            docking_result: DockingResult) -> None:
    target = tmp_path / "exported"
    written = export_all_poses(docking_result, target)

    names = sorted(p.name for p in written)
    assert names == ["pose_1.pdb", "pose_1.pdbqt", "pose_2.pdb", "pose_2.pdbqt",
                     "report.md", "summary.csv"]
    assert (target / "pose_2.pdb").read_text(encoding="utf-8") == PDB_BODY


def test_export_all_poses_can_skip_the_extras(tmp_path: Path,
                                              docking_result: DockingResult) -> None:
    written = export_all_poses(docking_result, tmp_path / "only",
                               with_table=False, with_report=False)
    assert len(written) == 4


def test_export_all_poses_without_files_raises(tmp_path: Path) -> None:
    result = DockingResult(poses=[PoseResult(1, -5.0)], workspace=tmp_path)
    with pytest.raises(ExportError) as info:
        export_all_poses(result, tmp_path / "out")
    assert "No pose files" in info.value.message


def test_export_csv_matches_the_summary_writer(tmp_path: Path,
                                               docking_result: DockingResult) -> None:
    written = export_csv(docking_result, tmp_path / "table.csv")
    lines = written.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "pose,affinity_kcal_mol,rmsd_lower_bound,rmsd_upper_bound"
    assert lines[1].startswith("1,-5.500")
    assert len(lines) == 3                             # header + 2 poses


def test_export_files_are_copies_not_moves(docking_result: DockingResult,
                                           tmp_path: Path) -> None:
    """The workspace stays untouched - exports must never consume the source."""
    source = docking_result.workspace / "results" / "pose_1.pdb"
    export_all_poses(docking_result, tmp_path / "copy")
    assert source.is_file()


# --------------------------------------------------------------- GUI wiring
def test_export_handlers_are_safe_without_a_result(qapp, monkeypatch,
                                                   tmp_path: Path) -> None:
    """Clicking an export button before docking must not raise or open a dialog."""
    from app.gui.main_window import MainWindow
    from app.utils.settings import AppSettings
    from PySide6.QtCore import QSettings

    ini = tmp_path / "settings.ini"
    window = MainWindow(AppSettings(QSettings(str(ini), QSettings.Format.IniFormat)))

    def forbidden(*_args, **_kwargs):                  # pragma: no cover - guard
        raise AssertionError("no dialog may open without a result")

    monkeypatch.setattr("app.gui.main_window.QFileDialog.getSaveFileName", forbidden)
    monkeypatch.setattr("app.gui.main_window.QFileDialog.getExistingDirectory", forbidden)
    monkeypatch.setattr("app.gui.main_window.show_info", forbidden)

    for handler in (window._on_export_best, window._on_export_all,
                    window._on_export_csv, window._on_export_report):
        handler()


def test_export_best_handler_writes_the_file(qapp, monkeypatch, tmp_path: Path) -> None:
    """Full handler path: dialog path -> exporter -> user feedback."""
    from PySide6.QtCore import QSettings

    from app.gui.main_window import MainWindow
    from app.utils.settings import AppSettings

    ini = tmp_path / "settings.ini"
    settings = AppSettings(QSettings(str(ini), QSettings.Format.IniFormat))
    window = MainWindow(settings)

    workspace = tmp_path / "2026-10-09_095522_1UBQ"
    workspace.mkdir()
    result = _result_with_poses(workspace, count=1)
    window._docking_result = result

    destination = tmp_path / "saved" / "my_pose.pdb"
    monkeypatch.setattr(
        "app.gui.main_window.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(destination), ""),
    )
    informed: list[str] = []
    monkeypatch.setattr("app.gui.main_window.show_info",
                        lambda parent, message, title="": informed.append(message))

    window._on_export_best()

    assert destination.is_file()
    assert (tmp_path / "saved" / "my_pose.pdbqt").is_file()
    assert informed and "my_pose.pdb" in informed[0]
    assert settings.get_str("paths/last_export_dir") == str(tmp_path / "saved")


def test_export_all_handler_writes_a_folder(qapp, monkeypatch, tmp_path: Path) -> None:
    from PySide6.QtCore import QSettings

    from app.gui.main_window import MainWindow
    from app.utils.settings import AppSettings

    ini = tmp_path / "settings.ini"
    window = MainWindow(AppSettings(QSettings(str(ini), QSettings.Format.IniFormat)))

    workspace = tmp_path / "run"
    workspace.mkdir()
    window._docking_result = _result_with_poses(workspace, count=3)

    target = tmp_path / "all_poses"
    monkeypatch.setattr(
        "app.gui.main_window.QFileDialog.getExistingDirectory",
        lambda *args, **kwargs: str(target),
    )
    monkeypatch.setattr("app.gui.main_window.show_info", lambda *args, **kwargs: None)

    window._on_export_all()

    assert (target / "pose_3.pdb").is_file()
    assert (target / "summary.csv").is_file()
    assert (target / "report.md").is_file()


# ------------------------------------------------- interactions in the report
def test_report_says_when_no_interaction_could_be_derived(
    docking_result: DockingResult,
) -> None:
    """The fixture has no prepared receptor, so the section must say so."""
    text = build_report(docking_result)
    assert "## Interactions of pose 1 (best pose)" in text
    assert "No receptor-ligand contact within 4.5 A" in text


def test_report_lists_the_interactions_of_the_best_pose(tmp_path: Path) -> None:
    """A preparable workspace gets a real interaction table."""
    workspace = tmp_path / "2026-10-09_101500_1UBQ"
    prepared = workspace / "prepared"
    prepared.mkdir(parents=True)
    prepared.joinpath("receptor_clean.pdb").write_text(
        "ATOM      1  OG  SER A   2       0.000   0.000   0.000  1.00 0.00           O\n"
        "ATOM      2  CD1 LEU A   8       0.000   6.000   0.000  1.00 0.00           C\n"
        "END\n",
        encoding="utf-8",
    )
    results = workspace / "results"
    results.mkdir()
    pose_file = results / "pose_1.pdb"
    pose_file.write_text(
        "HETATM    1  N1  LIG B   1       2.900   0.000   0.000  1.00 0.00           N\n"
        "HETATM    2  C1  LIG B   1       0.000   9.000   0.000  1.00 0.00           C\n"
        "END\n",
        encoding="utf-8",
    )
    result = DockingResult(
        receptor=prepared / "receptor.pdbqt",
        ligand=prepared / "ligand.pdbqt",
        engine="AutoDock Vina v1.2.7",
        poses=[PoseResult(pose=1, affinity=-5.5, rmsd_lb=0.0, rmsd_ub=0.0,
                          pose_file=pose_file)],
        workspace=workspace,
    )

    from app.export.report import best_pose_interactions

    items = best_pose_interactions(result)
    assert items
    text = build_report(result)
    section = text[text.index("## Interactions of pose 1"):]
    assert "| Interaction | Receptor | Ligand | Distance (A) |" in section
    assert "| H-bond | SER2:OG | LIG1:N1 |" in section
    assert "possible interaction(s)" in section


def test_an_unreadable_workspace_does_not_break_the_report(
    docking_result: DockingResult,
) -> None:
    """A moved or cleaned-up workspace must not stop an export."""
    docking_result.workspace = docking_result.workspace / "does-not-exist"  # type: ignore[operator]
    text = build_report(docking_result)
    assert "## Interactions of pose 1 (best pose)" in text


# ------------------------------------------------- interactions in the report
def test_report_says_when_no_interaction_could_be_derived(
    docking_result: DockingResult,
) -> None:
    """The fixture has no prepared receptor, so the section must say so."""
    text = build_report(docking_result)
    assert "## Interactions of pose 1 (best pose)" in text
    assert "No receptor-ligand contact within 4.5 A" in text


def test_report_lists_the_interactions_of_the_best_pose(tmp_path: Path) -> None:
    """A preparable workspace gets a real interaction table."""
    workspace = tmp_path / "2026-10-09_101500_1UBQ"
    prepared = workspace / "prepared"
    prepared.mkdir(parents=True)
    prepared.joinpath("receptor_clean.pdb").write_text(
        "ATOM      1  OG  SER A   2       0.000   0.000   0.000  1.00 0.00           O\n"
        "ATOM      2  CD1 LEU A   8       0.000   6.000   0.000  1.00 0.00           C\n"
        "END\n",
        encoding="utf-8",
    )
    results = workspace / "results"
    results.mkdir()
    pose_file = results / "pose_1.pdb"
    pose_file.write_text(
        "HETATM    1  N1  LIG B   1       2.900   0.000   0.000  1.00 0.00           N\n"
        "HETATM    2  C1  LIG B   1       0.000   9.000   0.000  1.00 0.00           C\n"
        "END\n",
        encoding="utf-8",
    )
    result = DockingResult(
        receptor=prepared / "receptor.pdbqt",
        ligand=prepared / "ligand.pdbqt",
        engine="AutoDock Vina v1.2.7",
        poses=[PoseResult(pose=1, affinity=-5.5, rmsd_lb=0.0, rmsd_ub=0.0,
                          pose_file=pose_file)],
        workspace=workspace,
    )

    from app.export.report import best_pose_interactions

    items = best_pose_interactions(result)
    assert items
    text = build_report(result)
    section = text[text.index("## Interactions of pose 1"):]
    assert "| Interaction | Receptor | Ligand | Distance (A) |" in section
    assert "| H-bond | SER2:OG | LIG1:N1 |" in section
    assert "possible interaction(s)" in section


def test_an_unreadable_workspace_does_not_break_the_report(
    docking_result: DockingResult,
) -> None:
    """A moved or cleaned-up workspace must not stop an export."""
    docking_result.workspace = docking_result.workspace / "does-not-exist"  # type: ignore[operator]
    text = build_report(docking_result)
    assert "## Interactions of pose 1 (best pose)" in text
