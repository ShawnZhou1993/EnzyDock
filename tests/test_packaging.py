"""Regression guards for the distribution tooling.

The files in ``packaging/`` are read by people who cannot debug them: a
colleague who received a folder that was copied incompletely.  Several
failures are invisible in an editor but fatal at run time:

* a byte-order mark in a ``.bat`` makes ``cmd.exe`` print an error instead of
  running the checks;
* LF-only line endings break the labelled ``:need`` subroutine;
* CRLF line endings in a ``.sh`` make macOS's ``sh`` choke on ``\r``;
* a ``.sh`` without the executable bit opens in an editor instead of running.

All are cheap to assert, and all are exactly what a well-meaning "save as
UTF-8" or a checkout with the wrong ``core.autocrlf`` does to these files.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGING = PROJECT_ROOT / "packaging"
PACKER_SCRIPT = PROJECT_ROOT / "scripts" / "pack_distribution.py"
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_bundle.py"


@pytest.fixture(scope="module")
def packer():
    """Load ``scripts/pack_distribution.py`` as a module."""
    spec = importlib.util.spec_from_file_location("pack_distribution",
                                                  PACKER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_text() -> str:
    return VERIFY_SCRIPT.read_text(encoding="utf-8")


# --- the two files that ship with every bundle -----------------------------


def test_the_batch_checker_is_pure_ascii() -> None:
    """A BOM would make the very first line of the .bat an error message."""
    data = (PACKAGING / "check-install.bat").read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "the .bat must not have a BOM"
    assert max(data) < 128, "the .bat must stay ASCII-only"


def test_the_batch_checker_uses_crlf() -> None:
    data = (PACKAGING / "check-install.bat").read_bytes()
    assert b"\r\n" in data
    assert data.replace(b"\r\n", b"").count(b"\n") == 0, "lone LF found"


def test_the_batch_checker_avoids_external_tools() -> None:
    """Piping into find.exe breaks when another find.exe comes first on PATH."""
    text = (PACKAGING / "check-install.bat").read_text(encoding="ascii")
    code = [line.strip().lower() for line in text.splitlines()
            if line.strip() and not line.strip().lower().startswith("rem")]
    body = "\n".join(code)
    assert "find " not in body, "counting via find.exe is fragile"
    assert "powershell" not in body
    assert "for /r " in body, "the file count must be pure batch"


def test_the_help_note_is_utf8_with_bom() -> None:
    """Notepad on older Windows shows mojibake without the BOM."""
    data = (PACKAGING / "HOW-TO-RUN.txt").read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    for needle in ("check-install.bat", "python312.dll", "--web-compat",
                   "C:\\EnzyDock"):
        assert needle in text, f"{needle!r} is missing from HOW-TO-RUN.txt"


def test_the_help_note_has_exactly_one_bom() -> None:
    """Rewriting the note used to append another BOM every time.

    ``utf-8-sig`` *adds* a BOM on write and only strips one on read, so a
    script that reads and rewrites the file grows a BOM per run.  Notepad
    then shows a stray character before the title.
    """
    data = (PACKAGING / "HOW-TO-RUN.txt").read_bytes()
    assert data.count(b"\xef\xbb\xbf") == 1, "a repeated BOM crept back in"
    assert "\ufeff" not in data.decode("utf-8-sig"), (
        "a BOM survived inside the text")


def test_the_windows_note_points_mac_users_at_the_mac_note() -> None:
    """The two notes are different; nobody should read the wrong one."""
    text = (PACKAGING / "HOW-TO-RUN.txt").read_text(encoding="utf-8-sig")
    assert "HOW-TO-RUN-mac.txt" in text
    assert "EnzyDock.app" in text


def test_the_expected_file_count_matches_a_real_build() -> None:
    """The number the .bat demands must not drift above reality."""
    internal = PROJECT_ROOT / "dist" / "EnzyDock" / "_internal"
    if not internal.is_dir():
        pytest.skip("no build present")
    text = (PACKAGING / "check-install.bat").read_text(encoding="ascii")
    match = re.search(r"a complete copy has\s+(\d+)", text)
    assert match is not None, "the expected count is not stated in the .bat"
    actual = sum(1 for path in internal.rglob("*") if path.is_file())
    assert int(match.group(1)) <= actual, (
        f"the .bat demands {match.group(1)} files but a build has {actual}")


def test_the_bundle_verifier_requires_the_helper_files() -> None:
    """The verifier must demand the helpers for *both* platforms."""
    text = _script_text()
    for name in ("check-install.bat", "HOW-TO-RUN.txt", "BUNDLE-MANIFEST.txt",
                 "check-install.sh", "HOW-TO-RUN-mac.txt"):
        assert name in text or name in (
            PROJECT_ROOT / "scripts" / "packaging_layout.py"
        ).read_text(encoding="utf-8"), f"verify_bundle.py no longer requires {name}"


# --- the macOS helpers -----------------------------------------------------


def test_the_shell_checker_has_no_carriage_returns() -> None:
    """``\\r`` at the end of every line makes sh report "command not found"."""
    data = (PACKAGING / "check-install.sh").read_bytes()
    assert b"\r" not in data, "the .sh must use LF line endings"


def test_the_shell_checker_has_no_bom() -> None:
    data = (PACKAGING / "check-install.sh").read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "the .sh must not have a BOM"
    assert data.startswith(b"#!"), "the .sh must start with a shebang"


def test_the_shell_checker_is_shellcheck_clean_enough() -> None:
    """It must work in ``/bin/sh``, which on macOS is not bash 4."""
    text = (PACKAGING / "check-install.sh").read_text(encoding="utf-8")
    assert "[[" not in text, "[[ is not portable to /bin/sh"
    assert "echo -e" not in text, "echo -e is not portable"
    # It has to run without anything the user must install first.
    for tool in ("brew", "python3", "pwsh"):
        assert f"$( {tool}" not in text


def test_the_shell_checker_names_the_quarantine_fix() -> None:
    """The one thing that stops a downloaded .app from opening."""
    text = (PACKAGING / "check-install.sh").read_text(encoding="utf-8")
    assert "xattr -dr com.apple.quarantine" in text
    assert "right-click" in text.lower() or "right click" in text.lower()


def test_the_mac_help_note_uses_lf_and_has_no_bom() -> None:
    """Read in TextEdit, which shows CRLF as visible artefacts."""
    data = (PACKAGING / "HOW-TO-RUN-mac.txt").read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in data, "the mac note should use LF line endings"


def test_the_mac_help_note_covers_the_three_traps() -> None:
    text = (PACKAGING / "HOW-TO-RUN-mac.txt").read_text(encoding="utf-8")
    for needle in (
        "EnzyDock.app",                 # what to run
        "/Applications",                # where to put it
        "xattr",                        # gatekeeper
        "check-install.sh",             # how to verify the copy
        "shasum -a 256",                # how to verify the archive
        "vina_mac_aarch64",             # the bundled engine
        "Contents/MacOS/EnzyDock",      # how to run --selftest / --web-compat
        "--selftest",
    ):
        assert needle in text, f"{needle!r} is missing from HOW-TO-RUN-mac.txt"


def test_the_mac_help_note_warns_against_moving_files_out_of_the_app() -> None:
    """The macOS equivalent of "only send EnzyDock.exe"."""
    text = (PACKAGING / "HOW-TO-RUN-mac.txt").read_text(encoding="utf-8")
    assert "Failed to load Python DLL" in text


def test_the_mac_note_tells_the_receiver_to_restore_the_executable_bit() -> None:
    """A ZIP drops POSIX permissions, so a double-click opens TextEdit.

    The merge script is chmod'ed when it is written, but that bit does not
    survive the archive - and a ``.command`` that is not executable is
    opened in an editor instead of being run.
    """
    text = (PACKAGING / "HOW-TO-RUN-mac.txt").read_text(encoding="utf-8")
    assert "chmod +x" in text
    # The advice has to sit next to the script it is about, not in a FAQ far
    # away - nobody reads that before the double-click fails.
    head = text.split("二、怎么用", 1)[-1][:1200]
    assert "chmod +x" in head
    assert "merge-parts.command" in head


# --- pack_distribution helpers --------------------------------------------


def test_sha256_matches_hashlib(tmp_path: Path, packer) -> None:
    target = tmp_path / "blob.bin"
    payload = bytes(range(256)) * 40
    target.write_bytes(payload)
    assert packer.sha256(target) == hashlib.sha256(payload).hexdigest()


def test_measure_counts_files_and_skips_the_manifest(tmp_path: Path,
                                                     packer) -> None:
    bundle = tmp_path / "EnzyDock"
    bundle.mkdir()
    (bundle / "a.dll").write_bytes(b"12")
    (bundle / "BUNDLE-MANIFEST.txt").write_text("not counted", encoding="utf-8")
    assert packer.measure(bundle) == (1, 2)


def test_human_formats_sizes(packer) -> None:
    assert packer.human(0) == "0 B"
    assert packer.human(1536) == "1.5 KiB"
    assert packer.human(610 * 1024 * 1024).startswith("610.0 MiB")


def test_copy_extras_fills_a_bundle(tmp_path: Path, packer) -> None:
    bundle = tmp_path / "EnzyDock"
    bundle.mkdir()
    copied = packer.copy_extras(bundle)
    assert {path.name for path in copied} == set(packer.EXTRAS)
    for path in copied:
        assert path.read_bytes() == (PACKAGING / path.name).read_bytes()


def test_make_zip_keeps_one_top_level_folder(tmp_path: Path, packer) -> None:
    """Extracting must not spray 537 files over the download folder."""
    bundle = tmp_path / "EnzyDock"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "EnzyDock.exe").write_bytes(b"MZ" * 16)
    (bundle / "_internal" / "python312.dll").write_bytes(b"dll" * 100)

    archive = tmp_path / "out" / "EnzyDock-0.1.0-win64.zip"
    written = packer.make_zip(bundle, archive, 6)
    assert written == 2

    with zipfile.ZipFile(archive) as opened:
        assert opened.testzip() is None
        names = opened.namelist()
    assert names, "empty archive"
    assert all(name.startswith("EnzyDock/") for name in names), names


# --- split_distribution helpers -------------------------------------------


@pytest.fixture(scope="module")
def splitter():
    """Load ``scripts/split_distribution.py`` as a module."""
    script = PROJECT_ROOT / "scripts" / "split_distribution.py"
    spec = importlib.util.spec_from_file_location("split_distribution", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_split_pieces_join_back_to_the_original(tmp_path: Path,
                                                splitter) -> None:
    """The whole point: copy /b a.001+a.002 must reproduce the file."""
    payload = bytes(range(256)) * 500           # 128 KiB
    archive = tmp_path / "EnzyDock-0.1.0-win64.zip"
    archive.write_bytes(payload)

    pieces = splitter.split(archive, tmp_path / "parts", 40 * 1024)
    assert len(pieces) == 4, [p.name for p in pieces]
    assert [p.name for p in pieces] == [
        f"{archive.name}.{i:03d}" for i in range(1, 5)]
    assert sum(p.stat().st_size for p in pieces) == len(payload)
    assert b"".join(p.read_bytes() for p in pieces) == payload
    assert splitter.sha256(archive) == hashlib.sha256(payload).hexdigest()


def test_the_merge_script_is_ascii_with_crlf(tmp_path: Path,
                                             splitter) -> None:
    """Same failure mode as check-install.bat - same guard."""
    archive = tmp_path / "EnzyDock-0.1.0-win64.zip"
    archive.write_bytes(b"x" * 100)
    pieces = splitter.split(archive, tmp_path / "parts", 40)
    bat = splitter.write_merge_bat(tmp_path / "parts", archive.name, pieces,
                                   splitter.sha256(archive))
    data = bat.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "a BOM breaks cmd.exe"
    assert max(data) < 128, "merge-parts.bat must stay ASCII-only"
    assert b"\r\n" in data
    assert data.replace(b"\r\n", b"").count(b"\n") == 0, "lone LF found"


def test_the_merge_script_needs_no_third_party_tool(tmp_path: Path,
                                                    splitter) -> None:
    """7-Zip and Python are not guaranteed on the receiving computer."""
    archive = tmp_path / "EnzyDock-0.1.0-win64.zip"
    archive.write_bytes(b"x" * 100)
    pieces = splitter.split(archive, tmp_path / "parts", 40)
    bat = splitter.write_merge_bat(tmp_path / "parts", archive.name, pieces,
                                   splitter.sha256(archive))
    text = bat.read_text(encoding="ascii")
    assert "copy /b " + "+".join(p.name for p in pieces) in text
    assert "certutil -hashfile" in text
    # Comments may mention what the script does *not* need; only the code
    # that actually runs has to stay dependency-free.
    code = "\n".join(line for line in text.splitlines()
                     if not line.strip().lower().startswith("rem"))
    lowered = code.lower()
    for forbidden in ("7z", "powershell"):
        assert forbidden not in lowered, f"{forbidden} is not guaranteed"
    # "Python DLL" may appear inside an echoed message; actually *running*
    # an interpreter is what must never happen.
    assert not re.search(r"^\s*(call\s+)?python(\.exe)?\b", lowered, re.M)


def test_the_mac_merge_script_uses_lf_even_when_packed_on_windows(
        tmp_path: Path, splitter) -> None:
    """``write_text`` would translate ``\\n`` to CRLF on the packing machine.

    A CRLF shell script dies on macOS with "syntax error near unexpected
    token", and the packing machine is usually Windows - which is exactly
    when nobody is looking.
    """
    archive = tmp_path / "EnzyDock-0.1.0-macos-arm64.zip"
    archive.write_bytes(b"x" * 100)
    pieces = splitter.split(archive, tmp_path / "parts", 40)

    script = splitter.write_merge_sh(tmp_path / "parts", archive.name, pieces,
                                     splitter.sha256(archive))
    data = script.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.startswith(b"#!/bin/sh"), "a shebang makes it double-clickable"
    assert b"\r" not in data, "CRLF would break /bin/sh on macOS"


def test_the_mac_merge_script_needs_no_third_party_tool(tmp_path: Path,
                                                        splitter) -> None:
    archive = tmp_path / "EnzyDock-0.1.0-macos-arm64.zip"
    archive.write_bytes(b"x" * 100)
    pieces = splitter.split(archive, tmp_path / "parts", 40)
    script = splitter.write_merge_sh(tmp_path / "parts", archive.name, pieces,
                                     splitter.sha256(archive))
    text = script.read_text(encoding="utf-8")

    assert "cat " + " ".join(p.name for p in pieces) + " > " in text
    assert "shasum -a 256" in text
    # Everything used has to ship with macOS itself.
    for tool in ("brew ", "python3 ", "pwsh "):
        assert tool not in text
    # And the receiver must be told what to do when the app will not open.
    assert "xattr -dr com.apple.quarantine" in text


def test_the_mac_merge_script_validates_its_own_syntax(tmp_path: Path,
                                                       splitter) -> None:
    """``sh -n`` parses it; a POSIX shell must be able to read it."""
    import shutil
    import subprocess

    shell = shutil.which("sh")
    if shell is None:                                   # pragma: no cover
        pytest.skip("no POSIX shell available to check with")
    archive = tmp_path / "EnzyDock-0.1.0-macos-arm64.zip"
    archive.write_bytes(b"x" * 100)
    pieces = splitter.split(archive, tmp_path / "parts", 40)
    script = splitter.write_merge_sh(tmp_path / "parts", archive.name, pieces,
                                     splitter.sha256(archive))
    completed = subprocess.run([shell, "-n", str(script)],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_the_parts_manifest_fingerprints_every_piece(tmp_path: Path,
                                                     splitter) -> None:
    archive = tmp_path / "EnzyDock-0.1.0-win64.zip"
    payload = bytes(range(256)) * 10
    archive.write_bytes(payload)
    pieces = splitter.split(archive, tmp_path / "parts", 1024)
    manifest = splitter.write_manifest(tmp_path / "parts", archive, pieces,
                                       1024, splitter.sha256(archive), "0.1.0")
    text = manifest.read_text(encoding="utf-8")
    for piece in pieces:
        digest = hashlib.sha256(piece.read_bytes()).hexdigest()
        assert f"{digest}  {piece.name}" in text
    assert f"Pieces      : {len(pieces)}" in text


# --- build_windows helpers ------------------------------------------------


@pytest.fixture(scope="module")
def builder():
    """Load ``scripts/build_windows.py`` as a module."""
    script = PROJECT_ROOT / "scripts" / "build_windows.py"
    spec = importlib.util.spec_from_file_location("build_windows", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retire_moves_a_bundle_aside(tmp_path: Path, builder) -> None:
    assert builder._retire(tmp_path / "absent") is True, \
        "nothing to move means the caller may proceed"
    victim = tmp_path / "EnzyDock"
    victim.mkdir()
    (victim / "EnzyDock.exe").write_bytes(b"MZ")
    assert builder._retire(victim) is True
    assert not victim.exists()
    assert (tmp_path / "EnzyDock_previous" / "EnzyDock.exe").is_file()


def test_sync_tree_replaces_an_undeletable_bundle(tmp_path: Path,
                                                  builder) -> None:
    """The fallback used when PyInstaller cannot recreate its output folder.

    Copying over files is always allowed; the point of the function is that
    the result is *identical* to the freshly built folder, including the
    removal of files that only the old bundle had.
    """
    source = tmp_path / "staging" / "EnzyDock"
    target = tmp_path / "dist" / "EnzyDock"
    (source / "_internal").mkdir(parents=True)
    (source / "EnzyDock.exe").write_bytes(b"new launcher")
    (source / "_internal" / "same.dll").write_bytes(b"unchanged")
    (source / "_internal" / "changed.dll").write_bytes(b"new content")
    (source / "_internal" / "added.dll").write_bytes(b"brand new")

    (target / "_internal").mkdir(parents=True)
    (target / "EnzyDock.exe").write_bytes(b"old launcher")
    (target / "_internal" / "same.dll").write_bytes(b"unchanged")
    (target / "_internal" / "changed.dll").write_bytes(b"old content")
    (target / "_internal" / "leftover.dll").write_bytes(b"obsolete")

    changed = builder.sync_tree(source, target)

    assert (target / "EnzyDock.exe").read_bytes() == b"new launcher"
    assert (target / "_internal" / "changed.dll").read_bytes() == b"new content"
    assert (target / "_internal" / "added.dll").read_bytes() == b"brand new"
    assert not (target / "_internal" / "leftover.dll").exists(), \
        "a file that the new build does not contain must be removed"
    # three files copied (launcher, changed.dll, added.dll) + one removed
    assert changed == 4

    # And nothing else: the two trees are now identical.
    left = sorted(p.relative_to(source).as_posix()
                  for p in source.rglob("*") if p.is_file())
    right = sorted(p.relative_to(target).as_posix()
                   for p in target.rglob("*") if p.is_file())
    assert left == right


def test_sync_tree_is_a_no_op_when_already_identical(tmp_path: Path,
                                                     builder) -> None:
    source = tmp_path / "fresh" / "EnzyDock"
    target = tmp_path / "dist" / "EnzyDock"
    for root in (source, target):
        (root / "_internal").mkdir(parents=True)
        (root / "EnzyDock.exe").write_bytes(b"x")
        (root / "_internal" / "a.dll").write_bytes(b"y")
    assert builder.sync_tree(source, target) == 0


def test_the_help_note_explains_the_single_exe_trap() -> None:
    """Sending EnzyDock.exe alone is the mistake this document must prevent."""
    text = (PACKAGING / "HOW-TO-RUN.txt").read_text(encoding="utf-8-sig")
    assert "13 MB" in text
    assert "_internal" in text
    assert "merge-parts.bat" in text


def test_the_manifest_lists_the_critical_hashes(tmp_path: Path, packer) -> None:
    bundle = tmp_path / "EnzyDock"
    bundle.mkdir()
    for relative in packer.CRITICAL_FILES:
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())

    manifest = packer.write_manifest(bundle, count=7, total=1234)
    text = manifest.read_text(encoding="utf-8")
    assert "Files         : 7" in text
    assert "1234 bytes" in text
    for relative in packer.CRITICAL_FILES:
        digest = hashlib.sha256(relative.encode()).hexdigest()
        assert f"{digest}  {relative}" in text


# --- repository hygiene -----------------------------------------------------
#
# .gitignore is the difference between a 5.7 MiB repository and a 3 GB one that
# pushes the virtual environment, the PyInstaller workpath and every docking
# run ever made.  Two mistakes are easy and silent, so both are guarded.


def _patterns() -> list[str]:
    """Every real pattern in .gitignore, comments and blanks removed."""
    text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_no_gitignore_pattern_carries_a_trailing_comment() -> None:
    """``build/  # note`` is a file named ``build/  # note``, not a directory.

    ``#`` starts a comment only at the beginning of a line, so an inline note
    silently turns the pattern into something that matches nothing.
    """
    for pattern in _patterns():
        assert "#" not in pattern, (
            f"{pattern!r} has an inline comment, so it matches nothing - put "
            "the note on its own line above it")


@pytest.mark.parametrize("must_be_ignored", [
    ".venv/", "build/", "dist/", "workspace/", "__pycache__/",
    ".pytest_cache/", ".pytest-data/", ".pytest-workspace/", ".workbuddy/",
    "*.py[cod]", "*.log", ".DS_Store",
])
def test_the_big_directories_are_actually_ignored(must_be_ignored: str) -> None:
    assert must_be_ignored in _patterns(), (
        f"{must_be_ignored} is not ignored - the first commit would carry it")


def test_the_repository_ships_the_two_files_a_public_repo_needs() -> None:
    """A repo without a licence or a README looks abandoned on arrival."""
    assert (PROJECT_ROOT / "LICENSE").is_file()
    assert (PROJECT_ROOT / "README.md").is_file()
    licence = (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in licence
    assert "Permission is hereby granted" in licence


def _gitattributes() -> list[str]:
    text = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


@pytest.mark.parametrize("pattern", [
    "*.sh text eol=lf",
    "*.command text eol=lf",
    "*.bat text eol=crlf",
    "packaging/HOW-TO-RUN.txt text eol=crlf",
    "packaging/HOW-TO-RUN-mac.txt text eol=lf",
])
def test_the_line_ending_contract_is_pinned(pattern: str) -> None:
    """Without these, a Windows checkout writes CRLF into shell scripts.

    ``build_mac.sh`` then fails on macOS with *syntax error near unexpected
    token*, and the ``.command`` sent to a colleague opens in TextEdit problem
    aside - it would not even run.
    """
    assert any(line.split() == pattern.split() for line in _gitattributes()), (
        f"{pattern!r} is missing from .gitattributes")


def test_a_fresh_checkout_produces_the_right_bytes(tmp_path: Path) -> None:
    """What a collaborator actually gets when they clone this repository.

    ``git checkout-index`` writes the index out through the same attribute
    machinery a clone uses, so it shows the real bytes - without the cost of
    a clone.  This is the check that caught the receiver note arriving with
    LF endings: the attributes looked right, but the file in the repository
    had been normalised, so the checkout produced the wrong bytes.
    """
    prefix = (tmp_path / "checkout").as_posix() + "/"
    (tmp_path / "checkout").mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ["git", "checkout-index", "-a", f"--prefix={prefix}"],
        cwd=PROJECT_ROOT, capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip(f"git checkout-index failed: {done.stderr.strip()}")

    root = tmp_path / "checkout"
    if not (root / "scripts" / "build_mac.sh").is_file():
        pytest.skip("the index is empty - nothing to check out")

    def crlf(relative: str) -> int:
        data = (root / relative).read_bytes()
        return data.count(b"\r\n")

    # Shell scripts must stay LF, or macOS refuses to run them.
    for script in ("scripts/build_mac.sh", "packaging/check-install.sh"):
        assert crlf(script) == 0, f"{script} would be CRLF after a checkout"

    # The batch file must stay CRLF, or cmd.exe trips over it.
    bat = (root / "packaging" / "check-install.bat").read_bytes()
    assert b"\r\n" in bat and bat.replace(b"\r\n", b"").count(b"\n") == 0

    # The Windows note must arrive as CRLF *with* the BOM, or Notepad shows
    # mojibake on one line.
    note = (root / "packaging" / "HOW-TO-RUN.txt").read_bytes()
    assert note.startswith(b"\xef\xbb\xbf"), "the BOM was lost"
    assert b"\r\n" in note and note.replace(b"\r\n", b"").count(b"\n") == 0

    # The macOS note must stay pure LF.
    mac_note = (root / "packaging" / "HOW-TO-RUN-mac.txt").read_bytes()
    assert b"\r\n" not in mac_note


def test_no_gitattributes_pattern_carries_a_trailing_comment() -> None:
    """Same silent trap as .gitignore: ``#`` only comments a whole line."""
    for pattern in _gitattributes():
        assert "#" not in pattern, (
            f"{pattern!r} has an inline comment, so it matches nothing")


