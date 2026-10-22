"""Pack a finished EnzyDock build for transfer to another computer.

The bundle is ~600 MB spread over ~540 files.  Copying it through a chat
client (WeChat, DingTalk) or a USB stick loses files more often than people
expect, and PyInstaller's symptom for that is famously unhelpful::

    Failed to load Python DLL '...\\_internal\\python312.dll'.
    LoadLibrary: The specified module could not be found.

``python312.dll`` is in the bundle - it is one of *its* dependencies that went
missing, so nothing can be downloaded to fix it.  The fix is to notice the
loss, and to make the transfer verifiable.

What this script does
---------------------
1. copies the helper files from ``packaging/`` into the bundle
   (``check-install.bat`` / ``check-install.sh`` - works without Python - and
   the matching ``HOW-TO-RUN`` note);
2. counts the files and bytes in the bundle;
3. packs it into a single ``EnzyDock-<version>-<tag>.zip`` whose top-level
   entry is the ``EnzyDock`` folder (``EnzyDock.app`` on macOS), so extracting
   always produces a clean folder instead of spraying files over the download
   directory;
4. writes ``BUNDLE-MANIFEST.txt`` and a ``.sha256`` sidecar with the hashes of
   the archive plus the handful of files that matter, so the receiving side can
   prove the copy is complete.

Which files those are, and what the archive is called, is decided by
:mod:`scripts.packaging_layout`.

Usage::

    python scripts/pack_distribution.py                  # after a build
    python scripts/pack_distribution.py --no-zip         # manifest only
    python scripts/pack_distribution.py --dist dist/EnzyDock --out dist
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:                      # pragma: no cover
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:          # pragma: no cover
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from app import __version__                                # noqa: E402

import packaging_layout                                    # noqa: E402

#: Ignore these while measuring (they are created by the packer itself).
MANIFEST_NAME = "BUNDLE-MANIFEST.txt"

#: Deprecated aliases kept so older callers keep working.
EXTRAS: tuple[str, ...] = packaging_layout.WINDOWS.extras
CRITICAL_FILES: tuple[str, ...] = packaging_layout.WINDOWS.critical_files


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    """Streaming SHA256 so a 200 MB DLL does not land in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def copy_extras(bundle: Path,
                layout: packaging_layout.BundleLayout | None = None) -> list[Path]:
    """Copy the colleague-facing helper files next to the launcher.

    On Windows they go in the bundle root; on macOS there is no root to speak
    of, so they land beside ``EnzyDock.app`` - which is where someone who just
    unzipped the archive is looking anyway.
    """
    layout = layout or packaging_layout.detect_layout(bundle)
    destination_dir = bundle
    if layout is packaging_layout.MACOS:
        destination_dir = bundle.parent

    copied: list[Path] = []
    for name in layout.extras:
        source = PROJECT_ROOT / "packaging" / name
        if not source.is_file():
            print(f"warning: missing packaging/{name}", file=sys.stderr)
            continue
        destination = destination_dir / name
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        if layout is packaging_layout.MACOS and name.endswith(".sh"):
            destination.chmod(0o755)
        copied.append(destination)
    return copied


def measure(bundle: Path) -> tuple[int, int]:
    """Return ``(file count, total bytes)`` for the bundle."""
    count = 0
    total = 0
    for path in bundle.rglob("*"):
        if path.is_file() and path.name != MANIFEST_NAME:
            count += 1
            total += path.stat().st_size
    return count, total


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


def make_zip(bundle: Path, destination: Path, compresslevel: int,
             side_files: tuple[Path, ...] = ()) -> int:
    """Pack the bundle, keeping ``EnzyDock/`` as the single top-level entry.

    ``side_files`` are written at the top level next to it.  On macOS the
    helper scripts and the manifest live *beside* ``EnzyDock.app`` — a file
    dropped inside a package is invisible until somebody right-clicks it —
    so they would otherwise be left behind, and the receiver would be told
    to run a ``check-install.sh`` that never arrived.

    Returns the number of files written.
    """
    files = [p for p in sorted(bundle.rglob("*")) if p.is_file()]
    sides = [p for p in side_files if p.is_file()]
    total = len(files) + len(sides)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=compresslevel) as archive:
        for index, path in enumerate(files, start=1):
            relative = path.relative_to(bundle)
            archive.write(path, f"{bundle.name}/{relative.as_posix()}")
            if index % 100 == 0 or index == len(files):
                print(f"  packed {index}/{len(files)} files")
        for index, path in enumerate(sides, start=1):
            archive.write(path, path.name)
            print(f"  packed {len(files) + index}/{total} files")

    # Verify only *after* the handle is closed.  ``testzip()`` on an archive
    # that is still open for writing reads bytes that are still sitting in the
    # writer's buffer, and reports CRC errors on perfectly good members.
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        written = len(archive.namelist())
    if bad is not None:                                   # pragma: no cover
        raise RuntimeError(f"the archive is corrupt at {bad}")
    if written != total:                                  # pragma: no cover
        raise RuntimeError(f"packed {written} of {total} files")
    return written


