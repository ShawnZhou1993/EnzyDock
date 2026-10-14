"""Tests for :mod:`app.docking.pipeline` - the end-to-end orchestration.

The docking backend is replaced by a scripted fake (see
``tests/test_vina_runner.py``) so the pipeline can be exercised without
AutoDock Vina present; preparation is skipped by handing the pipeline
already-prepared PDBQT files.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from app.chemistry.preparation import PreparedLigand
from app.chemistry.receptor import PreparedReceptor
from app.docking.docking_config import BoxConfig, DockingConfigError, DockingParameters
from app.docking.pipeline import DockingRequest, run_docking
from app.docking.vina_runner import VinaEngine

FAKE_SCRIPT = textwrap.dedent("""
    import sys, time
    args = sys.argv[1:]
    if "--version" in args:
        print("AutoDock Vina v1.2.3")
        sys.exit(0)
    out = args[args.index("--out") + 1]
    sys.stdout.write("Performing docking (random seed: 1) ...\\n")
    sys.stdout.write("|----|----|----|----|----|----|----|----|----|----|\\n")
    sys.stdout.flush()
    for _ in range(51):
        sys.stdout.write("*")
        sys.stdout.flush()
    sys.stdout.write("\\n\\n   1       -6.500          0          0\\n")
    sys.stdout.flush()
    with open(out, "w") as handle:
        handle.write(
            "MODEL 1\\n"
            "REMARK VINA RESULT:    -6.500      0.000      0.000\\n"
            "ATOM      1  C   UNL     1       1.000   2.000   3.000  1.00  0.00    +0.145 C  \\n"
            "ENDMDL\\n"
        )
    sys.exit(0)
""")

PDBQT_LINE = ("ATOM      1  C   ALA A   1       0.000   0.000   0.000  "
              "1.00  0.00    +0.000 C  \n")


@pytest.fixture
def workspace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the workspace root into the temporary directory."""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("ENZYDOCK_WORKSPACE", str(root))
    return root


@pytest.fixture
def prepared(tmp_path: Path):
    """Factory creating ``(PreparedReceptor, PreparedLigand)`` on disk."""
    def build(directory: Path | None = None):
        directory = directory or tmp_path / "prepared"
        directory.mkdir(parents=True, exist_ok=True)
        receptor_pdbqt = directory / "receptor.pdbqt"
        ligand_pdbqt = directory / "ligand.pdbqt"
        receptor_pdbqt.write_text(PDBQT_LINE, encoding="utf-8")
        ligand_pdbqt.write_text(PDBQT_LINE, encoding="utf-8")
        return (
            PreparedReceptor(name="test", pdbqt_path=receptor_pdbqt, atom_count=1),
            PreparedLigand(name="test", smiles="C", pdbqt_path=ligand_pdbqt),
        )

    return build


@pytest.fixture
def fake_engine(tmp_path: Path) -> VinaEngine:
    script = tmp_path / "fake_vina.py"
    script.write_text(FAKE_SCRIPT, encoding="utf-8")
    return VinaEngine(executable=script, argv_prefix=[sys.executable])


def _request(tmp_path: Path, **overrides) -> DockingRequest:
    source = tmp_path / "protein.pdb"
    source.write_text(PDBQT_LINE, encoding="utf-8")
    base = dict(
        receptor_source=source,
        ligand_source="CCO",
        ligand_kind="smiles",
        box=BoxConfig(center=(0.0, 0.0, 0.0), size=(20.0, 20.0, 20.0)),
        parameters=DockingParameters(num_poses=1, engine="AutoDock Vina"),
        receptor_name="test",
    )
    base.update(overrides)
    return DockingRequest(**base)                      # type: ignore[arg-type]


# ------------------------------------------------------------------ validation
def test_a_missing_protein_is_explained(tmp_path: Path) -> None:
    request = _request(tmp_path, receptor_source=tmp_path / "absent.pdb")
    with pytest.raises(DockingConfigError) as info:
        run_docking(request)
    assert "protein structure" in str(info.value).lower()


def test_a_missing_ligand_is_explained(tmp_path: Path) -> None:
    request = _request(tmp_path, ligand_source="   ")
    with pytest.raises(DockingConfigError) as info:
        run_docking(request)
    assert "ligand" in str(info.value).lower()


def test_invalid_parameters_are_rejected(tmp_path: Path) -> None:
    request = _request(tmp_path, parameters=DockingParameters(exhaustiveness=0))
    with pytest.raises(DockingConfigError):
        run_docking(request)