# --- checksum sidecars ------------------------------------------------------
#
# A `.sha256` file is consumed by a machine, not by a person: the receiver
# runs `sha256sum -c` (Linux, Git Bash) or `shasum -a 256 -c` (macOS) and
# expects it to print "OK".  A CRLF line ending makes both tools read the
# file name as "archive.zip\r", which they cannot open - the tool then
# reports the download as corrupt, when the download is in fact perfect.
# That is the worst possible symptom, so the sidecars are written with LF
# on every platform, and this block proves it.


def _checksum_tool() -> list[str]:
    """The command that verifies a checksum file, if this machine has one."""
    sha256sum = shutil.which("sha256sum")
    if sha256sum:
        return [sha256sum]
    shasum = shutil.which("shasum")
    if shasum:
        return [shasum, "-a", "256"]
    return []


def test_a_checksum_file_written_with_crlf_is_unusable(tmp_path: Path) -> None:
    """Why the sidecars below must use LF - demonstrated, not asserted."""
    payload = tmp_path / "archive.zip"
    payload.write_bytes(b"payload" * 100)
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()

    broken = tmp_path / "broken.sha256"
    broken.write_bytes(f"{digest} *archive.zip\r\n".encode("ascii"))
    good = tmp_path / "good.sha256"
    good.write_bytes(f"{digest} *archive.zip\n".encode("ascii"))

    tool = _checksum_tool()
    if not tool:
        pytest.skip("neither sha256sum nor shasum is installed here")

    good_run = subprocess.run(tool + ["-c", good.name], cwd=tmp_path,
                              capture_output=True, text=True)
    assert good_run.returncode == 0, good_run.stdout + good_run.stderr

    broken_run = subprocess.run(tool + ["-c", broken.name], cwd=tmp_path,
                                capture_output=True, text=True)
    assert broken_run.returncode != 0, (
        "the CRLF file verified fine, so the premise of this suite is wrong")
    assert "could not be read" in (broken_run.stdout + broken_run.stderr).lower()


