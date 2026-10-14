"""Split a distribution archive into pieces small enough to be sent.

A 235 MiB ZIP does not pass through mail attachments, WeChat or most
file-transfer services.  Splitting it into plain binary chunks lets the
sender use whatever channel they already have, and the receiver puts the
pieces back together with a tool that is already on the machine: ``copy /b``
and ``certutil`` on Windows, ``cat`` and ``shasum`` on macOS.  No 7-Zip, no
Python, no package manager.

Windows gets ``merge-parts.bat`` (ASCII, CRLF, survives any code page);
macOS gets ``merge-parts.command`` (UTF-8, LF, executable so a double-click
opens Terminal).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT / "scripts") not in sys.path:          # pragma: no cover
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import packaging_layout                                    # noqa: E402

#: WeChat and most mail services refuse files above ~100 MB; 90 MiB is safe.
DEFAULT_PART_MIB = 90

CHUNK = 1 << 20


def sha256(path: Path) -> str:
    """Return the SHA256 digest of a file (streamed, so size does not matter)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def human(size: int) -> str:
    """Format a byte count the way Windows Explorer does."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


MERGE_BAT = """@echo off
setlocal enabledelayedexpansion
REM ---------------------------------------------------------------------
REM  EnzyDock - put the downloaded pieces back together.
REM
REM  Double-click this file.  It needs no Python, no 7-Zip and no
REM  PowerShell: "copy" and "certutil" are part of every Windows.
REM
REM  All pieces must sit in this folder, next to this file.
REM ---------------------------------------------------------------------
cd /d "%~dp0"

echo.
echo EnzyDock - merging the downloaded pieces
echo ---------------------------------------------------------------------

set "BASE={base}"
set "WANTED={wanted_hash}"

REM Name the missing pieces instead of only reporting a bad fingerprint:
REM "I forgot to download one of them" is by far the most common mistake.
set "MISSING="
for %%P in ({listed}) do (
    if not exist "%%P" set "MISSING=!MISSING! %%P"
)
if defined MISSING (
    echo.
    echo [FAIL] these pieces are not in this folder:!MISSING!
    echo.
    echo        Every {base}.0NN file must sit next to this script.
    echo        Download the missing ones and run this script again.
    echo.
    pause
    exit /b 1
)

if exist "%BASE%" (
    echo A merged archive already exists; it will be rebuilt.
    del /f /q "%BASE%"
)

echo Joining {count} pieces ...
copy /b {joined} "%BASE%" >nul
if errorlevel 1 (
    echo.
    echo [FAIL] the pieces could not be joined.
    echo        Make sure every {base}.0NN file is in this folder
    echo        and that none of them is still being downloaded.
    echo.
    pause
    exit /b 1
)

for %%A in ("%BASE%") do echo   size : %%~zA bytes
echo.
echo Checking the fingerprint (this proves nothing was lost) ...

set "HASH="
for /f "skip=1 tokens=1" %%H in ('certutil -hashfile "%BASE%" SHA256') do (
    if not defined HASH set "HASH=%%H"
)

if /i "!HASH!"=="%WANTED%" (
    echo.
    echo [ OK ] the archive is complete and unchanged.
) else (
    echo.
    echo [FAIL] the fingerprint does not match.
    echo        got      !HASH!
    echo        expected %WANTED%
    echo        At least one piece is damaged - download it again.
    echo.
    pause
    exit /b 1
)

echo.
echo Next steps
echo ---------------------------------------------------------------------
echo 1. Right-click %BASE% and choose "Extract All...",
echo    then extract to  C:\\EnzyDock
echo    (do NOT run the program from the download or WeChat folder).
echo 2. If you have an antivirus running, add C:\\EnzyDock to its
echo    exceptions before extracting - some of them delete the DLLs
echo    inside _internal and EnzyDock then reports
echo    "Failed to load Python DLL".
echo 3. Open C:\\EnzyDock\\EnzyDock and double-click check-install.bat
echo    to confirm the copy is complete.
echo.
pause
"""

MERGE_SH = """#!/bin/sh
# ---------------------------------------------------------------------
#  EnzyDock - put the downloaded pieces back together (macOS).
#
#  Double-click this file, or run it from Terminal.  It needs no Python
#  and no Homebrew: "cat" and "shasum" ship with macOS.
#
#  All pieces must sit in this folder, next to this file.
# ---------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1

echo
echo "EnzyDock - merging the downloaded pieces"
echo "---------------------------------------------------------------------"

BASE="{base}"
WANTED="{wanted_hash}"

# Name the missing pieces instead of only reporting a bad fingerprint:
# "I forgot to download one of them" is by far the most common mistake.
MISSING=""
for PIECE in {listed}; do
    if [ ! -f "$PIECE" ]; then
        MISSING="$MISSING $PIECE"
    fi
