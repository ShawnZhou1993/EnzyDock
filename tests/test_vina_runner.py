"""Tests for the AutoDock Vina backend.

The engine is exercised with a *fake* Vina script (a small Python program that
prints a realistic Vina log and writes a realistic multi-model PDBQT), so these
tests run on any machine - including CI - without AutoDock Vina installed.
A single optional test at the bottom runs the real binary when it is present.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.docking.base import DockingEngine, EngineError
from app.docking.docking_config import BoxConfig, DockingParameters
from app.docking.vina_runner import (
    VinaEngine,
    _explain_failure,
    _label,
    _percent,
    _update_expected,
)

# --------------------------------------------------------------------- fixture
FAKE_LOG_HEAD = textwrap.dedent("""\
    AutoDock Vina v1.2.3
    #################################################################

    Scoring function : vina
    Grid center: X 1.0 Y 2.0 Z 3.0
    Grid size  : X 22 Y 22 Z 22

    Computing Vina grid ... done.
    Performing docking (random seed: 42) ...
    0%   10   20   30   40   50   60   70   80   90   100%
    |----|----|----|----|----|----|----|----|----|----|
    """)

FAKE_LOG_TAIL = textwrap.dedent("""\

    mode |   affinity | dist from best mode
         | (kcal/mol) | rmsd l.b.| rmsd u.b.
    -----+------------+----------+----------
       1       -8.123          0          0
       2       -7.456      1.926      4.457
    """)

FAKE_OUTPUT = """\
MODEL 1
REMARK VINA RESULT:    -8.123      0.000      0.000
REMARK INTER + INTRA:          -9.158
ROOT
ATOM      1  C   UNL     1       1.000   2.000   3.000  1.00  0.00    +0.145 A
ATOM      2  O   UNL     1       2.000   2.000   3.000  1.00  0.00    -0.320 OA
ENDROOT
TORSDOF 4
ENDMDL
MODEL 2
REMARK VINA RESULT:    -7.456      1.926      4.457
ROOT
ATOM      1  C   UNL     1       4.000   5.000   6.000  1.00  0.00    +0.145 A
ATOM      2  N   UNL     1       5.000   5.000   6.000  1.00  0.00    -0.320 N
ENDROOT
TORSDOF 4
ENDMDL
"""


def _write_fake_vina(path: Path, *, sleep: float = 0.0, marker: str = "") -> None:
    """Write a Python script that imitates AutoDock Vina's behaviour."""
    script = textwrap.dedent(f"""
        import sys, time
        args = sys.argv[1:]
        if "--version" in args:
            print("AutoDock Vina v1.2.3")
            sys.exit(0)
        out = None
        for index, token in enumerate(args):
            if token == "--out" and index + 1 < len(args):
                out = args[index + 1]
        sys.stdout.write({FAKE_LOG_HEAD!r})
        sys.stdout.flush()
        {f"open({marker!r}, 'w').close()" if marker else "pass"}
        for _ in range(51):
            sys.stdout.write("*")
            sys.stdout.flush()
            time.sleep(0.001)
        sys.stdout.write({FAKE_LOG_TAIL!r})
        sys.stdout.flush()
        time.sleep({sleep})
        if out:
            with open(out, "w") as handle:
                handle.write({FAKE_OUTPUT!r})
        sys.exit(0)
    """)
    path.write_text(script, encoding="utf-8")


@pytest.fixture
def fake_vina(tmp_path: Path):
    """Factory returning ``(VinaEngine, receptor, ligand, out_dir)``."""
    def build(*, sleep: float = 0.0, marker: Path | None = None):
        script = tmp_path / "fake_vina.py"
        _write_fake_vina(script, sleep=sleep, marker=str(marker) if marker else "")
        receptor = tmp_path / "receptor.pdbqt"
        ligand = tmp_path / "ligand.pdbqt"
        receptor.write_text("ATOM      1  C   ALA A   1       0.000   0.000   0.000  1.00 0.00    +0.000 C  \n",
                            encoding="utf-8")
        ligand.write_text("ATOM      1  C   UNL     1       0.000   0.000   0.000  1.00 0.00    +0.000 C  \n",
                          encoding="utf-8")
        engine = VinaEngine(executable=script, argv_prefix=[sys.executable])
        return engine, receptor, ligand, tmp_path / "docking"

    return build


# ------------------------------------------------------------------- registry
def test_vina_is_registered() -> None:
    import app.docking                                 # noqa: F401 - registers
    assert "AutoDock Vina" in DockingEngine.available_engines()
    assert isinstance(DockingEngine.create("AutoDock Vina"), VinaEngine)


def test_unknown_engine_gives_a_friendly_error() -> None:
    with pytest.raises(EngineError) as info:
        DockingEngine.create("GNINA")
    assert "not available" in info.value.message


