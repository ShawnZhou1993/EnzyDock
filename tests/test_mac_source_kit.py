"""The macOS build kit that gets handed to somebody with a Mac.

PyInstaller cannot cross-compile, so "here is the macOS client" has to mean
"here is the source plus one double-click".  That makes the *kit* the
deliverable, and a kit that is missing one file - or whose ``.command``
lost its executable bit and opens in TextEdit instead of building - is a
wasted round trip with a colleague.

These tests build the kit for real and inspect the archive.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KIT_PREFIX = "EnzyDock-mac-build-kit-0.1.0/"


def load_script(name: str):
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
def kit_module():
    return load_script("make_mac_source_kit")


@pytest.fixture(scope="module")
def kit(kit_module, tmp_path_factory: pytest.TempPathFactory):
    """Build the kit once and hand out the archive."""
    out = tmp_path_factory.mktemp("kit")
    kit_module.main(["--out", str(out)])
    return out / f"{kit_module.KIT_NAME}.zip"


def test_the_kit_is_built(kit) -> None:
    assert kit.is_file()
    with zipfile.ZipFile(kit) as zf:
        assert zf.testzip() is None


def test_the_kit_ships_everything_the_build_needs(kit) -> None:
    """One missing file means the colleague's build fails, not ours."""
    with zipfile.ZipFile(kit) as zf:
        names = set(zf.namelist())
    for relative in (
        "scripts/build_mac.sh",
        "scripts/build_macos.py",
        "scripts/make_icons.py",
        "scripts/verify_bundle.py",
        "scripts/packaging_layout.py",
        "scripts/pack_distribution.py",
        "scripts/split_distribution.py",
        "app/utils/platforms.py",
        "app/main.py",
        "app/resources/bin/vina_mac_aarch64",
        "app/resources/bin/vina_mac_x86_64",
        "app/resources/viewer/3Dmol.min.js",
        "app/resources/licenses/THIRD-PARTY-NOTICES.txt",
        "packaging/enzydock.entitlements",
        "packaging/check-install.sh",
        "packaging/HOW-TO-RUN-mac.txt",
        "examples/protein/1UBQ.pdb",
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "run.py",
    ):
        assert f"{KIT_PREFIX}EnzyDock/{relative}" in names, (
            f"{relative} is missing from the build kit")


def test_the_kit_leaks_nothing_from_this_machine(kit) -> None:
    """A 1 GB venv or another platform's binaries must not travel."""
    with zipfile.ZipFile(kit) as zf:
        names = zf.namelist()
    for bad in (".venv", "__pycache__", ".workbuddy", ".pytest_cache",
                "/build/", "/dist/"):
        assert not [n for n in names if bad in n], f"{bad} leaked into the kit"


def test_the_double_click_entry_point_exists_and_is_executable(kit) -> None:
    """Without 0755 Finder opens it in TextEdit instead of running it."""
    with zipfile.ZipFile(kit) as zf:
        entries = {i.filename: i for i in zf.infolist()}
        name = f"{KIT_PREFIX}双击我构建 EnzyDock.command"
        assert name in entries, "the double-click entry point is missing"
        entry = entries[name]
        assert entry.external_attr >> 16 == 0o100755, (
            f"the .command is not executable: {oct(entry.external_attr >> 16)}")
        assert entry.create_system == 3, "POSIX permissions were not recorded"


def test_the_double_click_script_is_valid_posix_shell(kit, tmp_path: Path) -> None:
    shell = shutil.which("sh")
    if shell is None:                                   # pragma: no cover
        pytest.skip("no POSIX shell available to check with")
    with zipfile.ZipFile(kit) as zf:
        data = zf.read(f"{KIT_PREFIX}双击我构建 EnzyDock.command")
    assert b"\r" not in data, "CRLF would break /bin/sh"
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.startswith(b"#!/bin/sh")

    script = tmp_path / "build.command"
    script.write_bytes(data)
    completed = subprocess.run([shell, "-n", str(script)],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_the_read_me_is_present_and_readable(kit) -> None:
    with zipfile.ZipFile(kit) as zf:
        name = f"{KIT_PREFIX}先看我-READ-ME-FIRST.txt"
        text = zf.read(name).decode("utf-8")
    # The two things a colleague gets wrong: not unzipping the folder, and
    # expecting a ready-made .app.
    assert "chmod +x" in text or "可执行" in text
    assert "EnzyDock.app" in text
    assert "xcode-select" in text


def test_the_shell_scripts_keep_their_executable_bit(kit) -> None:
    """A ``.sh`` without 0755 cannot be run on macOS.

    ``ZipFile.writestr`` records 0600, so every shell script in the kit
    would arrive as "Permission denied" - the colleague would have to chmod
    it by hand before anything worked.
    """
    with zipfile.ZipFile(kit) as zf:
        entries = {i.filename: i for i in zf.infolist()}
    scripts = [name for name in entries
               if name.endswith((".sh", ".command"))]
    assert scripts, "the kit contains no shell script at all"
    for name in scripts:
        mode = entries[name].external_attr >> 16
        assert mode == 0o100755, (
            f"{name} is not executable: {oct(mode)}")


def test_the_kit_has_one_top_level_folder(kit) -> None:
    """Extracting must not spray files over the Downloads folder."""
    with zipfile.ZipFile(kit) as zf:
        tops = {n.split("/")[0] for n in zf.namelist()}
    assert tops == {"EnzyDock-mac-build-kit-0.1.0"}
