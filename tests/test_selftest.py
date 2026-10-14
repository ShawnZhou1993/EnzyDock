"""Tests for :mod:`app.selftest` (Step 10 diagnostics).

The diagnostics exist to catch broken *installations*, so the tests have to
prove two things:

* every check passes on a healthy machine (they are not decorative), and
* a failure is reported as a ``FAIL`` check instead of an exception - a
  diagnostics tool that crashes is worthless.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.selftest import (
    DEFAULT_SMILES,
    Check,
    SelftestOptions,
    format_report,
    run_checks,
    run_selftest,
    write_report,
)


@pytest.fixture()
def static_checks() -> list[Check]:
    """The fast checks that never touch the docking engine."""
    return run_checks(SelftestOptions(dock=False))


def _by_name(checks: list[Check], name: str) -> Check:
    for check in checks:
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name!r} in {[c.name for c in checks]}")


# --------------------------------------------------------------- basic shape

def test_every_check_has_a_name_and_a_status(static_checks: list[Check]) -> None:
    for check in static_checks:
        assert check.name
        assert check.status in ("PASS", "FAIL")


def test_healthiest_checks_pass_on_a_working_machine(
        static_checks: list[Check]) -> None:
    """These must never fail: they only need EnzyDock's own dependencies."""
    for name in ("Application", "Viewer resources", "Writable folders",
                 "RDKit", "NumPy / SciPy", "PDBQT writer (meeko)",
                 "Ligand preparation"):
        check = _by_name(static_checks, name)
        assert check.ok, f"{name} failed: {check.detail}"


def test_vina_check_reports_a_location_when_found(static_checks: list[Check]) -> None:
    check = _by_name(static_checks, "AutoDock Vina")
    if check.ok:
        assert "vina" in check.detail.lower()
    else:
        # A machine without Vina must still get actionable advice.
        assert check.hint
        assert "vina" in check.hint.lower()


def test_receptor_and_docking_checks_are_skipped_without_a_protein(
        static_checks: list[Check]) -> None:
    names = [check.name for check in static_checks]
    assert "Protein preparation" not in names
    assert "Docking run" not in names


# ------------------------------------------------------------- real protein

def test_protein_preparation_check_uses_the_real_converter(
        project_root: Path) -> None:
    pdb = project_root / "examples" / "protein" / "1UBQ.pdb"
    if not pdb.is_file():                                # pragma: no cover
        pytest.skip("example protein is not available")
    checks = run_checks(SelftestOptions(pdb=pdb))
    check = _by_name(checks, "Protein preparation")
    assert check.ok, check.detail
    assert "76" in check.detail                          # ubiquitin: 76 residues


def test_missing_protein_is_reported_not_raised(tmp_path: Path) -> None:
    checks = run_checks(SelftestOptions(pdb=tmp_path / "nope.pdb"))
    check = _by_name(checks, "Protein preparation")
    assert not check.ok
    assert "no such file" in check.detail
    assert check.hint


# -------------------------------------------------------------- failure mode

def test_a_crashing_check_is_reported_instead_of_raised(monkeypatch) -> None:
    """A diagnostics tool must never raise - it must report."""
    import app.selftest as selftest

    def boom() -> Check:
        raise RuntimeError("simulated broken backend")

    monkeypatch.setattr(selftest, "_check_meeko", boom)
    checks = selftest.run_checks(SelftestOptions())
    check = _by_name(checks, "PDBQT writer (meeko)")
    assert not check.ok
    assert "simulated broken backend" in check.detail
    assert check.hint


def test_check_renders_a_hint_only_when_it_failed() -> None:
    passed = Check("x", True, "fine", "should not appear")
    assert "should not appear" not in passed.line()
    failed = Check("x", False, "broken", "do this")
    assert "do this" in failed.line()


# ---------------------------------------------------------------- reporting

def test_report_summarises_the_outcome(static_checks: list[Check]) -> None:
    report = format_report(static_checks)
    assert "EnzyDock diagnostics" in report
    assert "checks passed" in report
    for check in static_checks:
        assert check.name in report


def test_report_marks_failed_checks() -> None:
    checks = [Check("ok", True, "fine"), Check("bad", False, "broken", "fix it")]
    report = format_report(checks)
    assert "[FAIL] bad" in report
    assert "Failed: bad" in report
    assert "1/2 checks passed." in report


def test_write_report_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "report.txt"
    written = write_report([Check("x", True, "fine")], target)
    assert written == target
    assert "PASS" in target.read_text(encoding="utf-8")


def test_run_selftest_summary_aggregates(tmp_path: Path) -> None:
    summary = run_selftest(SelftestOptions(), report_path=tmp_path / "r.txt")
    assert summary.report_path is not None
    assert summary.report
    # Vina may legitimately be missing on a developer machine.
    assert summary.ok == all(check.ok for check in summary.checks)


def test_progress_callback_is_invoked() -> None:
    seen: list[tuple[int, str]] = []
    run_checks(SelftestOptions(), progress=lambda p, m: seen.append((p, m)))
    assert seen, "the diagnostics never reported progress"
    assert seen[-1][0] == 100
    assert all(0 <= percent <= 100 for percent, _ in seen)