def _load(name: str):
    path = PROJECT_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_sidecar_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_bundle_manifest_uses_lf(tmp_path: Path) -> None:
    packer = _load("pack_distribution")
    layout = _load("packaging_layout").WINDOWS
    bundle = tmp_path / "EnzyDock"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "EnzyDock.exe").write_bytes(b"MZ")
    manifest = packer.write_manifest(bundle, 1, 2, layout)
    data = manifest.read_bytes()
    assert b"\r" not in data, "the bundle manifest must use LF"
    assert b"BUNDLE-MANIFEST" not in data, "it is a list, not a binary"


def test_the_archive_sidecar_uses_lf(tmp_path: Path) -> None:
    """The real packing path: bundle in, archive + sidecar out.

    ``*.zip.sha256`` is the file a receiver feeds to ``sha256sum -c``, so it
    has to be written with LF no matter which machine packed the release.
    """
    packer = _load("pack_distribution")
    bundle = tmp_path / "EnzyDock"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "EnzyDock.exe").write_bytes(b"MZ")
    out = tmp_path / "out"
    assert packer.main(["--dist", str(bundle), "--out", str(out),
                         "--no-extras"]) == 0

    sidecars = list(out.glob("*.sha256"))
    assert len(sidecars) == 1, sidecars
    assert b"\r" not in sidecars[0].read_bytes()

    manifest = bundle / "BUNDLE-MANIFEST.txt"
    assert manifest.is_file(), "the bundle manifest is missing"
    assert b"\r" not in manifest.read_bytes()


def test_the_parts_manifest_uses_lf(tmp_path: Path) -> None:
    splitter = _load("split_distribution")
    layout = _load("packaging_layout").WINDOWS
    archive = tmp_path / "EnzyDock-0.1.0-win64.zip"
    archive.write_bytes(b"zip content")
    piece = tmp_path / "EnzyDock-0.1.0-win64.zip.001"
    piece.write_bytes(b"part")
    manifest = splitter.write_manifest(tmp_path, archive, [piece], 1 << 20,
                                       splitter.sha256(archive), "0.1.0",
                                       layout)
    assert b"\r" not in manifest.read_bytes()


def test_the_mac_kit_manifest_uses_lf(tmp_path: Path) -> None:
    kit = _load("make_mac_source_kit")
    archive = tmp_path / "EnzyDock-mac-build-kit-0.1.0.zip"
    archive.write_bytes(b"kit")
    manifest = kit.write_manifest(tmp_path, archive, 1, 10)
    assert b"\r" not in manifest.read_bytes()