def write_manifest(bundle: Path, count: int, total: int,
                   layout: packaging_layout.BundleLayout | None = None) -> Path:
    """Write the record that ships inside the bundle.

    The SHA256 of the archive itself cannot live here - it is written next to
    the archive (``*.zip.sha256``) once packing has finished.
    """
    layout = layout or packaging_layout.detect_layout(bundle)
    checker = layout.extras[0]
    archive_name = f"{layout.archive_stem(__version__)}.zip"

    lines = [
        f"EnzyDock {__version__} - {layout.system} {layout.arch} distribution",
        "=" * 58,
        "",
        f"Packed        : {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Built from    : {bundle}",
        f"Files         : {count} (this manifest not counted)",
        f"Size          : {total} bytes ({human(total)})",
        "",
        "How to verify a copy on the receiving computer",
        "--------------------------------------------",
        f"1. unzip, then double-click {checker} (needs no Python);",
        "2. for the archive itself, compare SHA256 against the number that",
        "   was sent with it:",
        f"       {layout.hash_command} {archive_name}",
    ]
    if layout.notes:
        lines += ["", "Before you start"] + [f"  {line}" for line in layout.notes]
    lines += [
        "",
        "SHA256 of the files a broken copy is usually caught by",
        "-----------------------------------------------------",
    ]
    for relative, path in layout.critical_paths(bundle):
        if path.is_file():
            lines.append(f"  {sha256(path)}  {relative}")
        else:                                              # pragma: no cover
            lines.append(f"  {'-' * 64}  {relative}  (MISSING)")
    lines.append("")
    # Windows: inside the bundle, next to the .exe.  macOS: beside the .app,
    # next to check-install.sh - the .app is a package, and a file dropped
    # inside it is invisible to anyone who has not right-clicked it.
    manifest = (bundle.parent if layout is packaging_layout.MACOS
                else bundle) / MANIFEST_NAME
    # LF, always.  ``Path.write_text`` translates ``\n`` to the *host's* line
    # ending, so on Windows this file would be written with CRLF - and then
    # ``sha256sum -c BUNDLE-MANIFEST.txt`` fails with "could not be read",
    # because the file name it extracts ends in a stray carriage return.
    manifest.write_bytes("\n".join(lines).encode("utf-8"))
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pack a built EnzyDock folder for transfer")
    parser.add_argument("--dist", type=Path, default=None,
                        help="the built bundle (default: dist/EnzyDock on "
                             "Windows, dist/EnzyDock.app on macOS)")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "dist",
                        help="where the zip / manifest are written")
    parser.add_argument("--no-zip", action="store_true",
                        help="only add the helper files and the manifest")
    parser.add_argument("--no-extras", action="store_true",
                        help="do not copy the check-install / HOW-TO-RUN files")
    parser.add_argument("--compresslevel", type=int, default=6,
                        help="zip deflate level 0-9 (default 6)")
    parser.add_argument("--platform", choices=("windows", "macos"),
                        default=None,
                        help="force a layout instead of detecting it")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    default_dist = (PROJECT_ROOT / "dist" /
                    (packaging_layout.MACOS_APP if sys.platform == "darwin"
                     else packaging_layout.WINDOWS.bundle_dir))
    bundle: Path = args.dist or default_dist
    layout = (packaging_layout.for_system(args.platform)
              if args.platform else packaging_layout.detect_layout(bundle))

    launcher = bundle / layout.launcher
    if not launcher.is_file():
        print(f"error: no {layout.launcher} in {bundle} - build first:\n"
              f"    python scripts/build_{'macos' if layout is packaging_layout.MACOS else 'windows'}.py",
              file=sys.stderr)
        return 1

    print(f"Bundling {bundle} ({layout.system} {layout.arch})")
    extras: list[Path] = []
    if not args.no_extras:
        for path in copy_extras(bundle, layout):
            print(f"  added {path.name}")
            extras.append(path)

    # Measure first, then write the manifest, so that the manifest itself ships
    # inside the archive and its own size never enters the count.
    stale = bundle / MANIFEST_NAME
    if stale.exists():
        stale.unlink()
    count, total = measure(bundle)
    manifest = write_manifest(bundle, count, total, layout)

    archive: Path | None = None
    archive_hash: str | None = None
    archive_name = f"{layout.archive_stem(__version__)}.zip"
    if not args.no_zip:
        args.out.mkdir(parents=True, exist_ok=True)
        archive = args.out / archive_name
        if archive.exists():
            archive.unlink()
        print(f"Packing {archive.name} (this takes a minute)")
        # On macOS the helpers and the manifest sit outside the .app, so they
        # are not covered by packing the bundle and have to be handed over
        # explicitly; on Windows they are already inside it.
        outside = tuple(p for p in (*extras, manifest)
                        if p.is_file() and bundle not in p.parents)
        written = make_zip(bundle, archive, args.compresslevel, outside)
        print(f"  verified: {written} members, CRCs all match")
        archive_hash = sha256(archive)
        sidecar = archive.with_suffix(archive.suffix + ".sha256")
        # LF, not the host's line ending - see write_manifest() above.  A CRLF
        # here makes ``sha256sum -c`` fail on the *file name*, which looks
        # exactly like a corrupt download.
        sidecar.write_bytes(f"{archive_hash} *{archive.name}\n".encode("ascii"))

    print()
    print(f"  files     : {count}")
    print(f"  size      : {human(total)}")
    if archive is not None:
        print(f"  archive   : {archive} ({human(archive.stat().st_size)})")
        print(f"  sha256    : {archive_hash}")
    print(f"  manifest  : {manifest}")
    print()
    print(f"Send only the archive.  The receiver unzips it, runs "
          f"{layout.extras[0]},\nand reports back if anything is missing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
