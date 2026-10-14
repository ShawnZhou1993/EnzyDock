"""Run the macOS packaging chain on a synthetic ``.app``.

PyInstaller cannot cross-compile, so a real ``EnzyDock.app`` only exists on a
Mac.  Everything *around* that build is plain Python, though, and it is easy
to get wrong in ways that only show up on the build machine:

* the macOS layout used to spell its paths relative to the *archive* root
  while Windows used the bundle root, so the packer looked for
  ``EnzyDock.app/EnzyDock.app/Contents/MacOS/EnzyDock`` and refused to pack;
* the helper scripts and the manifest are written *beside* the ``.app`` (a
  file inside a package is invisible), and were therefore left out of the
  archive - the receiver was told to run a ``check-install.sh`` that never
  arrived;
* the merge script must be LF even when it is produced on Windows.

These tests build a small ``.app`` with the right shape and run the real
packer, splitter and verifier against it, so all of that is caught here.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    """Import a script from ``scripts/`` by file path."""
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
def layouts():
    return load_script("packaging_layout")


@pytest.fixture(scope="module")
def packer():
    return load_script("pack_distribution")


@pytest.fixture(scope="module")
def splitter():
    return load_script("split_distribution")


@pytest.fixture(scope="module")
def verifier():
    return load_script("verify_bundle")


def _mach_o_stub(size: int = 32) -> bytes:
    """Enough of a Mach-O header to satisfy the verifier's file checks."""
    return b"\xcf\xfa\xed\xfe" * size


def build_fake_app(root: Path) -> Path:
    """Create ``root/EnzyDock.app`` with the shape PyInstaller produces."""
    app = root / "EnzyDock.app"
    macos = app / "Contents" / "MacOS"
    frameworks = app / "Contents" / "Frameworks"
    resources = app / "Contents" / "Resources"
    for folder in (macos, frameworks, resources):
        folder.mkdir(parents=True)

    (macos / "EnzyDock").write_bytes(_mach_o_stub())
    (app / "Contents" / "Info.plist").write_text(
        '<plist version="1.0"><dict>'
        "<key>CFBundleIdentifier</key><string>com.enzydock.app</string>"
        "</dict></plist>\n", encoding="utf-8")

    # --- Contents/Frameworks: the Python payload ---------------------------
    (frameworks / "libpython3.12.dylib").write_bytes(_mach_o_stub())
    qt = frameworks / "QtWebEngineCore.framework" / "Versions" / "A"
    qt.mkdir(parents=True)
    (qt / "QtWebEngineCore").write_bytes(_mach_o_stub())

    for relative in ("meeko/data/residue_chem_templates.json",
                     "meeko/data/params/ad4_types.json",
                     "meeko/data/template_charges.json",
                     "scipy/spatial/_ckdtree.cpython-312-darwin.so"):
        target = frameworks / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8") \
            if relative.endswith(".json") else target.write_bytes(_mach_o_stub())

    # The verifier wants at least ``min_payload_files``; pad with stubs.
    for index in range(400):
        (frameworks / f"module_{index:03d}.so").write_bytes(_mach_o_stub(1))

    # --- Contents/Resources: the --add-data payload ------------------------
    viewer = resources / "app" / "resources" / "viewer"
    viewer.mkdir(parents=True)
    (viewer / "3Dmol.min.js").write_text("// 3Dmol.js\n", encoding="utf-8")
    (viewer / "index.html").write_text("<html></html>\n", encoding="utf-8")
    bin_dir = resources / "app" / "resources" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vina_mac_aarch64").write_bytes(_mach_o_stub())
    licences = resources / "app" / "resources" / "licenses"
    licences.mkdir(parents=True)
    (licences / "THIRD-PARTY-NOTICES.txt").write_text("notices\n",
                                                      encoding="utf-8")
    return app


