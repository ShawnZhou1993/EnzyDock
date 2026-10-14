#!/bin/sh
# ===========================================================================
#  Build EnzyDock for macOS, end to end.
#
#  Usage (from the project root, on a Mac):
#
#      sh scripts/build_mac.sh
#      sh scripts/build_mac.sh --arch arm64 --zip
#
#  It creates a virtual environment if needed, installs the runtime and
#  build dependencies, generates the icon, builds EnzyDock.app, verifies the
#  result with a real docking run, and (optionally) makes the transfer ZIP.
#
#  Everything it runs is idempotent - re-running after a `git pull` is the
#  normal way to produce a new build.
# ===========================================================================

set -eu

cd "$(dirname "$0")/.." || exit 1

ARCH=""
ZIP=""
# A ``for arg in "$@"`` loop cannot consume the argument after ``--arch``:
# ``shift`` does not move the loop's cursor.  Walk the list with ``while``.
while [ "$#" -gt 0 ]; do
    case "$1" in
        --arch=*) ARCH="${1#--arch=}" ;;
        --arch)   ARCH="${2:-}"; shift ;;
        --zip)    ZIP="--zip" ;;
        *)        ;;
    esac
    shift
done

PYTHON="${PYTHON:-python3}"
VENV=".venv"

echo "============================================================"
echo "  EnzyDock - macOS build"
echo "============================================================"
echo "  project : $(pwd)"
echo "  python  : $($PYTHON --version 2>&1)"
echo "  machine : $(uname -m)"
echo

# --- 1. virtual environment ------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    echo "--- creating $VENV"
    "$PYTHON" -m venv "$VENV"
fi

VPY="$VENV/bin/python"
"$VPY" -m pip install --quiet --upgrade pip

echo "--- installing runtime dependencies (this takes a few minutes)"
"$VPY" -m pip install --quiet -r requirements.txt

echo "--- installing build dependencies"
"$VPY" -m pip install --quiet -r requirements-dev.txt

# --- 2. icon --------------------------------------------------------------
echo "--- generating the application icon"
QT_QPA_PLATFORM=offscreen "$VPY" scripts/make_icons.py

# --- 3. tests (fast suite only; the e2e marker needs a real docking run) ---
echo "--- running the test suite (excluding the slow end-to-end tests)"
QT_QPA_PLATFORM=offscreen "$VPY" -m pytest -q -m "not e2e"

# --- 4. build -------------------------------------------------------------
echo
echo "--- building EnzyDock.app"
# shellcheck disable=SC2086
"$VPY" scripts/build_macos.py --arch "${ARCH:-universal2}" $ZIP

# --- 5. verify the bundle -------------------------------------------------
echo
echo "--- verifying the bundle (docks a real molecule inside the .app)"
if [ -n "${SKIP_GUI:-}" ]; then
    "$VPY" scripts/verify_bundle.py --dist dist/EnzyDock.app --skip-gui
else
    "$VPY" scripts/verify_bundle.py --dist dist/EnzyDock.app
fi

echo
echo "============================================================"
echo "  Done."
echo "  Application : dist/EnzyDock.app"
echo "  To install  : drag it into /Applications"
echo "  To send it  : dist/EnzyDock-*-macos-*.zip (+ .sha256)"
echo "============================================================"
