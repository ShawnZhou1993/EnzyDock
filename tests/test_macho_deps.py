"""Reading Mach-O load commands from a Windows or Linux checkout.

The macOS counterpart of ``scripts/pe_imports.py``.  It exists so the macOS
binaries can be audited on *any* machine: a Mac-only check would only ever
run on the one platform where nobody is worried about missing libraries.

Two layers are tested:

* the parser, against hand-built Mach-O images - so a mistake in the byte
  offsets is caught here rather than silently reporting "no dependencies";
* the two real Vina builds EnzyDock ships.
"""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = PROJECT_ROOT / "app" / "resources" / "bin"


@pytest.fixture(scope="module")
def macho():
    """Load ``scripts/macho_deps.py`` as a module."""
    script = PROJECT_ROOT / "scripts" / "macho_deps.py"
    spec = importlib.util.spec_from_file_location("macho_deps", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- building a Mach-O image by hand ---------------------------------------

MH_MAGIC_64 = 0xFEEDFACF
LC_LOAD_DYLIB = 0x0C
LC_RPATH = 0x1C | 0x80000000
CPU_TYPE_ARM64 = 0x0100000C
MH_EXECUTE = 0x2
FAT_MAGIC = 0xCAFEBABE


def load_command(command: int, payload: bytes) -> bytes:
    """One ``<cmd, cmdsize, ...>`` record, padded to 8 bytes."""
    size = 8 + len(payload)
    padded = payload + b"\x00" * (-size % 8)
    return struct.pack("<II", command, size + (-size % 8)) + padded


def dylib_command(path: str, command: int = LC_LOAD_DYLIB) -> bytes:
    """A ``dylib_command`` naming ``path``.

    Layout: ``cmd, cmdsize`` (8 bytes, added by :func:`load_command`), then
    ``dylib`` (name offset, timestamp, current version, compatibility
    version = 16 bytes), then the string.  The name offset is measured from
    the start of the command, so it is 8 + 16 = 24.
    """
    payload = struct.pack("<IIII", 24, 0, 0, 0) + path.encode()
    return load_command(command, payload)


def rpath_command(path: str) -> bytes:
    """``LC_RPATH``: ``cmd, cmdsize, path_offset`` then the string."""
    payload = struct.pack("<I", 12) + path.encode()
    return load_command(LC_RPATH, payload)


def _count(commands: bytes) -> int:
    """Number of load commands in a concatenated blob."""
    total = 0
    cursor = 0
    while cursor < len(commands):
        _cmd, size = struct.unpack_from("<II", commands, cursor)
        cursor += size
        total += 1
    return total


def macho_image(commands: bytes, cputype: int = CPU_TYPE_ARM64) -> bytes:
    """A minimal but valid 64-bit little-endian Mach-O header + commands.

    The 64-bit header is 32 bytes: ``magic, cputype, cpusubtype, filetype,
    ncmds, sizeofcmds, flags, reserved``.
    """
    header = struct.pack(
        "<IiiIIIII", MH_MAGIC_64, cputype, 0, MH_EXECUTE,
        _count(commands), len(commands), 0, 0,
    )
    assert len(header) == 32
    return header + commands


@pytest.fixture
def image(tmp_path: Path):
    """Write a synthetic Mach-O and return its path."""

    def _make(commands: bytes, cputype: int = CPU_TYPE_ARM64,
              name: str = "tool") -> Path:
        executable = tmp_path / name
        executable.write_bytes(macho_image(commands, cputype))
        return executable

    return _make


# --- the parser ------------------------------------------------------------


def test_a_system_dylib_only_binary_is_self_contained(macho, image) -> None:
    executable = image(
        dylib_command("/usr/lib/libc++.1.dylib")
        + dylib_command("/usr/lib/libSystem.B.dylib")
    )
    slices = macho.read(executable)
    assert len(slices) == 1
    assert slices[0].arch == "arm64"
    assert slices[0].foreign_dylibs == []
    assert macho.is_self_contained(executable)


def test_a_homebrew_dependency_is_reported_as_foreign(macho, image) -> None:
    """This is the failure the script exists to catch."""
    executable = image(
        dylib_command("/usr/lib/libSystem.B.dylib")
        + dylib_command("/opt/homebrew/opt/boost/lib/libboost.dylib")
    )
    assert not macho.is_self_contained(executable)
    assert macho.read(executable)[0].foreign_dylibs == [
        "/opt/homebrew/opt/boost/lib/libboost.dylib"]


def test_a_relative_dependency_is_foreign(macho, image) -> None:
    """``@rpath`` / ``@executable_path`` deps must not be treated as system."""
    executable = image(dylib_command("@rpath/libfoo.dylib"))
    assert not macho.is_self_contained(executable)


def test_system_library_prefixes_are_recognised(macho) -> None:
    for name in ("/usr/lib/libz.dylib",
                 "/System/Library/Frameworks/Foundation.framework/"
                 "Versions/A/Foundation"):
        assert macho.is_system_dylib(name)
    for name in ("/opt/homebrew/lib/libx.dylib",
                 "@rpath/libx.dylib",
                 "/Applications/Foo.app/Contents/Frameworks/libx.dylib"):
        assert not macho.is_system_dylib(name)


def test_an_rpath_is_not_a_dependency(macho, image) -> None:
    """``LC_RPATH`` points *into* the bundle; it is not a library to load."""
    executable = image(
        dylib_command("/usr/lib/libSystem.B.dylib")
        + rpath_command("@executable_path/../Frameworks")
    )
    slice_ = macho.read(executable)[0]
    assert slice_.dylibs == ["/usr/lib/libSystem.B.dylib"]
    assert slice_.rpaths == ["@executable_path/../Frameworks"]


def test_the_architecture_is_read_from_the_header(macho, image) -> None:
    arm = macho.read(image(dylib_command("/usr/lib/libSystem.B.dylib"),
                           cputype=0x0100000C))[0]
    assert arm.arch == "arm64"
    intel = macho.read(image(dylib_command("/usr/lib/libSystem.B.dylib"),
                             cputype=0x01000007, name="tool_intel"))[0]
    assert intel.arch == "x86_64"


def test_a_universal_binary_reports_every_slice(macho, tmp_path: Path) -> None:
    """Fat binaries hold one Mach-O per architecture."""
    thin = macho_image(dylib_command("/usr/lib/libSystem.B.dylib"))
    fat = tmp_path / "universal"
    # Two offsets, each aligned to 8 bytes.
    first_offset = 8 + 2 * 20
    second_offset = first_offset + len(thin)
    header = struct.pack(">II", FAT_MAGIC, 2)
    entries = struct.pack(">iiIII", CPU_TYPE_ARM64, 0, first_offset,
                          len(thin), 0)
    entries += struct.pack(">iiIII", 0x01000007, 0, second_offset,
                           len(thin), 0)
    fat.write_bytes(header + entries + thin + thin)

    slices = macho.read(fat)
    assert {item.arch for item in slices} == {"arm64", "x86_64"}


def test_a_file_that_is_not_macho_is_rejected(macho, tmp_path: Path) -> None:
    junk = tmp_path / "notmacho"
    junk.write_bytes(b"MZ" + b"\x00" * 64)          # a PE file
    with pytest.raises(macho.MachOError):
        macho.read(junk)


def test_a_truncated_file_is_rejected(macho, tmp_path: Path) -> None:
    stub = tmp_path / "stub"
    stub.write_bytes(struct.pack("<I", MH_MAGIC_64))
    with pytest.raises(macho.MachOError):
        macho.read(stub)


def test_an_empty_file_is_rejected(macho, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    with pytest.raises(macho.MachOError):
        macho.read(empty)


def test_the_command_line_reports_success(macho, image, capsys) -> None:
    executable = image(dylib_command("/usr/lib/libSystem.B.dylib"))
    assert macho.main([str(executable)]) == 0
    assert "self-contained" in capsys.readouterr().out


def test_the_command_line_reports_a_foreign_dependency(macho, image,
                                                       capsys) -> None:
    executable = image(dylib_command("/opt/homebrew/lib/libx.dylib"))
    assert macho.main([str(executable)]) == 1
    assert "NOT self-contained" in capsys.readouterr().out


def test_the_command_line_emits_json(macho, image, capsys) -> None:
    import json

    executable = image(dylib_command("/usr/lib/libSystem.B.dylib"))
    assert macho.main([str(executable), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["arch"] == "arm64"
    assert payload[0]["foreign"] == []


# --- the binaries we actually ship -----------------------------------------


@pytest.mark.parametrize("name", ["vina_mac_aarch64", "vina_mac_x86_64"])
def test_the_shipped_macos_engine_is_self_contained(macho, name: str) -> None:
    binary = BIN_DIR / name
    if not binary.is_file():                            # pragma: no cover
        pytest.skip(f"{name} is not in this checkout")
    foreign = [dylib for slice_ in macho.read(binary)
               for dylib in slice_.foreign_dylibs]
    assert not foreign, (
        f"{name} would need {sorted(set(foreign))} at run time; those are not "
        "part of macOS and would have to travel with the app")


@pytest.mark.parametrize("name,arch",
                         [("vina_mac_aarch64", "arm64"),
                          ("vina_mac_x86_64", "x86_64")])
def test_the_shipped_macos_engine_has_the_expected_architecture(macho, name,
                                                                arch) -> None:
    binary = BIN_DIR / name
    if not binary.is_file():                            # pragma: no cover
        pytest.skip(f"{name} is not in this checkout")
    slice_ = macho.read(binary)[0]
    assert slice_.arch == arch