done
if [ -n "$MISSING" ]; then
    echo
    echo "[FAIL] these pieces are not in this folder:$MISSING"
    echo
    echo "       Every {base}.0NN file must sit next to this script."
    echo "       Download the missing ones and run this script again."
    echo
    exit 1
fi

if [ -f "$BASE" ]; then
    echo "A merged archive already exists; it will be rebuilt."
    rm -f "$BASE"
fi

echo "Joining {count} pieces ..."
if ! cat {joined} > "$BASE"; then
    echo
    echo "[FAIL] the pieces could not be joined."
    echo
    exit 1
fi

echo "  size : $(wc -c < "$BASE" | tr -d ' ') bytes"
echo
echo "Checking the fingerprint (this proves nothing was lost) ..."

HASH=$(shasum -a 256 "$BASE" | awk '{{print $1}}')

if [ "$HASH" = "$WANTED" ]; then
    echo
    echo "[ OK ] the archive is complete and unchanged."
else
    echo
    echo "[FAIL] the fingerprint does not match."
    echo "       got      $HASH"
    echo "       expected $WANTED"
    echo "       At least one piece is damaged - download it again."
    echo
    exit 1
fi

echo
echo "Next steps"
echo "---------------------------------------------------------------------"
echo "1. Double-click $BASE to unpack it."
echo "2. Drag EnzyDock.app into your Applications folder."
echo "   (do NOT run it from the Downloads or WeChat folder)."
echo "3. The first time, right-click EnzyDock.app and choose Open, then"
echo "   confirm - macOS quarantines anything downloaded from the internet."
echo "   If it still refuses, run in Terminal:"
echo "       xattr -dr com.apple.quarantine \\"$BASE_DEST\\""
echo "   (use the full path of the app you installed)."
echo "4. Run ./check-install.sh to confirm the copy is complete."
echo
"""

MANIFEST = """EnzyDock {version} - split distribution
{line}

Archive     : {base}
Size        : {total} bytes ({total_human})
SHA256      : {wanted_hash}

Pieces      : {count} x {part_human} ({part} bytes each)
Folder      : {folder}

Fingerprints
------------
{pieces}

How the receiver puts it back together
--------------------------------------
1. download every piece into one empty folder, together with
   {merge} (nothing else is needed - no 7-Zip, no Python);
2. run {merge}: it joins the pieces and prints [ OK ] only when the
   fingerprint matches;
3. unpack the resulting {base} and move {bundle_dir} to {target_hint}
   (never run it from the WeChat or Downloads folder);
4. run {checker} to confirm the copy is complete.

Manual equivalent of step 2:
{manual_join}
{manual_hash}

