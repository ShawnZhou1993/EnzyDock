"""The docking engine travels with EnzyDock - these tests keep it that way.

Users are experimental scientists, not system administrators: asking them to
install AutoDock Vina and point the application at it produced a support
question the first time a packaged build was handed to a colleague.  The
engine now ships inside ``app/resources/bin`` and is found automatically, on
every platform EnzyDock supports.

The tests below guard the parts that are easy to break later:

* every platform's binary is present, is the *unmodified* upstream build, and
  the one for the machine running the tests actually starts;
* it needs nothing but the operating system (a static build - verified
  through its PE import table on Windows and its Mach-O load commands on
  macOS - so shipping it cannot create a missing-library problem);
* its licence and the notices travel with it, as Apache 2.0 requires;
* a user can point EnzyDock at another engine and that choice is honoured
  (``DockingEngine.create`` used to drop the settings argument silently, so
  the stored path was never read).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from app.utils.platforms import bundled_vina_name, ensure_executable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = PROJECT_ROOT / "app" / "resources" / "bin"
LICENCE_DIR = PROJECT_ROOT / "app" / "resources" / "licenses"

#: The upstream release these copies were taken from.  EnzyDock redistributes
#: the files byte for byte, so the digests are part of the contract.
UPSTREAM_SHA256 = "e0c4b2715e0c1a74f6e92d0f3be0328ac97542eafbc111e6b1efad897a73cce5"
UPSTREAM_SHA256_MAC_ARM64 = (
    "823c2bbacf26d72183861322345f0a89736aca66c8e81054c66f93af5ad623f1")
UPSTREAM_SHA256_MAC_X86_64 = (
    "9f44ccbb163223613283a75d0be53235d9e63f4da08292cb7196144f9838b7f9")

#: Every engine binary, with the digest the notices must also record.
UPSTREAM_BINARIES = {
    "vina.exe": UPSTREAM_SHA256,
    "vina_mac_aarch64": UPSTREAM_SHA256_MAC_ARM64,
    "vina_mac_x86_64": UPSTREAM_SHA256_MAC_X86_64,
}

#: The binary this machine would use.
NATIVE_NAME = bundled_vina_name()

#: DLLs that every Windows program may import without shipping anything.
SYSTEM_DLLS = {
    "kernel32.dll", "user32.dll", "advapi32.dll", "shell32.dll", "ole32.dll",
    "ws2_32.dll", "bcrypt.dll", "version.dll", "msvcrt.dll", "ntdll.dll",
    "api-ms-win-crt-runtime-l1-1-0.dll",
}

#: dylibs that every macOS ships; anything else would have to travel with us.
SYSTEM_DYLIBS = ("/usr/lib/", "/System/Library/")


def native_binary() -> Path:
    """The engine this platform ships."""
    return BIN_DIR / NATIVE_NAME


def require_native_engine() -> None:
    """Skip a test that has to *execute* the engine when none is shipped.

    EnzyDock builds for Windows and macOS, so those two checkouts always have
    a binary to run.  A Linux checkout has the source but no engine, and the
    suite must stay green there too - the README says the code runs anywhere.
    """
    if not native_binary().is_file():                    # pragma: no cover
        pytest.skip(f"{NATIVE_NAME} is not shipped for {sys.platform}")


def runnable(path: Path) -> Path:
    """``path``, with the executable bit restored if the checkout lost it."""
    ensure_executable(path)
    return path


@pytest.fixture(scope="module")
def pe_imports():
    """Load ``scripts/pe_imports.py`` as a module."""
    script = PROJECT_ROOT / "scripts" / "pe_imports.py"
    spec = importlib.util.spec_from_file_location("pe_imports", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def macho_deps():
    """Load ``scripts/macho_deps.py`` as a module.

    Parsing Mach-O load commands is pure ``struct`` work, so this check runs
    on Windows and Linux too - which is the point: the macOS binaries are
    verified on every machine that has the checkout.
    """
    script = PROJECT_ROOT / "scripts" / "macho_deps.py"
    spec = importlib.util.spec_from_file_location("macho_deps", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the binaries ----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(UPSTREAM_BINARIES))
def test_the_engine_is_shipped_with_the_application(name: str) -> None:
    """Every platform we build for ships its own engine."""
    binary = BIN_DIR / name
    assert binary.is_file(), f"{binary} is missing - docking cannot work at all"
    assert binary.stat().st_size > 500_000, "that file is too small to be Vina"


@pytest.mark.parametrize("name,digest", sorted(UPSTREAM_BINARIES.items()))
def test_the_engine_is_the_unmodified_upstream_build(name: str,
                                                     digest: str) -> None:
    """Apache 2.0 redistribution keeps the notices; an edit would need one."""
    actual = hashlib.sha256((BIN_DIR / name).read_bytes()).hexdigest()
    assert actual == digest, (
        f"the bundled {name} is not the upstream build the notices describe - "
        "update THIRD-PARTY-NOTICES.txt if the binary was replaced")


def test_the_native_engine_actually_starts() -> None:
    """The shipped binary really is Vina, on this machine's architecture."""
    require_native_engine()
    completed = subprocess.run(
        [str(runnable(native_binary())), "--version"],
        capture_output=True, text=True, errors="replace", timeout=60,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    assert "AutoDock Vina" in output, output


def test_the_engine_needs_nothing_but_windows(pe_imports) -> None:
    """A static build cannot reproduce the "missing DLL" support question."""
    if sys.platform != "win32":                        # pragma: no cover
        pytest.skip("a PE import table only exists on Windows")
    imported = {name.lower() for name in pe_imports.imports(BIN_DIR / "vina.exe")}
    assert imported, "no import table could be read"
    unexpected = imported - SYSTEM_DLLS
    assert not unexpected, (
        f"the bundled engine depends on {sorted(unexpected)}; those DLLs would "
        "have to travel with it or the user would see a load error")


def test_the_macos_engines_need_nothing_but_macos(macho_deps) -> None:
    """The macOS counterpart: only Apple's own dylibs may be referenced."""
    for name in ("vina_mac_aarch64", "vina_mac_x86_64"):
        binary = BIN_DIR / name
        if not binary.is_file():                       # pragma: no cover
            pytest.skip(f"{name} is not in this checkout")
        foreign = [dylib
                   for slice_ in macho_deps.read(binary)
                   for dylib in slice_.foreign_dylibs]
        assert not foreign, (
            f"{name} depends on {sorted(set(foreign))}; those libraries would "
            "have to travel with it or the user would see a load error")


def test_the_macos_engines_are_the_right_architecture(macho_deps) -> None:
    """An arm64 Mac cannot run the Intel binary and vice versa."""
    expected = {"vina_mac_aarch64": "arm64", "vina_mac_x86_64": "x86_64"}
    for name, arch in expected.items():
        binary = BIN_DIR / name
        if not binary.is_file():                       # pragma: no cover
            pytest.skip(f"{name} is not in this checkout")
        found = {slice_.arch for slice_ in macho_deps.read(binary)}
        assert found == {arch}, f"{name} is {found}, expected {arch}"


def test_the_engine_speaks_the_flag_the_runner_uses() -> None:
    """VinaEngine drives the CLI; make sure this build knows ``--help``."""
    require_native_engine()
    completed = subprocess.run(
        [str(runnable(native_binary())), "--help"],
        capture_output=True, text=True, errors="replace", timeout=60,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    for flag in ("--center_x", "--size_x", "--exhaustiveness", "--out"):
        assert flag in output, f"{flag} is not supported by the bundled engine"


# --- the licence -----------------------------------------------------------


def test_the_apache_licence_ships_with_the_binary() -> None:
    text = (LICENCE_DIR / "AutoDock-Vina-LICENSE.txt").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text


def test_the_notices_name_the_exact_build_and_the_copyright_holder() -> None:
    text = (LICENCE_DIR / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    assert "AutoDock Vina 1.2.7" in text
    # Every shipped binary must be identified by digest, not just the first.
    for name, digest in UPSTREAM_BINARIES.items():
        assert digest in text, f"the notices do not identify the build of {name}"
    assert "Scripps" in text
    assert "Apache License" in text
    # Publications have to cite the engine; the notice is where users look.
    assert "cite" in text.lower()


# --- discovery -------------------------------------------------------------


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure no machine-specific override changes what is found."""
    monkeypatch.delenv("ENZYDOCK_VINA", raising=False)


def test_the_engine_is_found_without_any_configuration(clean_environment,
                                                       qapp,
                                                       tmp_path: Path,
                                                       monkeypatch) -> None:
    """A fresh installation must work: no settings, no environment, no PATH.

    ``ENZYDOCK_DATA_DIR`` is pointed at a scratch folder because a read-only
    bundle may need the engine copied out to restore its executable bit, and
    that copy must not land in the user's real data directory.
    """
    from app.docking.vina_runner import VinaEngine

    require_native_engine()
    monkeypatch.setenv("ENZYDOCK_DATA_DIR", str(tmp_path / "data"))
    found = VinaEngine().locate()
    assert found is not None, "the bundled engine was not discovered"
    assert found.path.name == NATIVE_NAME
    assert found.path.parent in (BIN_DIR, tmp_path / "data" / "bin")
    assert "bundled" in found.source.lower()
    assert "1.2" in found.version, found.version


def test_the_bundled_engine_wins_over_other_copies(clean_environment, qapp,
                                                   monkeypatch,
                                                   tmp_path: Path) -> None:
    """Whatever else is installed, the shipped copy is what runs by default."""
    decoy = tmp_path / "vina.exe"
    decoy.write_bytes(b"not really vina")
    monkeypatch.setenv("ENZYDOCK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    from app.docking.vina_runner import VinaEngine

    found = VinaEngine().locate()
    assert found is not None and found.path != decoy


def test_a_configured_path_is_honoured(clean_environment, qapp,
                                       tmp_path: Path,
                                       monkeypatch) -> None:
    """Regression: ``create`` used to drop ``settings``, so this never worked."""
    from PySide6.QtCore import QSettings

    from app.docking.base import DockingEngine
    from app.docking.vina_runner import VinaEngine
    from app.utils.settings import AppSettings

    monkeypatch.setenv("ENZYDOCK_DATA_DIR", str(tmp_path / "data"))
    custom = tmp_path / "my_vina.exe"
    custom.write_bytes(b"MZ")                       # only its existence matters
    settings = AppSettings(QSettings(str(tmp_path / "settings.ini"),
                                     QSettings.Format.IniFormat))
    settings.set_value("paths/vina_executable", str(custom))

    engine = DockingEngine.create("AutoDock Vina", settings=settings)
    assert isinstance(engine, VinaEngine)
    found = engine.locate()
    assert found is not None and found.path.stem == custom.stem, (
        "the engine path stored in settings was ignored")


def test_engine_creation_ignores_keywords_a_backend_does_not_know(qapp) -> None:
    """Adding an argument for one engine must not break the others."""
    from app.docking.base import DockingEngine

    class Minimal(DockingEngine):
        name = "__minimal_test_engine"

        def is_available(self) -> tuple[bool, str]:
            return True, "always"

        def run(self, *args: object, **kwargs: object):   # pragma: no cover
            raise NotImplementedError

    DockingEngine.register(Minimal)
    try:
        engine = DockingEngine.create(Minimal.name, settings=object(),
                                      executable="x", nonsense=1)
        assert isinstance(engine, Minimal)
    finally:
        del DockingEngine._registry[Minimal.name]
