"""Tests for :mod:`app.docking.docking_config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.docking.docking_config import (
    BoxConfig,
    DockingConfigError,
    DockingParameters,
    DockingResult,
    PoseResult,
)


# --------------------------------------------------------------------------- Box
def test_box_defaults_and_volume() -> None:
    box = BoxConfig(center=(1.0, 2.0, 3.0))
    assert box.size == (22.0, 22.0, 22.0)
    assert box.volume == pytest.approx(22.0 ** 3)
    assert "center=(1.000, 2.000, 3.000)" in str(box)


@pytest.mark.parametrize(
    "center,size",
    [
        ((0.0, 0.0), (10.0, 10.0, 10.0)),        # centre needs 3 components
        ((0.0, 0.0, 0.0), (10.0, 10.0)),         # size needs 3 components
        ((0.0, 0.0, 0.0), (0.0, 10.0, 10.0)),    # zero edge
        ((0.0, 0.0, 0.0), (-5.0, 10.0, 10.0)),   # negative edge
        ((0.0, 0.0, 0.0), (80.0, 10.0, 10.0)),   # absurdly large
    ],
)
def test_box_rejects_invalid_input(center, size) -> None:
    with pytest.raises(DockingConfigError):
        BoxConfig(center=center, size=size)


def test_box_rejects_non_numeric() -> None:
    with pytest.raises(DockingConfigError):
        BoxConfig(center=("a", 0.0, 0.0))


# ------------------------------------------------------------------ parameters
def test_parameters_defaults_match_specification() -> None:
    params = DockingParameters()
    assert params.exhaustiveness == 8
    assert params.num_poses == 10
    assert params.energy_range == 3.0
    assert params.seed is None
    assert params.engine == "AutoDock Vina"
    params.validate()


@pytest.mark.parametrize(
    "field,value",
    [
        ("exhaustiveness", 0),
        ("exhaustiveness", 500),
        ("num_poses", 0),
        ("num_poses", 500),
        ("energy_range", -1.0),
        ("energy_range", 50.0),
        ("seed", -3),
        ("cpu", -1),
    ],
)
def test_parameters_validate_rejects_out_of_range(field: str, value) -> None:
    params = DockingParameters()
    setattr(params, field, value)
    with pytest.raises(DockingConfigError):
        params.validate()


def test_parameters_to_dict_is_serialisable() -> None:
    payload = DockingParameters(seed=42).to_dict()
    assert payload["seed"] == 42
    assert isinstance(payload, dict)


# ---------------------------------------------------------------------- result
def test_docking_result_best_pose() -> None:
    poses = [
        PoseResult(pose=1, affinity=-7.2, rmsd_lb=1.5, rmsd_ub=2.0),
        PoseResult(pose=2, affinity=-9.1, rmsd_lb=0.0, rmsd_ub=0.0),
        PoseResult(pose=3, affinity=-8.0, rmsd_lb=2.0, rmsd_ub=3.1),
    ]
    result = DockingResult(poses=poses, engine="AutoDock Vina")
    assert result.best_affinity == pytest.approx(-9.1)
    assert result.best_pose is poses[1]

    rows = result.to_rows()
    assert len(rows) == 3
    assert rows[0]["pose"] == 1
    assert rows[0]["affinity"] == pytest.approx(-7.2)


def test_empty_result_has_no_best_pose() -> None:
    result = DockingResult()
    assert result.best_pose is None
    assert result.best_affinity is None
    assert result.to_rows() == []


def test_pose_result_optional_pose_file(tmp_path: Path) -> None:
    pose = PoseResult(pose=1, affinity=-8.5, pose_file=tmp_path / "pose_1.pdb")
    assert pose.to_dict()["pose_file"].endswith("pose_1.pdb")
    assert PoseResult(pose=2, affinity=-1.0).to_dict()["pose_file"] == ""