# ----------------------------------------------------------------------- happy
def test_pipeline_produces_a_result_and_a_workspace(
    workspace_env: Path, prepared, fake_engine: VinaEngine, tmp_path: Path,
) -> None:
    receptor, ligand = prepared()
    request = _request(tmp_path, prepared_receptor=receptor,
                       prepared_ligand=ligand, engine=fake_engine)
    result = run_docking(request)

    assert len(result.poses) == 1
    assert result.best_affinity == pytest.approx(-6.5)
    assert result.workspace is not None
    assert result.workspace.parent == workspace_env

    for sub in ("input", "prepared", "docking", "results", "visualization"):
        assert (result.workspace / sub).is_dir()
    assert (result.workspace / "results" / "pose_1.pdb").is_file()
    assert (result.workspace / "results" / "pose_1.pdbqt").is_file()
    assert (result.workspace / "results" / "summary.csv").is_file()
    assert (result.workspace / "docking" / "log.txt").is_file()
    assert result.poses[0].pose_file is not None


def test_pipeline_archives_the_original_inputs(
    workspace_env: Path, prepared, fake_engine: VinaEngine, tmp_path: Path,
) -> None:
    receptor, ligand = prepared()
    request = _request(tmp_path, ligand_source="CC(=O)Oc1ccccc1C(=O)O",
                       prepared_receptor=receptor, prepared_ligand=ligand,
                       engine=fake_engine)
    result = run_docking(request)
    input_dir = result.workspace / "input"
    assert (input_dir / "protein.pdb").is_file()
    assert (input_dir / "ligand.smi").read_text(encoding="utf-8").startswith("CC(=O)O")


def test_pipeline_archives_a_ligand_file(
    workspace_env: Path, prepared, fake_engine: VinaEngine, tmp_path: Path,
) -> None:
    receptor, ligand = prepared()
    ligand_file = tmp_path / "aspirin.sdf"
    ligand_file.write_text("dummy\n", encoding="utf-8")
    request = _request(tmp_path, ligand_source=str(ligand_file), ligand_kind="file",
                       prepared_receptor=receptor, prepared_ligand=ligand,
                       engine=fake_engine)
    result = run_docking(request)
    assert (result.workspace / "input" / "aspirin.sdf").is_file()


def test_pipeline_progress_covers_the_whole_range(
    workspace_env: Path, prepared, fake_engine: VinaEngine, tmp_path: Path,
) -> None:
    receptor, ligand = prepared()
    request = _request(tmp_path, prepared_receptor=receptor,
                       prepared_ligand=ligand, engine=fake_engine)
    seen: list[int] = []
    run_docking(request, progress=lambda percent, message: seen.append(percent))
    assert min(seen) == 0
    assert max(seen) == 100
    assert seen == sorted(seen)                        # never goes backwards


def test_pipeline_reports_the_stage_names(
    workspace_env: Path, prepared, fake_engine: VinaEngine, tmp_path: Path,
) -> None:
    receptor, ligand = prepared()
    request = _request(tmp_path, prepared_receptor=receptor,
                       prepared_ligand=ligand, engine=fake_engine)
    messages: list[str] = []
    run_docking(request, progress=lambda percent, message: messages.append(message))
    assert any("AutoDock Vina" in message for message in messages)
    assert any("poses" in message for message in messages)


def test_an_unavailable_engine_is_reported_before_any_work(
    workspace_env: Path, prepared, tmp_path: Path,
) -> None:
    from app.docking.base import EngineError

    receptor, ligand = prepared()
    engine = VinaEngine(executable=tmp_path / "absent-binary")
    request = _request(tmp_path, prepared_receptor=receptor,
                       prepared_ligand=ligand, engine=engine)
    with pytest.raises(EngineError) as info:
        run_docking(request)
    # Vina ships with EnzyDock, so an unusable engine means the bundled copy
    # is damaged - the message says that instead of "please install Vina".
    assert "could not be started" in info.value.message.lower()
    assert "ships with EnzyDock" in info.value.message


def test_prepared_files_are_reused_and_not_rebuilt(
    workspace_env: Path, prepared, fake_engine: VinaEngine, tmp_path: Path,
) -> None:
    """A valid cached PDBQT means the preparation stage is skipped."""
    receptor, ligand = prepared()
    request = _request(tmp_path, prepared_receptor=receptor,
                       prepared_ligand=ligand, engine=fake_engine)
    result = run_docking(request)
    assert result.receptor == receptor.pdbqt_path
    assert result.ligand == ligand.pdbqt_path
    # Nothing was re-written into the run's own prepared folder.
    assert list((result.workspace / "prepared").iterdir()) == []