# ------------------------------------------------------------------ defaults

def test_default_smiles_is_parseable() -> None:
    from rdkit import Chem

    assert Chem.MolFromSmiles(DEFAULT_SMILES) is not None


def test_options_normalise_the_protein_path(tmp_path: Path) -> None:
    pdb = tmp_path / "x.pdb"
    pdb.write_text("REMARK\nEND\n", encoding="utf-8")
    options = SelftestOptions(pdb=str(pdb))
    assert isinstance(options.pdb, Path)
    assert options.pdb == pdb


# ------------------------------------------------------------ CLI + GUI wiring

def test_cli_selftest_writes_a_report(tmp_path: Path) -> None:
    """``--selftest`` is the only readable output of a windowed build."""
    from app.main import main

    report = tmp_path / "out" / "diagnostics.txt"
    code = main(["--selftest", "--report", str(report)])
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "EnzyDock diagnostics" in text
    assert "[PASS]" in text
    # Vina may legitimately be absent, so only the exit-code contract is fixed.
    assert code == (0 if "[FAIL]" not in text else 1)


def test_gui_diagnostics_shows_a_report(qapp, monkeypatch, tmp_path: Path) -> None:
    """*Help -> Run Diagnostics* must not open a modal dialog in tests."""
    from PySide6.QtCore import QEventLoop, QSettings, QTimer

    import app.gui.main_window as main_window_module
    from app.gui.main_window import MainWindow
    from app.utils.settings import AppSettings

    captured: list[str] = []
    monkeypatch.setattr(
        main_window_module, "show_report",
        lambda parent, title, text, intro="": captured.append(text),
    )
    monkeypatch.setenv("ENZYDOCK_WORKSPACE", str(tmp_path / "workspace"))

    window = MainWindow(AppSettings(QSettings(str(tmp_path / "settings.ini"),
                                              QSettings.Format.IniFormat)))
    window._run_diagnostics()
    worker = window._worker
    assert worker is not None

    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    QTimer.singleShot(60_000, loop.quit)
    loop.exec()

    assert len(captured) == 1
    assert "EnzyDock diagnostics" in captured[0]
    assert "[PASS] RDKit" in captured[0]
    window.close()


# ------------------------------------------------- the interaction analysis
def test_the_interaction_check_reports_a_missing_docking_result() -> None:
    """Without a pose there is nothing to analyse - and it must not raise."""
    from app.selftest import _check_interactions

    check = _check_interactions({})
    assert check.ok is False
    assert "no result" in check.detail


def test_the_interaction_check_analyses_a_real_pose(tmp_path: Path) -> None:
    """The check runs the very code behind the result view."""
    from app.docking.docking_config import DockingResult, PoseResult
    from app.selftest import _check_interactions

    workspace = tmp_path / "run"
    (workspace / "prepared").mkdir(parents=True)
    (workspace / "prepared" / "receptor_clean.pdb").write_text(
        "ATOM      1  OG  SER A   2       0.000   0.000   0.000  1.00 0.00           O\n"
        "END\n",
        encoding="utf-8",
    )
    (workspace / "results").mkdir()
    pose = workspace / "results" / "pose_1.pdb"
    pose.write_text(
        "HETATM    1  N1  LIG B   1       3.000   0.000   0.000  1.00 0.00           N\n"
        "END\n",
        encoding="utf-8",
    )
    result = DockingResult(
        poses=[PoseResult(pose=1, affinity=-5.0, rmsd_lb=0.0, rmsd_ub=0.0,
                          pose_file=pose)],
        workspace=workspace,
    )

    check = _check_interactions({"result": result})
    assert check.ok is True, check.detail
    assert "pocket residues" in check.detail
    assert "H-bond" in check.detail


def test_the_interaction_check_fails_when_the_receptor_is_gone(
    tmp_path: Path,
) -> None:
    """A ligand-only scene is exactly what this check exists to catch."""
    from app.docking.docking_config import DockingResult, PoseResult
    from app.selftest import _check_interactions

    workspace = tmp_path / "run"
    (workspace / "results").mkdir(parents=True)
    pose = workspace / "results" / "pose_1.pdb"
    pose.write_text(
        "HETATM    1  N1  LIG B   1       3.000   0.000   0.000  1.00 0.00           N\n"
        "END\n",
        encoding="utf-8",
    )
    result = DockingResult(
        poses=[PoseResult(pose=1, affinity=-5.0, rmsd_lb=0.0, rmsd_ub=0.0,
                          pose_file=pose)],
        workspace=workspace,
    )

    check = _check_interactions({"result": result})
    assert check.ok is False
    assert "receptor" in check.detail
    assert "receptor_clean.pdb" in check.hint


def test_the_planned_step_count_matches_the_checks_that_run() -> None:
    from app.selftest import _planned_steps

    assert _planned_steps(SelftestOptions()) == 8
    assert _planned_steps(SelftestOptions(pdb=Path("x.pdb"))) == 9
    assert _planned_steps(
        SelftestOptions(pdb=Path("x.pdb"), dock=True)) == 11
