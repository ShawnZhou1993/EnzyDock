"""Dump the imported DLL names of a PE file (no external dependency).

Usage:  python build/pe_imports.py <path-to-exe-or-dll>
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


def imports(path: Path) -> list[str]:
    data = path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe : pe + 4] == b"PE\0\0", "not a PE file"
    size_optional = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    # Data directory 1 is the import table; offset differs between PE32/PE32+.
    dd = optional + (112 if magic == 0x20B else 96)
    rva, size = struct.unpack_from("<II", data, dd + 8)
    if rva == 0:
        return []

    sections = []
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    sec = optional + size_optional
    for index in range(nsec):
        off = sec + index * 40
        va, vsize = struct.unpack_from("<II", data, off + 12)
        raw_size, raw = struct.unpack_from("<II", data, off + 16)
        sections.append((va, max(vsize, raw_size), raw))

    def to_offset(rva_value: int) -> int | None:
        for va, vsize, raw in sections:
            if va <= rva_value < va + vsize:
                return raw + (rva_value - va)
        return None

    names: list[str] = []
    cursor = to_offset(rva)
    if cursor is None:
        return []
    while True:
        entry = data[cursor : cursor + 20]
        if len(entry) < 20 or entry == b"\x00" * 20:
            break
        name_rva = struct.unpack_from("<I", entry, 12)[0]
        if name_rva == 0:
            break
        name_off = to_offset(name_rva)
        if name_off is None:
            break
        end = data.index(b"\x00", name_off)
        names.append(data[name_off:end].decode("ascii", "replace"))
        cursor += 20
    return names


def main(argv: list[str]) -> int:
    for argument in argv:
        path = Path(argument)
        print(f"== {path} ({path.stat().st_size} bytes)")
        for name in imports(path):
            print(f"   {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
