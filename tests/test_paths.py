"""Tests for :mod:`app.utils.paths`."""

from __future__ import annotations

import os
from pathlib import Path

from app.utils import paths


def test_workspace_root_respects_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_WORKSPACE, str(tmp_path / "ws"))
    root = paths.workspace_root()
    assert root == tmp_path / "ws"
    assert root.is_dir()


def test_create_workspace_builds_standard_tree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_WORKSPACE, str(tmp_path))
    workspace = paths.create_workspace(tag="My Protein!")
    for sub in paths.WORKSPACE_SUBDIRS:
        assert (workspace / sub).is_dir(), f"missing {sub}"
    assert workspace.parent == tmp_path
    assert "My-Protein" in workspace.name


def test_create_workspace_never_collides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_WORKSPACE, str(tmp_path))
    first = paths.create_workspace()
    second = paths.create_workspace()
    assert first != second
    assert first.is_dir() and second.is_dir()


def test_unique_path_appends_counter(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()
    assert paths.unique_path(target) == tmp_path / "out_1"
    (tmp_path / "out_1").mkdir()
    assert paths.unique_path(target) == tmp_path / "out_2"


def test_safe_copy_preserves_original_and_name(tmp_path: Path) -> None:
    source = tmp_path / "protein.pdb"
    source.write_text("ATOM\n", encoding="utf-8")
    destination = tmp_path / "workspace" / "input"

    copied = paths.safe_copy(source, destination)
    assert copied.is_file()
    assert copied.parent == destination
    assert copied.read_text(encoding="utf-8") == "ATOM\n"

    again = paths.safe_copy(source, destination)
    assert again.name == "protein_1.pdb"      # original is never overwritten
    assert source.read_text(encoding="utf-8") == "ATOM\n"


def test_resource_dir_points_into_package() -> None:
    assert paths.resource_dir().name == "resources"
    assert (paths.resource_dir() / "viewer").is_dir()


def test_user_data_dir_uses_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "data"))
    assert paths.user_data_dir() == tmp_path / "data"


def test_iter_files_filters_by_suffix(tmp_path: Path) -> None:
    (tmp_path / "a.pdb").write_text("", encoding="utf-8")
    (tmp_path / "b.cif").write_text("", encoding="utf-8")
    (tmp_path / "c.txt").write_text("", encoding="utf-8")
    found = paths.iter_files(tmp_path, [".pdb", "cif"])
    assert [p.name for p in found] == ["a.pdb", "b.cif"]
    assert paths.iter_files(tmp_path / "missing", [".pdb"]) == []


def test_paths_are_absolute_and_windows_safe() -> None:
    for candidate in (paths.project_root(), paths.app_package_dir(),
                      paths.resource_dir()):
        assert candidate.is_absolute()
        assert "\\" not in candidate.as_posix() or os.name == "nt"