Packed {stamp}
"""


def split(archive: Path, destination: Path, part_size: int) -> list[Path]:
    """Write the archive as ``<name>.001`` ... and return the pieces."""
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob(archive.name + ".0*"):
        stale.unlink()

    pieces: list[Path] = []
    index = 0
    with archive.open("rb") as source:
        while True:
            index += 1
            target = destination / f"{archive.name}.{index:03d}"
            written = 0
            with target.open("wb") as sink:
                while written < part_size:
                    block = source.read(min(CHUNK, part_size - written))
                    if not block:
                        break
                    sink.write(block)
                    written += len(block)
            if written == 0:
                target.unlink()
                index -= 1
                break
            pieces.append(target)
    return pieces


def write_merge_bat(folder: Path, base: str, pieces: list[Path],
                    wanted_hash: str) -> Path:
    """Write the Windows merge script (ASCII + CRLF, works in any code page)."""
    joined = "+".join(p.name for p in pieces)
    text = MERGE_BAT.format(base=base, count=len(pieces), joined=joined,
                            listed=" ".join(p.name for p in pieces),
                            wanted_hash=wanted_hash)
    path = folder / "merge-parts.bat"
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n")
                     .encode("ascii"))
    return path


def write_merge_sh(folder: Path, base: str, pieces: list[Path],
                   wanted_hash: str) -> Path:
    """Write the macOS merge script (UTF-8, LF, executable).

    ``Path.write_text`` translates ``\\n`` to the *host's* line ending, so on
    a Windows machine this would produce a CRLF file that macOS's ``sh``
    cannot run ("syntax error near unexpected token").  The bytes are written
    directly instead - the file's line endings must not depend on who packed
    the archive.
    """
    text = MERGE_SH.format(base=base, count=len(pieces),
                           joined=" ".join(p.name for p in pieces),
                           listed=" ".join(p.name for p in pieces),
                           wanted_hash=wanted_hash)
    path = folder / "merge-parts.command"
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    # Without the executable bit a double-click opens the editor instead of
    # running it - the one thing that makes this script worth having.
    try:
        path.chmod(0o755)
    except OSError:                                     # pragma: no cover
        pass                    # a Windows checkout cannot store POSIX bits
    return path


def write_merge_script(folder: Path, layout, base: str, pieces: list[Path],
                       wanted_hash: str) -> Path:
    """Write the merge script for ``layout`` (batch on Windows, shell on macOS)."""
    if layout is packaging_layout.MACOS:
        return write_merge_sh(folder, base, pieces, wanted_hash)
    return write_merge_bat(folder, base, pieces, wanted_hash)


def write_manifest(folder: Path, archive: Path, pieces: list[Path],
                   part_size: int, wanted_hash: str, version: str,
                   layout=None) -> Path:
    """Write the human readable record of the split."""
    layout = layout or packaging_layout.WINDOWS
    lines = [f"{sha256(p)}  {p.name}" for p in pieces]
    joined = " ".join(p.name for p in pieces)
    if layout is packaging_layout.MACOS:
        manual_join = f"    cat {joined} > {archive.name}"
    else:
        manual_join = f"    copy /b {'+'.join(p.name for p in pieces)} {archive.name}"
    text = MANIFEST.format(
        version=version,
        line="=" * 58,
        base=archive.name,
        total=archive.stat().st_size,
        total_human=human(archive.stat().st_size),
        wanted_hash=wanted_hash,
        count=len(pieces),
        part_human=human(part_size),
        part=part_size,
        folder=folder.name,
        pieces="\n".join(lines),
        joined=joined,
        merge=layout.merge_script,
        checker=layout.extras[0],
        bundle_dir=layout.bundle_dir,
        target_hint=layout.target_hint,
        manual_join=manual_join,
        manual_hash=f"    {layout.hash_command} {archive.name}",
        stamp=f"{datetime.now():%Y-%m-%d %H:%M:%S}",
    )
    path = folder / "PARTS-MANIFEST.txt"
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n")
                     .encode("utf-8"))
    return path


#: ``EnzyDock-0.1.0-win64.zip`` / ``EnzyDock-0.1.0-macos-arm64.zip``
ARCHIVE_NAME_RE = re.compile(r"^EnzyDock-(?P<version>.+?)-(?P<tag>[a-z0-9]+(?:-[a-z0-9_]+)*)\.zip$")


def version_from_archive(name: str) -> str:
    """Extract the version from a distribution archive name."""
    match = ARCHIVE_NAME_RE.match(name)
    if match:
        return match.group("version")
    # Older / hand-renamed archives: fall back to the first two dashes.
    parts = name.removeprefix("EnzyDock-").removesuffix(".zip").split("-")
    return parts[0] if parts else name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", nargs="?", type=Path, default=None,
                        help="the zip to split (default: newest "
                             "dist/EnzyDock-*.zip)")
    parser.add_argument("--part-size", type=int, default=DEFAULT_PART_MIB,
                        metavar="MIB",
                        help=f"piece size in MiB (default {DEFAULT_PART_MIB})")
    parser.add_argument("--out", type=Path, default=None, metavar="DIR",
                        help="output folder (default: dist/parts)")
    parser.add_argument("--platform", choices=("windows", "macos"),
                        default=None,
                        help="receiver platform (default: guessed from the "
                             "archive name)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    archive = args.archive
    if archive is None:
        found = sorted((PROJECT_ROOT / "dist").glob("EnzyDock-*.zip"))
        if not found:
            print("no dist/EnzyDock-*.zip found - run pack_distribution.py "
                  "first", file=sys.stderr)
            return 1
        archive = found[-1]
    if not archive.is_file():
        print(f"not a file: {archive}", file=sys.stderr)
        return 1

    version = version_from_archive(archive.name)
    layout = packaging_layout.for_system(args.platform) if args.platform else None
    if layout is None:
        # Pick the layout the receiver will need, from the archive name.
        layout = (packaging_layout.MACOS if "macos" in archive.name
                  else packaging_layout.WINDOWS)

    part_size = args.part_size * 1024 * 1024
    folder = args.out or (PROJECT_ROOT / "dist" / "parts")

    print(f"Splitting {archive.name} ({human(archive.stat().st_size)}) "
          f"for {layout.system}")
    pieces = split(archive, folder, part_size)
    wanted_hash = sha256(archive)

    script = write_merge_script(folder, layout, archive.name, pieces,
                                wanted_hash)
    manifest = write_manifest(folder, archive, pieces, part_size,
                              wanted_hash, version, layout)

    print(f"  {len(pieces)} pieces in {folder}:")
    for piece in pieces:
        print(f"    {piece.name}  {human(piece.stat().st_size)}")
    print(f"  {script.name}")
    print(f"  {manifest.name}")
    print(f"\nSend every {archive.name}.0NN file plus {script.name}.")
    print(f"The receiver runs {script.name}; it verifies "
          f"SHA256 {wanted_hash[:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
