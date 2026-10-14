"""Tests for :mod:`app.docking.result_parser`."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.docking.docking_config import DockingResult, PoseResult
from app.docking.result_parser import (
    attach_pose_files,
    iter_models,
    parse_log_table,
    parse_pose_energies,
    parse_poses,
    pdbqt_to_pdb,
    read_output_text,
    split_poses,
    summarise,
    write_summary_csv,
)

OUTPUT = """\
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
ATOM      1  C   UNL     1       4.000   5.000   6.000  1.00  0.00    +0.145 C
ATOM      2  HD  UNL     1       5.000   5.000   6.000  1.00  0.00    +0.120 HD
ENDROOT
ENDMDL
"""


@pytest.fixture
def output_file(tmp_path: Path) -> Path:
    path = tmp_path / "output.pdbqt"
    path.write_text(OUTPUT, encoding="utf-8")
    return path


# ------------------------------------------------------------------- tolerance
def test_reading_tolerates_the_nul_byte_vina_writes(tmp_path: Path) -> None:
    path = tmp_path / "out.pdbqt"
    path.write_bytes(b"MODEL 1\n\x00REMARK VINA RESULT:    -1.0 0.0 0.0\nENDMDL\n")
    text = read_output_text(path)
    assert "\x00" not in text
    assert "VINA RESULT" in text


def test_reading_tolerates_broken_encoding(tmp_path: Path) -> None:
    path = tmp_path / "out.pdbqt"
    path.write_bytes(b"MODEL 1\nREMARK \xff\xfe bad bytes\nENDMDL\n")
    assert "bad bytes" in read_output_text(path)


# --------------------------------------------------------------------- parsing
def test_pose_energies_are_read_from_the_remarks(output_file: Path) -> None:
    energies = parse_pose_energies(read_output_text(output_file))
    assert energies == [(-8.123, 0.0, 0.0), (-7.456, 1.926, 4.457)]


def test_models_are_split_by_number(output_file: Path) -> None:
    models = list(iter_models(read_output_text(output_file)))
    assert [number for number, _ in models] == [1, 2]
    assert "TORSDOF" in models[0][1]


def test_parse_poses_numbers_from_one(output_file: Path) -> None:
    poses = parse_poses(output_file)
    assert [pose.pose for pose in poses] == [1, 2]
    assert poses[0].affinity == pytest.approx(-8.123)
    assert poses[1].rmsd_ub == pytest.approx(4.457)


def test_parse_poses_returns_nothing_for_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdbqt"
    path.write_text("no remarks here\n", encoding="utf-8")
    assert parse_poses(path) == []


def test_score_table_is_parsed_as_a_fallback() -> None:
    text = (
        "mode |   affinity | dist from best mode\n"
        "     | (kcal/mol) | rmsd l.b.| rmsd u.b.\n"
        "-----+------------+----------+----------\n"
        "   1       -4.516          0          0\n"
        "   2       -4.073      1.926      4.457\n"
    )
    poses = parse_log_table(text)
    assert [(p.pose, p.affinity, p.rmsd_lb) for p in poses] == [
        (1, -4.516, 0.0), (2, -4.073, 1.926)
    ]


def test_score_table_ignores_unrelated_numbers() -> None:
    assert parse_log_table("Grid size  : X 22 Y 22 Z 22\n") == []


# -------------------------------------------------------------- file splitting
def test_pdbqt_models_become_plain_pdb() -> None:
    pdb = pdbqt_to_pdb(
        "ATOM      1  C   UNL     1       1.000   2.000   3.000  1.00  0.00    +0.145 A \n"
        "ATOM      2  HD  UNL     1       2.000   2.000   3.000  1.00  0.00    +0.120 HD \n"
        "ROOT\n"
        "TORSDOF 4\n",
        title="pose 1",
    )
    lines = pdb.splitlines()
    assert lines[0].startswith("REMARK")
    assert lines[1].startswith("ATOM")
    assert lines[-1] == "END"
    assert lines[2][76:78].strip() == "H"          # AutoDock "HD" -> element H
    assert len(lines[1]) == 78
    assert "TORSDOF" not in pdb


def test_split_poses_writes_one_file_per_model(output_file: Path, tmp_path: Path) -> None:
    created = split_poses(output_file, tmp_path / "results")
    names = sorted(path.name for path in created)
    assert "pose_1.pdb" in names and "pose_2.pdbqt" in names
    assert (tmp_path / "results" / "pose_2.pdb").read_text(encoding="utf-8").count("ATOM") == 2


def test_attach_pose_files_points_at_existing_files(tmp_path: Path) -> None:
    (tmp_path / "pose_1.pdb").write_text("ATOM\n", encoding="utf-8")
    poses = [PoseResult(pose=1, affinity=-1.0), PoseResult(pose=2, affinity=-2.0)]
    attach_pose_files(poses, tmp_path)
    assert poses[0].pose_file is not None
    assert poses[0].pose_file.name == "pose_1.pdb"
    assert poses[1].pose_file is None


# ------------------------------------------------------------------- reporting
def test_summary_csv_has_a_header_and_one_row_per_pose(tmp_path: Path) -> None:
    result = DockingResult(poses=[PoseResult(1, -8.123), PoseResult(2, -7.456, 1.9, 4.5)])
    path = write_summary_csv(result, tmp_path / "summary.csv")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].startswith("pose,affinity_kcal_mol")
    assert len(lines) == 3
    assert "-8.123" in lines[1]


def test_summarise_handles_an_empty_result() -> None:
    assert "no poses" in summarise(DockingResult())


def test_summarise_reports_best_affinity() -> None:
    text = summarise(DockingResult(engine="AutoDock Vina",
                                   poses=[PoseResult(1, -8.5), PoseResult(2, -6.0)]))
    assert "-8.50" in text
    assert "2 poses" in text
