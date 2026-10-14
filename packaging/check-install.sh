#!/bin/sh
# ===========================================================================
#  EnzyDock - is this copy complete?  (macOS)
#
#  Double-click this file after unpacking, or run it in Terminal.  It answers
#  one question: did every file survive the transfer?  A half-copied app is
#  the most common reason for "Failed to load Python DLL" style errors, and
#  no download can fix that.
#
#  Deliberately free of external tools: "find", "wc" and "shasum" ship with
#  every macOS, and nothing here needs Python, Homebrew or Xcode.
# ===========================================================================

set -u

cd "$(dirname "$0")" || exit 1

echo
echo "============================================================"
echo "  EnzyDock - is this copy complete?"
echo "============================================================"
echo
echo "  Folder: $(pwd)"
echo

PROBLEMS=0

# The app lives in this folder (or one level down, if it was already moved).
APP="EnzyDock.app"
if [ ! -d "$APP" ]; then
    FOUND=$(find . -maxdepth 2 -name "EnzyDock.app" -type d 2>/dev/null | head -1)
    if [ -n "$FOUND" ]; then
        APP="$FOUND"
    fi
fi

need() {
    if [ -e "$1" ]; then
        echo "  [ OK ] $1"
    else
        echo "  [FAIL] $1 is MISSING"
        PROBLEMS=$((PROBLEMS + 1))
    fi
}

echo "--- the application --------------------------------------------"
need "$APP"
need "$APP/Contents/MacOS/EnzyDock"
need "$APP/Contents/Info.plist"

echo
echo "--- the files a half-copied app always loses -------------------"
need "$APP/Contents/Frameworks/libpython3.12.dylib"
need "$APP/Contents/Frameworks/QtWebEngineCore.framework/Versions/A/QtWebEngineCore"
need "$APP/Contents/Frameworks/QtWebEngineCore.framework/Resources/icudtl.dat"
need "$APP/Contents/Resources/app/resources/viewer/3Dmol.min.js"
need "$APP/Contents/Resources/app/resources/viewer/index.html"
need "$APP/Contents/Resources/app/resources/bin/vina_mac_aarch64"
need "$APP/Contents/Resources/app/resources/licenses/AutoDock-Vina-LICENSE.txt"
need "$APP/Contents/Resources/meeko/data/residue_chem_templates.json"

echo
echo "--- file count -------------------------------------------------"
# Only the payload is counted: an .app also holds symlinked framework
# versions, which would make the number look inflated.
COUNT=$(find "$APP/Contents/Frameworks" "$APP/Contents/Resources" -type f 2>/dev/null | wc -l | tr -d ' ')
echo "  files inside the app : $COUNT"
echo "  a complete copy has    350 or more"
if [ "$COUNT" -lt 350 ]; then
    echo "  [FAIL] files are missing - the copy never finished"
    PROBLEMS=$((PROBLEMS + 1))
else
    echo "  [ OK ] the file count looks right"
fi

echo
echo "--- can the docking engine actually run? -----------------------"
VINA="$APP/Contents/Resources/app/resources/bin/vina_mac_aarch64"
if [ -f "$VINA" ]; then
    if [ ! -x "$VINA" ]; then
        echo "  [warn] the engine lost its executable bit; fixing it"
        chmod +x "$VINA" 2>/dev/null
    fi
    VERSION=$("$VINA" --version 2>&1 | head -1)
    case "$VERSION" in
        *AutoDock\ Vina*)
            echo "  [ OK ] $VERSION"
            ;;
        *)
            echo "  [FAIL] the engine would not start: $VERSION"
            echo "         this is almost always the quarantine flag; run:"
            echo "             xattr -dr com.apple.quarantine \"$APP\""
            PROBLEMS=$((PROBLEMS + 1))
            ;;
    esac
else
    echo "  [FAIL] the bundled docking engine is missing"
    PROBLEMS=$((PROBLEMS + 1))
fi

echo
echo "--- result -----------------------------------------------------"
if [ "$PROBLEMS" -eq 0 ]; then
    echo "  This copy looks complete."
    echo
    echo "  To start: double-click $APP."
    echo "  The FIRST time, right-click it and choose Open - macOS blocks"
    echo "  apps that came from the internet until you confirm once."
else
    echo "  $PROBLEMS problem(s) found - do NOT run EnzyDock yet."
    echo
    echo "  1. Ask for the ZIP again and unpack it somewhere normal,"
    echo "     e.g. your home folder.  Never run it from the Downloads"
    echo "     or WeChat folder."
    echo "  2. If macOS refuses to open the app, remove the quarantine"
    echo "     flag:"
    echo "         xattr -dr com.apple.quarantine \"$(pwd)/$APP\""
    echo "  3. Move EnzyDock.app to /Applications before using it."
    echo "  4. See HOW-TO-RUN-mac.txt for the full checklist."
fi
echo
echo "Press Return to close."
read -r _ 2>/dev/null || true
exit 0
