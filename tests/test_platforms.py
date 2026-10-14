"""The platform layer: one place that knows what machine we are on.

Everything that differs between Windows and macOS - file suffixes, which
docking engine ships with us, where third party tools live, how to hand a
file to the desktop - goes through :mod:`app.utils.platforms`.  These tests
pin the behaviour down on *every* host by overriding ``sys.platform``, so the
macOS branches are covered even when the tests run on Windows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.utils import platforms


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch):
    """Pretend to run on ``platform`` (``"win32"`` / ``"darwin"``)."""

    def _switch(platform: str, arch: str = "arm64") -> None:
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(platforms.platform, "machine",
                            lambda: {"arm64": "arm64",
                                     "x86_64": "x86_64"}.get(arch, arch))

    return _switch


# --- detection -------------------------------------------------------------


def test_windows_is_recognised(on) -> None:
    on("win32", "x86_64")
    assert platforms.is_windows()
    assert not platforms.is_macos()
    assert platforms.exe_suffix() == ".exe"
    assert platforms.app_display_name() == "EnzyDock.exe"


def test_macos_is_recognised(on) -> None:
    on("darwin", "arm64")
    assert platforms.is_macos()
    assert not platforms.is_windows()
    assert platforms.exe_suffix() == ""
    assert platforms.app_display_name() == "EnzyDock"


def test_linux_is_neither(on) -> None:
    on("linux", "x86_64")
    assert platforms.is_linux()
    assert not platforms.is_windows()
    assert not platforms.is_macos()
    assert platforms.exe_suffix() == ""


def test_platform_label_is_human_readable(on) -> None:
    on("darwin", "arm64")
    assert platforms.platform_label() == "macOS arm64"
    on("win32", "x86_64")
    assert platforms.platform_label() == "Windows x86_64"


def test_amd64_is_reported_as_x86_64(on) -> None:
    """``platform.machine()`` says AMD64 on Windows and x86_64 elsewhere."""
    on("win32", "AMD64")
    assert platforms.host_arch() == "x86_64"


def test_aarch64_is_reported_as_arm64(on) -> None:
    on("linux", "aarch64")
    assert platforms.host_arch() == "arm64"


# --- the bundled engine ----------------------------------------------------


def test_windows_ships_vina_exe(on) -> None:
    on("win32", "x86_64")
    assert platforms.bundled_vina_name() == "vina.exe"


def test_apple_silicon_ships_the_arm64_engine(on) -> None:
    on("darwin", "arm64")
    assert platforms.bundled_vina_name() == "vina_mac_aarch64"


def test_intel_macs_ship_the_x86_64_engine(on) -> None:
    on("darwin", "x86_64")
    assert platforms.bundled_vina_name() == "vina_mac_x86_64"


def test_bundled_name_is_always_the_first_name_searched(on, monkeypatch) -> None:
    for platform, arch in (("win32", "x86_64"), ("darwin", "arm64"),
                           ("darwin", "x86_64")):
        on(platform, arch)
        assert platforms.vina_names()[0] == platforms.bundled_vina_name()


def test_macos_never_looks_for_a_windows_binary(on) -> None:
    """A PE file cannot run on macOS; offering it would hide the real fault."""
    on("darwin", "arm64")
    assert not any(name.endswith(".exe") for name in platforms.vina_names())


def test_windows_never_looks_for_a_macho_binary(on) -> None:
    on("win32", "x86_64")
    assert not any("mac" in name for name in platforms.vina_names())


def test_windows_still_checks_plain_vina(on) -> None:
    """People put a ``vina`` without extension on PATH even on Windows."""
    on("win32", "x86_64")
    assert "vina" in platforms.vina_names()


def test_apple_silicon_keeps_the_intel_build_as_a_last_resort(on) -> None:
    """Rosetta 2 exists; if the native build is gone, the Intel one works."""
    on("darwin", "arm64")
    names = platforms.vina_names()
    assert "vina_mac_x86_64" in names
    assert names.index("vina_mac_aarch64") < names.index("vina_mac_x86_64")


def test_intel_macs_do_not_offer_the_arm64_build(on) -> None:
    """No emulator runs arm64 code on an Intel Mac."""
    on("darwin", "x86_64")
    assert "vina_mac_aarch64" not in platforms.vina_names()


# --- where tools live ------------------------------------------------------


def test_windows_looks_in_program_files(on) -> None:
    on("win32", "x86_64")
    directories = platforms.common_tool_dirs()
    assert any("Program Files" in item for item in directories)
    assert all(len(item) > 2 and item[1] == ":" for item in directories)


def test_macos_looks_in_the_homebrew_prefixes(on) -> None:
    on("darwin", "arm64")
    directories = platforms.common_tool_dirs()
    assert "/opt/homebrew/bin" in directories      # Apple Silicon
    assert "/usr/local/bin" in directories         # Intel
    assert not any("C:" in item for item in directories)


def test_openbabel_names_follow_the_platform(on) -> None:
    on("win32", "x86_64")
    assert platforms.obabel_names() == ("obabel.exe", "babel.exe")
    on("darwin", "arm64")
    assert platforms.obabel_names() == ("obabel", "babel")


def test_openbabel_roots_follow_the_platform(on) -> None:
    on("win32", "x86_64")
    assert all(isinstance(item, Path) for item in platforms.obabel_search_roots())
    assert any("Program Files" in str(item)
               for item in platforms.obabel_search_roots())
    on("darwin", "arm64")
    # ``Path("/opt/homebrew")`` renders with the *host's* separator, so a
    # Windows test run sees ``\opt\homebrew``; compare the meaningful parts.
    roots = ["/".join(item.parts).lstrip("\\/")
             for item in platforms.obabel_search_roots()]
    assert "opt/homebrew" in roots
    assert not any(root.startswith("C:") for root in roots)


# --- per user locations ----------------------------------------------------


def test_macos_uses_the_application_support_convention(on) -> None:
    on("darwin", "arm64")
    data = platforms.default_user_data_dir()
    assert data.parts[-3:] == ("Library", "Application Support", "EnzyDock")


def test_windows_uses_a_dot_directory(on) -> None:
    on("win32", "x86_64")
    assert platforms.default_user_data_dir().name == ".enzydock"


def test_macos_workspace_is_visible_in_documents(on) -> None:
    """Users have to find their results; a hidden folder would not do."""
    on("darwin", "arm64")
    assert platforms.default_workspace_root().parts[-2:] == ("Documents",
                                                             "EnzyDock")


# --- executable bits -------------------------------------------------------


def test_windows_files_are_always_executable() -> None:
    """On Windows the concept does not exist, so nothing is ever copied."""
    if sys.platform != "win32":                        # pragma: no cover
        pytest.skip("only meaningful on Windows")
    assert platforms.runnable_copy(Path(__file__)) == Path(__file__)


def test_a_missing_file_yields_nothing(tmp_path: Path) -> None:
    assert platforms.runnable_copy(tmp_path / "nope") is None


def test_an_executable_file_is_returned_untouched(tmp_path: Path) -> None:
    if sys.platform == "win32":                        # pragma: no cover
        pytest.skip("executable bits are a POSIX concept")
    script = tmp_path / "tool"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    assert platforms.runnable_copy(script) == script


def test_a_file_without_the_executable_bit_is_copied_out(tmp_path: Path) -> None:
    """The ZIP problem: the bits survived neither the archive nor the copy.

    The copy must land somewhere we own - never inside the installed
    application, where chmod would invalidate the code signature.
    """
    if sys.platform == "win32":                        # pragma: no cover
        pytest.skip("executable bits are a POSIX concept")
    script = tmp_path / "tool"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o644)

    cache = tmp_path / "cache"
    fixed = platforms.runnable_copy(script, cache)
    assert fixed is not None
    assert fixed.parent == cache
    assert fixed != script
    # The original is left exactly as it was found.
    assert not script.stat().st_mode & 0o111


def test_quarantine_hint_names_the_real_cause_per_platform(on) -> None:
    on("darwin", "arm64")
    hint = platforms.quarantine_hint()
    assert "xattr" in hint and "quarantine" in hint.lower()
    on("win32", "x86_64")
    hint = platforms.quarantine_hint()
    assert "antivirus" in hint.lower()


# --- file dialog filters ---------------------------------------------------


def test_windows_dialog_offers_exe_files(on) -> None:
    on("win32", "x86_64")
    assert "*.exe" in platforms.vina_file_filter()
    assert "All files" in platforms.vina_file_filter()


def test_macos_dialog_does_not_filter_on_exe(on) -> None:
    """macOS binaries have no extension; a *.exe filter greys them all out."""
    on("darwin", "arm64")
    assert "*.exe" not in platforms.vina_file_filter()
    assert platforms.vina_file_filter() == "All files (*)"


# --- opening things --------------------------------------------------------


def test_macos_uses_open(monkeypatch, on) -> None:
    on("darwin", "arm64")
    calls: list[list[str]] = []
    monkeypatch.setattr(platforms.subprocess, "Popen",
                        lambda command: calls.append(command))
    assert platforms.open_path("/tmp/x")
    assert platforms.reveal_path("/tmp/x")
    assert platforms.open_in_editor("/tmp/x")
    assert calls == [["open", "/tmp/x"],
                     ["open", "-R", "/tmp/x"],
                     ["open", "-t", "/tmp/x"]]


def test_windows_uses_explorer_and_notepad(monkeypatch, on) -> None:
    on("win32", "x86_64")
    calls: list[list[str]] = []
    monkeypatch.setattr(platforms.subprocess, "Popen",
                        lambda command: calls.append(command))
    assert platforms.open_path(r"C:\x")
    assert platforms.reveal_path(r"C:\x")
    assert platforms.open_in_editor(r"C:\x")
    assert calls == [["explorer", r"C:\x"],
                     ["explorer", "/select," + r"C:\x"],
                     ["notepad", r"C:\x"]]


def test_a_missing_launcher_is_reported_not_raised(monkeypatch, on) -> None:
    on("darwin", "arm64")

    def boom(command):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(platforms.subprocess, "Popen", boom)
    assert platforms.open_path("/tmp/x") is False