# ------------------------------------------------------------------ discovery
def test_missing_binary_is_reported_in_plain_language(tmp_path: Path) -> None:
    """Vina ships with the application, so a missing one is a packaging fault.

    The message has to name the file that should be there and the thing that
    actually removed it - an antivirus on Windows, the quarantine flag on
    macOS - not tell the user to go and install Vina, which is never
    necessary.
    """
    from app.docking.vina_runner import VINA_BUNDLED_NAME
    from app.utils.platforms import quarantine_hint

    engine = VinaEngine(executable=tmp_path / "nope-binary")
    ok, reason = engine.is_available()
    assert ok is False
    assert "ships with EnzyDock" in reason
    assert VINA_BUNDLED_NAME in reason               # the expected path
    assert quarantine_hint() in reason               # platform specific cause
    assert "Install Vina" not in reason


def test_found_binary_is_reported_with_version(fake_vina) -> None:
    engine, _, _, _ = fake_vina()
    ok, reason = engine.is_available()
    assert ok is True
    assert "found" in reason


def test_version_label_does_not_repeat_the_name() -> None:
    assert _label("AutoDock Vina v1.2.3") == "AutoDock Vina v1.2.3"
    assert _label("1.2.3") == "AutoDock Vina 1.2.3"
    assert _label("") == "AutoDock Vina"


# ------------------------------------------------------------------- commands
def test_config_file_documents_the_run(tmp_path: Path) -> None:
    engine = VinaEngine(executable=tmp_path / "vina-test")
    text = engine.config_text(
        "receptor.pdbqt", "ligand.pdbqt",
        BoxConfig(center=(1.5, 2.5, 3.5), size=(20.0, 20.0, 20.0)),
        DockingParameters(exhaustiveness=16, num_poses=9, energy_range=3.0, seed=7),
    )
    assert "center_x = 1.500" in text
    assert "size_z = 20.000" in text
    assert "exhaustiveness = 16" in text
    assert "seed = 7" in text


def test_command_carries_the_box_and_the_parameters(tmp_path: Path) -> None:
    engine = VinaEngine(executable=tmp_path / "vina-test")
    command = engine.build_command(
        "r.pdbqt", "l.pdbqt",
        BoxConfig(center=(1.0, 2.0, 3.0), size=(22.0, 22.0, 22.0)),
        DockingParameters(exhaustiveness=8, num_poses=5, energy_range=3.0, seed=11),
        "out.pdbqt",
    )
    joined = " ".join(command)
    assert "--center_x 1.000" in joined
    assert "--size_y 22.000" in joined
    assert "--num_modes 5" in joined
    assert "--seed 11" in joined
    assert "--out out.pdbqt" in joined


def test_seed_is_omitted_when_the_user_left_it_random(tmp_path: Path) -> None:
    engine = VinaEngine(executable=tmp_path / "vina-test")
    command = engine.build_command(
        "r.pdbqt", "l.pdbqt", BoxConfig(center=(0, 0, 0)),
        DockingParameters(seed=None), "out.pdbqt",
    )
    assert "--seed" not in command


# ------------------------------------------------------------ progress parsing
def test_progress_bar_length_is_read_from_the_marker_line() -> None:
    assert _update_expected("|----|----|----|----|----|----|----|----|----|----|", 51) == 51
    assert _update_expected("Computing Vina grid ... done.", 51) == 51


def test_percent_is_clamped() -> None:
    assert _percent(0, 51) == 0
    assert _percent(51, 51) == 100
    assert _percent(999, 51) == 100
    assert _percent(10, 0) == 0


# ----------------------------------------------------------------- failure text
def test_box_too_small_gives_actionable_advice() -> None:
    message, _ = _explain_failure("ERROR: The ligand is outside the grid.", 1)
    assert "docking box" in message.lower()


def test_unparsable_structure_gives_actionable_advice() -> None:
    message, _ = _explain_failure("Parse error on line 12", 1)
    assert "could not read" in message.lower()


def test_unknown_failure_stays_friendly() -> None:
    message, details = _explain_failure("segmentation fault", 139)
    assert "Traceback" not in message
    assert "139" in details


# ------------------------------------------------------------------------- run
def test_run_returns_parsed_poses(fake_vina) -> None:
    engine, receptor, ligand, out_dir = fake_vina()
    result = engine.run(receptor, ligand, BoxConfig(center=(1, 2, 3)),
                        DockingParameters(num_poses=2), out_dir)
    assert [pose.pose for pose in result.poses] == [1, 2]
    assert result.best_affinity == pytest.approx(-8.123)
    assert result.poses[1].rmsd_lb == pytest.approx(1.926)
    assert result.output_file is not None and result.output_file.is_file()


def test_run_writes_config_command_and_log(fake_vina) -> None:
    engine, receptor, ligand, out_dir = fake_vina()
    engine.run(receptor, ligand, BoxConfig(center=(1, 2, 3)),
               DockingParameters(), out_dir)
    assert (out_dir / "config.txt").is_file()
    assert (out_dir / "command.txt").is_file()
    assert (out_dir / "log.txt").is_file()
    assert "REMARK VINA RESULT" not in (out_dir / "log.txt").read_text()
    assert "mode |   affinity" in (out_dir / "log.txt").read_text()


