"""The per-platform bundle layout that the packaging scripts share.

Windows produces ``EnzyDock/EnzyDock.exe`` plus ``_internal``; macOS produces
``EnzyDock.app`` with its payload split across ``Contents/Frameworks`` and
``Contents/Resources``.  Getting a path wrong here means shipping a bundle
whose integrity check reports MISSING for a file that is actually there - so
the layout is described once and asserted here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    """Import a script from ``scripts/`` by file path.

    The module is registered in ``sys.modules`` before it is executed because
    ``packaging_layout`` defines a dataclass, and ``dataclasses`` looks the
    defining module up by name when it resolves annotations.
    """
    script = PROJECT_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:                               # pragma: no cover
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture(scope="module")
def layouts():
    """The shared bundle layout description."""
    return load_script("packaging_layout")


# --- the two layouts -------------------------------------------------------


def test_windows_layout_matches_a_real_onedir_build(layouts) -> None:
    assert layouts.WINDOWS.bundle_dir == "EnzyDock"
    assert layouts.WINDOWS.launcher == "EnzyDock.exe"
    assert layouts.WINDOWS.payload_dir == "_internal"
    assert layouts.WINDOWS.merge_script == "merge-parts.bat"
    assert layouts.WINDOWS.hash_command == "certutil -hashfile"


def test_macos_layout_matches_a_real_app_bundle(layouts) -> None:
    assert layouts.MACOS.bundle_dir == "EnzyDock.app"
    assert layouts.MACOS.launcher == "Contents/MacOS/EnzyDock"
    assert layouts.MACOS.payload_dir == "Contents/Frameworks"
    assert layouts.MACOS.merge_script == "merge-parts.command"
    assert layouts.MACOS.hash_command == "shasum -a 256"


def test_every_layout_path_is_relative_to_the_bundle_dir(layouts) -> None:
    """``--dist`` points at the bundle, so nothing may repeat its name.

    The macOS layout used to spell its paths as ``EnzyDock.app/Contents/...``
    while Windows used ``EnzyDock.exe`` - two different reference points in
    one dataclass.  The packer then looked for
    ``EnzyDock.app/EnzyDock.app/Contents/MacOS/EnzyDock`` and refused to pack
    a perfectly good build.
    """
    for layout in (layouts.WINDOWS, layouts.MACOS):
        for relative in (layout.launcher, *layout.critical_files):
            assert not relative.startswith(layout.bundle_dir + "/"), (
                f"{layout.system}: {relative} repeats the bundle name")
        assert not layout.payload_dir.startswith(layout.bundle_dir)


def test_every_layout_ships_a_receiver_side_checker(layouts) -> None:
    for layout in (layouts.WINDOWS, layouts.MACOS):
        assert layout.extras, f"{layout.system} has no helper files"
        checker = layout.extras[0]
        assert "check-install" in checker
        source = PROJECT_ROOT / "packaging" / checker
        assert source.is_file(), f"packaging/{checker} does not exist"


def test_every_layout_ships_a_how_to_run_note(layouts) -> None:
    for layout in (layouts.WINDOWS, layouts.MACOS):
        note = [name for name in layout.extras if name.startswith("HOW-TO-RUN")]
        assert note, f"{layout.system} has no HOW-TO-RUN note"
        assert (PROJECT_ROOT / "packaging" / note[0]).is_file()


# --- critical files --------------------------------------------------------


def test_the_checker_verifies_every_critical_file(layouts) -> None:
    """A file the manifest fingerprints but the checker ignores is a gap."""
    for layout in (layouts.WINDOWS, layouts.MACOS):
        checker = (PROJECT_ROOT / "packaging" / layout.extras[0])
        text = checker.read_text(encoding="utf-8", errors="replace")
        for relative in layout.critical_files:
            # The last path segment is what a checker will mention.
            leaf = relative.rsplit("/", 1)[-1]
            assert leaf in text, (
                f"{layout.extras[0]} does not check {relative}")


def test_windows_critical_files_live_under_internal(layouts) -> None:
    for relative in layouts.WINDOWS.critical_files[1:]:
        assert relative.startswith("_internal/")


def test_macos_critical_files_live_inside_the_app(layouts) -> None:
    for relative in layouts.MACOS.critical_files:
        assert relative.startswith("Contents/")


def test_macos_fingerprints_the_docking_engine(layouts) -> None:
    """The engine is the one file whose loss looks like a missing install."""
    assert any("vina" in name for name in layouts.MACOS.critical_files)
    assert any("vina" in name for name in layouts.WINDOWS.critical_files)


# --- archive names ---------------------------------------------------------


def test_archive_names_are_distinct_per_platform(layouts) -> None:
    win = layouts.WINDOWS.archive_stem("0.1.0")
    mac = layouts.MACOS.archive_stem("0.1.0")
    assert win == "EnzyDock-0.1.0-win64"
    assert mac == "EnzyDock-0.1.0-macos-arm64"
    assert win != mac


def test_a_forced_layout_overrides_detection(layouts) -> None:
    assert layouts.for_system("macos") is layouts.MACOS
    assert layouts.for_system("Windows") is layouts.WINDOWS
    assert layouts.for_system("darwin") is layouts.MACOS
    with pytest.raises(ValueError):
        layouts.for_system("plan9")


def test_detection_from_disk(layouts, tmp_path: Path) -> None:
    app = tmp_path / "EnzyDock.app"
    app.mkdir()
    assert layouts.detect_layout(app) is layouts.MACOS

    onedir = tmp_path / "EnzyDock"
    onedir.mkdir()
    (onedir / "EnzyDock.exe").write_bytes(b"MZ")
    assert layouts.detect_layout(onedir) is layouts.WINDOWS


def test_detection_falls_back_to_the_archive_name(layouts) -> None:
    """``--dist`` may point at a folder that no longer exists."""
    assert layouts.detect_layout(Path("/nowhere/EnzyDock.app")) is layouts.MACOS
    assert layouts.detect_layout(Path("/nowhere/EnzyDock")) is layouts.WINDOWS


def test_current_matches_the_running_platform(layouts) -> None:
    expected = layouts.MACOS if sys.platform == "darwin" else layouts.WINDOWS
    assert layouts.current() is expected


# --- cross checks with the scripts that consume the layout ----------------


def test_the_splitter_parses_every_archive_name(layouts) -> None:
    module = load_script("split_distribution")

    for layout in (layouts.WINDOWS, layouts.MACOS):
        name = f"{layout.archive_stem('0.1.0')}.zip"
        assert module.version_from_archive(name) == "0.1.0"


def test_the_verifier_uses_the_platform_engine_name(layouts) -> None:
    module = load_script("verify_bundle")

    assert module.bundled_vina_name(layouts.WINDOWS) == "vina.exe"
    assert module.bundled_vina_name(layouts.MACOS) == "vina_mac_aarch64"
    for layout in (layouts.WINDOWS, layouts.MACOS):
        files = module.required_files(layout)
        assert "BUNDLE-MANIFEST.txt" in files
        assert any("check-install" in name for name in files)
