"""Read the dynamic-library dependencies of a macOS Mach-O executable.

This is the macOS counterpart of :mod:`scripts.pe_imports`.  Where the Windows
script proves that ``vina.exe`` needs nothing but the operating system, this
one proves the same thing for the macOS build: every ``LC_LOAD_DYLIB`` it
reports must be a library that ships with macOS itself.

It is deliberately dependency-free (only :mod:`struct` from the standard
library) so it can run anywhere -- including on Windows, where ``otool`` does
not exist.

Usage::

    python scripts/macho_deps.py app/resources/bin/vina
    python scripts/macho_deps.py --json app/resources/bin/vina
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

# --- Mach-O constants -------------------------------------------------------

FAT_MAGIC = 0xCAFEBABE
FAT_CIGAM = 0xBEBAFECA

MH_MAGIC_64 = 0xFEEDFACF  # 64-bit, same endianness as the host
MH_CIGAM_64 = 0xCFFAEDFE  # 64-bit, byte-swapped
MH_MAGIC_32 = 0xFEEDFACE
MH_CIGAM_32 = 0xCEFAEDFE

# Load commands that name a dylib we will need at run time.
LC_LOAD_DYLIB = 0x0C
LC_ID_DYLIB = 0x0D
LC_LOAD_WEAK_DYLIB = 0x18 | 0x80000000
LC_REEXPORT_DYLIB = 0x1F | 0x80000000
LC_LAZY_LOAD_DYLIB = 0x20 | 0x80000000
LC_LOAD_UPWARD_DYLIB = 0x23 | 0x80000000

DYLIB_COMMANDS = frozenset(
    {
        LC_LOAD_DYLIB,
        LC_ID_DYLIB,
        LC_LOAD_WEAK_DYLIB,
        LC_REEXPORT_DYLIB,
        LC_LAZY_LOAD_DYLIB,
        LC_LOAD_UPWARD_DYLIB,
    }
)

# ``LC_REQ_DYLD`` commands that only ever point back inside the app / toolchain.
LC_RPATH = 0x1C | 0x80000000

CPU_NAMES = {
    0x01000007: "x86_64",
    0x0100000C: "arm64",
    0x00000007: "i386",
    0x0000000C: "arm",
    0x02000000: "any",
}

# Every Mach-O below is 64-bit little-endian in practice; these are the
# prefixes that mean "this dylib ships with macOS".
SYSTEM_PREFIXES = (
    "/usr/lib/",
    "/usr/libexec/",
    "/System/Library/",
    "/Library/System/",
)


class MachOError(Exception):
    """Raised when a file is not a Mach-O binary we understand."""


class Slice:
    """One architecture inside a (possibly universal) Mach-O file."""

    def __init__(self, arch: str, dylibs: list[str], rpaths: list[str]) -> None:
        self.arch = arch
        self.dylibs = dylibs
        self.rpaths = rpaths

    @property
    def system_dylibs(self) -> list[str]:
        return [name for name in self.dylibs if is_system_dylib(name)]

    @property
    def foreign_dylibs(self) -> list[str]:
        return [name for name in self.dylibs if not is_system_dylib(name)]

    def to_dict(self) -> dict:
        return {
            "arch": self.arch,
            "dylibs": self.dylibs,
            "rpaths": self.rpaths,
            "system": self.system_dylibs,
            "foreign": self.foreign_dylibs,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Slice({self.arch!r}, dylibs={self.dylibs!r})"


def is_system_dylib(name: str) -> bool:
    """True when ``name`` is one of the dylibs every macOS install carries."""
    return name.startswith(SYSTEM_PREFIXES)


def _read_slice(data: bytes, offset: int, arch_hint: str = "") -> Slice:
    # Apple writes the magic in the file's own byte order, so reading it as
    # little-endian yields MH_MAGIC_* when the slice is little-endian and
    # MH_CIGAM_* when it is big-endian.  (Every macOS binary we ship is
    # little-endian; the big-endian arms exist only for completeness.)
    magic = struct.unpack_from("<I", data, offset)[0]
    if magic == MH_MAGIC_64:
        endian, header_size = "<", 32
    elif magic == MH_CIGAM_64:
        endian, header_size = ">", 32
    elif magic == MH_MAGIC_32:
        endian, header_size = "<", 28
    elif magic == MH_CIGAM_32:
        endian, header_size = ">", 28
    else:  # pragma: no cover - guarded by callers
        raise MachOError(f"not a Mach-O slice (magic {magic:#x})")
    arch = arch_hint or ("x86_64-or-arm64" if header_size == 32 else "i386")

    if not arch_hint:
        cputype = struct.unpack_from(endian + "i", data, offset + 4)[0]
        arch = CPU_NAMES.get(cputype, f"cpu-{cputype:#x}")

    ncmds = struct.unpack_from(endian + "I", data, offset + 16)[0]
    cursor = offset + header_size

    dylibs: list[str] = []
    rpaths: list[str] = []
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from(endian + "II", data, cursor)
        if cmdsize <= 0:  # pragma: no cover - corrupt file
            raise MachOError("load command with zero size")
        if cmd in DYLIB_COMMANDS or cmd == LC_RPATH:
            name_offset = struct.unpack_from(endian + "I", data, cursor + 8)[0]
            raw = data[cursor + name_offset : cursor + cmdsize]
            name = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
            if cmd == LC_RPATH:
                rpaths.append(name)
            else:
                dylibs.append(name)
        cursor += cmdsize

    return Slice(arch, dylibs, rpaths)


def read(path: str | Path) -> list[Slice]:
    """Return one :class:`Slice` per architecture found in ``path``."""
    data = Path(path).read_bytes()
    if len(data) < 8:
        raise MachOError("file is too short to be a Mach-O binary")

    magic = struct.unpack_from(">I", data, 0)[0]
    if magic == FAT_MAGIC:
        # Universal binary: big-endian header, then per-arch entries.
        count = struct.unpack_from(">I", data, 4)[0]
        slices: list[Slice] = []
        for index in range(count):
            # ``fat_arch`` is 20 bytes: cputype, cpusubtype, offset, size,
            # align - all big-endian, like the header itself.
            base = 8 + index * 20
            cputype = struct.unpack_from(">i", data, base)[0]
            arch = CPU_NAMES.get(cputype, f"cpu-{cputype:#x}")
            offset, _size = struct.unpack_from(">II", data, base + 8)
            slices.append(_read_slice(data, offset, arch))
        return slices
    if magic == FAT_CIGAM:  # pragma: no cover - never seen in the wild
        raise MachOError("byte-swapped fat binaries are not supported")

    return [_read_slice(data, 0)]


def is_self_contained(path: str | Path) -> bool:
    """True when every dependency is a macOS system library."""
    return all(not item.foreign_dylibs for item in read(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("binary", help="path to a Mach-O executable")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    try:
        slices = read(args.binary)
    except (MachOError, OSError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([item.to_dict() for item in slices], indent=2))
    else:
        print(f"{args.binary}: {len(slices)} architecture(s)")
        for item in slices:
            print(f"  [{item.arch}]")
            if not item.dylibs:
                print("    (no dynamic library dependencies)")
            for name in item.dylibs:
                kind = "system" if is_system_dylib(name) else "FOREIGN"
                print(f"    {kind:8s} {name}")
            for name in item.rpaths:
                print(f"    rpath   {name}")
        bad = [n for item in slices for n in item.foreign_dylibs]
        if bad:
            print(f"\nNOT self-contained: {', '.join(sorted(set(bad)))}")
        else:
            print("\nself-contained: only macOS system libraries are needed")

    return 1 if any(item.foreign_dylibs for item in slices) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