def test_run_reports_progress_from_the_progress_bar(fake_vina) -> None:
    engine, receptor, ligand, out_dir = fake_vina()
    seen: list[int] = []
    engine.run(receptor, ligand, BoxConfig(center=(1, 2, 3)),
               DockingParameters(), out_dir,
               progress=lambda percent, message: seen.append(percent))
    assert seen
    assert max(seen) == 100
    assert len(set(seen)) > 3          # the bar really is streamed


def test_run_streams_engine_output_as_log_lines(fake_vina) -> None:
    engine, receptor, ligand, out_dir = fake_vina()
    lines: list[str] = []
    engine.log_line.connect(lines.append)
    engine.run(receptor, ligand, BoxConfig(center=(1, 2, 3)),
               DockingParameters(), out_dir)
    assert any("Performing docking" in line for line in lines)


def test_cancel_stops_a_running_job(fake_vina, tmp_path: Path) -> None:
    marker = tmp_path / "started.marker"
    engine, receptor, ligand, out_dir = fake_vina(sleep=30.0, marker=marker)

    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            engine.run(receptor, ligand, BoxConfig(center=(1, 2, 3)),
                       DockingParameters(), out_dir)
        except BaseException as exc:                   # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not marker.is_file() and time.time() < deadline:
        time.sleep(0.02)
    assert marker.is_file(), "the fake engine never started"
    engine.cancel()
    thread.join(timeout=30)

    assert not thread.is_alive()
    error = outcome.get("error")
    assert isinstance(error, EngineError)
    assert "cancelled" in error.message.lower()


def test_run_without_an_executable_explains_what_to_do(tmp_path: Path) -> None:
    from app.docking.vina_runner import VINA_BUNDLED_NAME

    engine = VinaEngine(executable=tmp_path / "missing-binary")
    with pytest.raises(EngineError) as info:
        engine.run("r.pdbqt", "l.pdbqt", BoxConfig(center=(0, 0, 0)),
                   DockingParameters(), tmp_path)
    assert "ships with EnzyDock" in info.value.message
    assert "Set Docking Engine" in info.value.message   # the real menu entry
    assert VINA_BUNDLED_NAME in info.value.details


def test_run_requires_existing_input_files(fake_vina, tmp_path: Path) -> None:
    engine, receptor, ligand, out_dir = fake_vina()
    with pytest.raises(EngineError) as info:
        engine.run(tmp_path / "absent.pdbqt", ligand,
                   BoxConfig(center=(0, 0, 0)), DockingParameters(), out_dir)
    assert "missing" in info.value.message.lower()


def test_engine_rejects_an_empty_input_file(fake_vina, tmp_path: Path) -> None:
    engine, receptor, ligand, out_dir = fake_vina()
    empty = tmp_path / "empty.pdbqt"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(EngineError) as info:
        engine.run(empty, ligand, BoxConfig(center=(0, 0, 0)),
                   DockingParameters(), out_dir)
    assert "empty" in info.value.message.lower()


# --------------------------------------------------------- optional: real Vina
@pytest.mark.skipif(
    VinaEngine().executable_path() is None,
    reason="AutoDock Vina is not installed on this machine",
)
def test_real_vina_produces_poses(tmp_path: Path) -> None:
    """Run the actual binary (only when it is installed)."""
    from app.chemistry.preparation import prepare_from_smiles
    from app.chemistry.receptor import prepare_receptor

    receptor = prepare_receptor("examples/protein/1UBQ.pdb",
                                output_dir=tmp_path / "prepared")
    ligand = prepare_from_smiles("CCO", output_dir=tmp_path / "prepared")
    engine = VinaEngine()
    assert receptor.pdbqt_path and ligand.pdbqt_path
    center = receptor.center or (0.0, 0.0, 0.0)
    result = engine.run(receptor.pdbqt_path, ligand.pdbqt_path,
                        BoxConfig(center=center),
                        DockingParameters(exhaustiveness=4, num_poses=3, seed=1),
                        tmp_path / "docking")
    assert result.poses
    assert result.best_affinity is not None
    assert result.best_affinity < 0


def test_subprocess_is_used_not_a_shell(fake_vina) -> None:
    """The engine must never hand user paths to a shell."""
    engine, receptor, ligand, out_dir = fake_vina()
    original = subprocess.Popen
    seen: list[list[str]] = []

    def spy(argv, **kwargs):
        seen.append(list(argv))
        return original(argv, **kwargs)

    subprocess.Popen = spy                             # type: ignore[assignment]
    try:
        engine.run(receptor, ligand, BoxConfig(center=(1, 2, 3)),
                   DockingParameters(), out_dir)
    finally:
        subprocess.Popen = original                    # type: ignore[assignment]
    assert seen
    # ``seen[0]`` is the version probe; the last entry is the docking run.
    assert seen[-1][-2:] == ["--out", str(out_dir / "output.pdbqt")]