@pytest.fixture(scope="module")
def fake_app(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_fake_app(tmp_path_factory.mktemp("macbundle"))


# --- packing ---------------------------------------------------------------


def test_the_packer_accepts_a_real_app_layout(packer, fake_app,
                                              tmp_path: Path) -> None:
    """Regression: the launcher path used to be doubled up."""
    code = packer.main(["--platform", "macos", "--dist", str(fake_app),
                        "--out", str(tmp_path), "--no-zip"])
    assert code == 0


def test_the_helpers_land_beside_the_app(packer, fake_app) -> None:
    """Inside a ``.app`` they would be invisible to the receiver."""
    packer.copy_extras(fake_app, packer.packaging_layout.MACOS)
    for name in ("check-install.sh", "HOW-TO-RUN-mac.txt"):
        assert (fake_app.parent / name).is_file(), f"{name} is not beside the app"
        assert not (fake_app / name).exists(), f"{name} was put inside the app"


def test_the_manifest_lands_beside_the_app(packer, fake_app) -> None:
    packer.write_manifest(fake_app, 1, 1, packer.packaging_layout.MACOS)
    assert (fake_app.parent / packer.MANIFEST_NAME).is_file()


def test_everything_the_receiver_is_told_to_run_is_in_the_archive(
        packer, fake_app, tmp_path: Path) -> None:
    """Regression: the helpers sat outside the ``.app`` and were left out."""
    packer.main(["--platform", "macos", "--dist", str(fake_app),
                 "--out", str(tmp_path)])
    archive = tmp_path / "EnzyDock-0.1.0-macos-arm64.zip"
    assert archive.is_file()

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()

    for member in ("check-install.sh", "HOW-TO-RUN-mac.txt",
                   "BUNDLE-MANIFEST.txt",
                   "EnzyDock.app/Contents/MacOS/EnzyDock",
                   "EnzyDock.app/Contents/Resources/app/resources/bin/"
                   "vina_mac_aarch64"):
        assert member in names, f"{member} is missing from the archive"

    # Two top-level *files* plus the app: nothing sprayed over the desktop.
    tops = {name.split("/")[0] for name in names}
    assert tops == {"EnzyDock.app", "check-install.sh", "HOW-TO-RUN-mac.txt",
                    "BUNDLE-MANIFEST.txt"}


def test_the_archive_is_verified_not_just_written(packer, fake_app,
                                                  tmp_path: Path) -> None:
    packer.main(["--platform", "macos", "--dist", str(fake_app),
                 "--out", str(tmp_path)])
    archive = tmp_path / "EnzyDock-0.1.0-macos-arm64.zip"
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
    assert (archive.with_suffix(archive.suffix + ".sha256")).is_file()


# --- splitting -------------------------------------------------------------


def test_the_mac_merge_script_survives_being_written_on_windows(
        splitter, tmp_path: Path) -> None:
    archive = tmp_path / "EnzyDock-0.1.0-macos-arm64.zip"
    archive.write_bytes(b"x" * 5000)
    # ``split`` takes the part size in *bytes* (the CLI flag is MiB).
    pieces = splitter.split(archive, tmp_path / "parts", 2048)
    script = splitter.write_merge_sh(tmp_path / "parts", archive.name, pieces,
                                     splitter.sha256(archive))
    data = script.read_bytes()
    assert b"\r" not in data, "CRLF would make /bin/sh report a syntax error"
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.startswith(b"#!/bin/sh")


# --- the verifier ----------------------------------------------------------


def test_the_verifier_accepts_the_app_itself(verifier, layouts,
                                             fake_app) -> None:
    """``--dist`` points at the bundle, not at the folder holding it."""
    result = verifier.Result()
    verifier.check_files(fake_app, layouts.MACOS, result)
    failures = [text for ok, text in result.entries if not ok]
    assert not failures, f"the verifier rejected a sound .app: {failures}"


def test_the_verifier_looks_for_helpers_beside_the_app(verifier, layouts,
                                                       fake_app) -> None:
    """They are not inside the package, so the search must not be either."""
    for name in layouts.MACOS.extras:
        (fake_app.parent / name).write_text("x", encoding="utf-8")
    result = verifier.Result()
    verifier.check_files(fake_app, layouts.MACOS, result)
    for name in layouts.MACOS.extras:
        assert any(name in text and ok for ok, text in result.entries), (
            f"the verifier did not find {name}")
