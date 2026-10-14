"""Startup diagnostics ("self test").

Why this module exists
----------------------
EnzyDock depends on a stack that can break in ways the GUI cannot see until
the user presses *Start Docking*:

* RDKit must be able to embed and optimise a 3D structure,
* meeko must be able to load **its JSON data files** (residue templates and
  AutoDock parameters) - a PyInstaller build that forgets ``--collect-data
  meeko`` imports fine and only fails later, deep inside a run,
* SciPy must be present (meeko imports ``scipy.spatial`` at module level),
* AutoDock Vina must be findable on the machine.

:func:`run_checks` exercises each of those for real instead of merely
importing them, so a broken installation is reported *before* the user
invests ten minutes in preparing a protein.

It is used in three places:

1. ``EnzyDock --selftest [--report FILE]`` - for packaged builds, where
   there is no console to read (on macOS run the binary inside
   ``EnzyDock.app/Contents/MacOS/``);
2. *Help -> Run Diagnostics* in the GUI, which shows the same report in a
   dialog the user can copy into a bug report;
3. the test suite (``tests/test_selftest.py``).
"""

from __future__ import annotations

import platform
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from . import __version__
from .utils.logger import get_logger
from .utils.paths import is_frozen, resource, user_data_dir, workspace_root

log = get_logger(__name__)

#: Aspirin - small, well behaved, and familiar to every chemist.
DEFAULT_SMILES = "CC(=O)Oc1ccccc1C(=O)O"

#: Viewer assets that must survive packaging.
REQUIRED_RESOURCES: tuple[str, ...] = (
    "viewer/index.html",
    "viewer/3Dmol.min.js",
)


@dataclass
class Check:
    """The outcome of a single diagnostic."""

    name: str
    ok: bool
    #: One line explaining what was found (version, path, affinity, ...).
    detail: str = ""
    #: What the user should do when ``ok`` is False.
    hint: str = ""

    @property
    def status(self) -> str:
        """``PASS`` or ``FAIL``."""
        return "PASS" if self.ok else "FAIL"

    def line(self) -> str:
        """Format the check for the plain text report."""
        text = f"[{self.status}] {self.name}"
        if self.detail:
            text += f" - {self.detail}"
        if not self.ok and self.hint:
            text += f"\n        -> {self.hint}"
        return text


@dataclass
class SelftestOptions:
    """What :func:`run_checks` should exercise."""

    #: Optional protein structure; enables the receptor and docking checks.
    pdb: Path | None = None
    #: Ligand SMILES used for the ligand check (aspirin by default).
    smiles: str = DEFAULT_SMILES
    #: When ``True`` (and Vina plus a protein are available) run a real docking.
    dock: bool = False

    def __post_init__(self) -> None:
        if self.pdb is not None:
            self.pdb = Path(self.pdb)


ProgressFn = Callable[[int, str], None]


def run_checks(
    options: SelftestOptions | None = None,
    progress: ProgressFn | None = None,
) -> list[Check]:
    """Run every diagnostic and return the results, never raising.

    A failing *check* is a normal outcome; only a bug in this module may
    raise, and even that is contained by :func:`_guarded` for the checks
    that touch third party code.
    """
    opts = options or SelftestOptions()
    checks: list[Check] = []
    steps = _planned_steps(opts)

    def emit(index: int, check: Check) -> None:
        checks.append(check)
        log.info("selftest %s: %s", check.status, check.name)
        if progress is not None:
            progress(int(100 * (index + 1) / steps), check.name)

    index = 0

    def step(name: str, fn: Callable[[], Check]) -> None:
        nonlocal index
        emit(index, _guarded(name, fn))
        index += 1

    step("Application", _check_environment)
    step("Viewer resources", _check_resources)
    step("Writable folders", _check_directories)
    step("RDKit", _check_rdkit)
    step("NumPy / SciPy", _check_numpy_scipy)
    step("PDBQT writer (meeko)", _check_meeko)
    step("Ligand preparation", lambda: _check_ligand(opts.smiles))

    if opts.pdb is not None:
        step("Protein preparation", lambda: _check_receptor(opts.pdb))

    vina = _guarded("AutoDock Vina", _check_vina)
    emit(index, vina)
    index += 1

    if opts.dock and opts.pdb is not None:
        # The result view is as much part of the product as the docking run,
        # so it gets a check of its own - and it reuses the pose that was just
        # produced instead of docking a second time.
        analysed: dict[str, object] = {}
        step("Docking run",
             lambda: _check_docking(opts, vina_ok=vina.ok, holder=analysed))
        step("Interaction analysis", lambda: _check_interactions(analysed))

    return checks


def _guarded(name: str, fn: Callable[[], Check]) -> Check:
    """Run one check, turning an unexpected crash into a FAIL result.

    A diagnostics tool that raises is useless: the user is shown a traceback
    instead of a list of things to fix.
    """
    try:
        return fn()
    except Exception as exc:                             # noqa: BLE001
        log.exception("diagnostic '%s' crashed", name)
        return Check(
            name, False,
            f"the check itself failed: {type(exc).__name__}: {exc}",
            "This looks like a bug in EnzyDock. Please report it together "
            "with the log file (Help -> Open Log File).",
        )


def _planned_steps(options: SelftestOptions) -> int:
    """Number of checks :func:`run_checks` will run (for progress).

    Kept in sync with :func:`run_checks` - the test suite asserts that the
    last progress report is 100 %.
    """
    steps = 4                                  # environment, resources,
                                               # writable folders, RDKit
    steps += 1                                 # numpy / scipy
    steps += 1                                 # meeko
    steps += 1                                 # ligand
    if options.pdb is not None:
        steps += 1                             # receptor
    steps += 1                                 # vina
    if options.dock and options.pdb is not None:
        steps += 1                             # full docking
        steps += 1                             # interaction analysis
    return steps


# -- individual checks ------------------------------------------------------

def _check_environment() -> Check:
    return Check(
        "Application",
        True,
        f"EnzyDock {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.machine()}, "
        f"{'packaged build' if is_frozen() else 'source checkout'}",
    )


def _check_resources() -> Check:
    missing: list[str] = []
    for relative in REQUIRED_RESOURCES:
        path = resource(*relative.split("/"))
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(relative)
    if missing:
        return Check(
            "Viewer resources",
            False,
            f"missing or empty: {', '.join(missing)}",
            "The 3D viewer assets are not installed correctly; reinstall "
            "EnzyDock (the 3D view will stay blank without them).",
        )
    return Check("Viewer resources", True,
                 f"{len(REQUIRED_RESOURCES)} files present")


def _check_directories() -> Check:
    """Confirm the two writable directories really accept files."""
    try:
        for directory in (user_data_dir(), workspace_root()):
            probe = directory / ".enzydock_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
    except OSError as exc:
        return Check(
            "Writable folders",
            False,
            f"{type(exc).__name__}: {exc}",
            "EnzyDock cannot write to its settings or workspace folder. "
            "Check the permissions of your home directory.",
        )
    return Check("Writable folders", True,
                 f"settings: {user_data_dir(create=False)}")


def _check_rdkit() -> Check:
    """Not just an import: really embed and optimise a 3D structure."""
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem import AllChem

        RDLogger.DisableLog("rdApp.*")
        mol = Chem.MolFromSmiles(DEFAULT_SMILES)
        if mol is None:
            return Check("RDKit", False, "could not parse the test molecule",
                         "The chemistry toolkit is broken; reinstall EnzyDock.")
        mol = Chem.AddHs(mol)
        if AllChem.EmbedMolecule(mol, randomSeed=0xf00d) != 0:
            return Check("RDKit", False, "3D embedding failed",
                         "The chemistry toolkit cannot build 3D structures.")
        if AllChem.MMFFOptimizeMolecule(mol) != 0:
            return Check("RDKit", False, "force field optimisation failed",
                         "The chemistry toolkit cannot optimise structures.")
    except Exception as exc:                             # noqa: BLE001
        return Check("RDKit", False, f"{type(exc).__name__}: {exc}",
                     "The chemistry toolkit is missing or broken; "
                     "reinstall EnzyDock.")
    return Check("RDKit", True, f"3D build OK ({mol.GetNumAtoms()} atoms)")


def _check_numpy_scipy() -> Check:
    """SciPy is a hard dependency of meeko (``scipy.spatial``)."""
    try:
        import numpy
        import scipy.spatial                             # noqa: F401
    except Exception as exc:                             # noqa: BLE001
        return Check(
            "NumPy / SciPy", False, f"{type(exc).__name__}: {exc}",
            "A numerical library is missing. It is required to write the "
            "PDBQT files AutoDock Vina reads; reinstall EnzyDock.",
        )
    return Check("NumPy / SciPy", True,
                 f"numpy {numpy.__version__}")


def _check_meeko() -> Check:
    """meeko must import *and* find its JSON data files."""
    try:
        from meeko import (                              # noqa: F401
            MoleculePreparation,
            ResidueChemTemplates,
        )
    except Exception as exc:                             # noqa: BLE001
        return Check(
            "PDBQT writer (meeko)", False,
            f"import failed: {type(exc).__name__}: {exc}",
            "The PDBQT writer is not installed, so proteins and ligands "
            "cannot be prepared for docking. Reinstall EnzyDock.",
        )
    try:
        # This reads data/residue_chem_templates.json and data/params/*.json.
        ResidueChemTemplates.create_from_defaults()
        MoleculePreparation()
    except Exception as exc:                             # noqa: BLE001
        return Check(
            "PDBQT writer (meeko)", False,
            f"data files unreadable: {type(exc).__name__}: {exc}",
            "The PDBQT writer is installed but its parameter files are "
            "missing. Reinstall EnzyDock from a complete build.",
        )
    return Check("PDBQT writer (meeko)", True,
                 "import and parameter files OK")


def _check_ligand(smiles: str) -> Check:
    """Turn a SMILES string into a real PDBQT file."""
    try:
        from .chemistry.preparation import prepare_from_smiles
    except Exception as exc:                             # noqa: BLE001
        return Check("Ligand preparation", False,
                     f"import failed: {type(exc).__name__}: {exc}",
                     "The ligand preparation module is broken.")
    try:
        # The inspection has to happen *inside* the block: once it exits the
        # temporary directory - and the PDBQT with it - is gone.
        with tempfile.TemporaryDirectory(prefix="enzydock-selftest-") as tmp:
            prepared = prepare_from_smiles(smiles, output_dir=Path(tmp))
            if prepared.pdbqt_path is None or not Path(prepared.pdbqt_path).is_file():
                return Check("Ligand preparation", False,
                             "no PDBQT file was written",
                             "The ligand converter produced no output.")
            size = Path(prepared.pdbqt_path).stat().st_size
    except Exception as exc:                             # noqa: BLE001
        return Check(
            "Ligand preparation", False,
            f"{type(exc).__name__}: {getattr(exc, 'message', exc)}",
            "A ligand could not be converted to PDBQT format, so docking "
            "is not possible. Check the details in the log file.",
        )
    return Check("Ligand preparation", True,
                 f"PDBQT written ({size} bytes), "
                 f"{prepared.rotatable_bonds} rotatable bonds, "
                 f"converter: {prepared.converter or 'unknown'}")


def _check_receptor(pdb: Path) -> Check:
    """Clean, protonate and convert a real protein structure."""
    path = Path(pdb)
    if not path.is_file():
        return Check("Protein preparation", False, f"no such file: {path}",
                     "Pass an existing .pdb / .cif file with --selftest-pdb.")
    try:
        from .chemistry.receptor import prepare_receptor
    except Exception as exc:                             # noqa: BLE001
        return Check("Protein preparation", False,
                     f"import failed: {type(exc).__name__}: {exc}",
                     "The protein preparation module is broken.")
    started = time.perf_counter()
    try:
        # Again inside the block - see _check_ligand.
        with tempfile.TemporaryDirectory(prefix="enzydock-selftest-") as tmp:
            prepared = prepare_receptor(path, output_dir=Path(tmp))
            seconds = time.perf_counter() - started
            if prepared.pdbqt_path is None or not Path(prepared.pdbqt_path).is_file():
                return Check("Protein preparation", False,
                             "no PDBQT file was written",
                             "The protein converter produced no output.")
    except Exception as exc:                             # noqa: BLE001
        return Check(
            "Protein preparation", False,
            f"{type(exc).__name__}: {getattr(exc, 'message', exc)}",
            "The protein could not be converted to PDBQT format. Check the "
            "details in the log file.",
        )
    return Check("Protein preparation", True,
                 f"{path.name}: {prepared.residue_count} residues, "
                 f"{prepared.atom_count} atoms in {seconds:.1f}s "
                 f"(converter: {prepared.converter or 'unknown'})")


def _check_vina() -> Check:
    """Locate the docking engine binary (bundled with EnzyDock)."""
    try:
        from .docking.vina_runner import VINA_ENV_VAR, VINA_NAMES
        from .utils.executables import describe_search, find_executable
    except Exception as exc:                             # noqa: BLE001
        return Check("AutoDock Vina", False, f"{type(exc).__name__}: {exc}",
                     "The executable search module is broken.")
    found = find_executable(VINA_NAMES, env_var=VINA_ENV_VAR)
    if found is None:
        return Check(
            "AutoDock Vina", False, "not found",
            "The AutoDock Vina that should ship inside EnzyDock could not be "
            "started. Extract the EnzyDock folder again - an antivirus often "
            "removes this file - or choose another engine in "
            "File > Set Docking Engine. "
            + describe_search(VINA_NAMES, env_var=VINA_ENV_VAR)
            .replace("\n", " "),
        )
    version = found.version or "version unknown"
    return Check("AutoDock Vina", True, f"{found.path} ({version}, {found.source})")


def _check_docking(
    options: SelftestOptions,
    vina_ok: bool,
    holder: dict[str, object] | None = None,
) -> Check:
    """Run the complete pipeline once, exactly like the START button does.

    The result is handed to ``holder`` so that
    :func:`_check_interactions` can analyse the very pose this run produced.
    """
    if not vina_ok:
        return Check("Docking run", False, "skipped (AutoDock Vina not found)",
                     "Install AutoDock Vina to run a docking.")
    assert options.pdb is not None                        # noqa: S101

    try:
        from .docking import engines                     # noqa: F401 - register
        from .docking.base import DockingEngine
        from .docking.docking_config import (
            BoxConfig,
            DockingParameters,
        )
        from .docking.pipeline import DockingRequest, run_docking
        from .chemistry.preparation import PreparationOptions
        from .chemistry.receptor import ReceptorOptions
    except Exception as exc:                             # noqa: BLE001
        return Check("Docking run", False,
                     f"import failed: {type(exc).__name__}: {exc}",
                     "The docking pipeline is broken; reinstall EnzyDock.")

    # The engine is a QObject, so Qt needs an application object.
    _ensure_qapplication()

    params = DockingParameters(exhaustiveness=4, num_poses=3, cpu=1, seed=42)
    try:
        engine = DockingEngine.create(params.engine)
    except Exception as exc:                             # noqa: BLE001
        return Check("Docking run", False, f"{type(exc).__name__}: {exc}",
                     "No docking engine is registered.")

    centre, size = _box_for(options.pdb)
    started = time.perf_counter()
    try:
        result = run_docking(
            DockingRequest(
                receptor_source=options.pdb,
                ligand_source=options.smiles,
                ligand_kind="smiles",
                box=BoxConfig(center=centre, size=size),
                parameters=params,
                receptor_options=ReceptorOptions(),
                ligand_options=PreparationOptions(),
                engine=engine,
                receptor_name=options.pdb.stem,
            )
        )
    except Exception as exc:                             # noqa: BLE001
        return Check(
            "Docking run", False,
            f"{type(exc).__name__}: {getattr(exc, 'message', exc)}",
            "A full docking run failed. See the log file for details.",
        )
    seconds = time.perf_counter() - started
    if holder is not None:
        holder["result"] = result
    if not result.poses:
        return Check("Docking run", False, "no poses were produced",
                     "Docking finished but returned no poses. Try a larger "
                     "binding box or check the log file.")
    return Check(
        "Docking run", True,
        f"{len(result.poses)} poses in {seconds:.1f}s, "
        f"best {result.best_affinity:.2f} kcal/mol" if result.best_affinity
        else f"{len(result.poses)} poses in {seconds:.1f}s",
    )


def _check_interactions(holder: dict[str, object]) -> Check:
    """Analyse the pose the docking check just produced (Step 7).

    This runs exactly the code behind the result view, so a build that cannot
    analyse a pose fails here instead of quietly showing a lonely molecule
    with no receptor and no contacts.
    """
    try:
        from .visualization.interactions import describe
        from .visualization.viewer import build_scene
    except Exception as exc:                             # noqa: BLE001
        return Check("Interaction analysis", False,
                     f"import failed: {type(exc).__name__}: {exc}",
                     "The 3D result view cannot work; reinstall EnzyDock.")

    result = holder.get("result")
    if result is None:
        return Check("Interaction analysis", False,
                     "skipped (the docking run produced no result)",
                     "Fix the docking run above first.")
    pose = result.best_pose
    workspace = result.workspace
    if pose is None or pose.pose_file is None or workspace is None:
        return Check("Interaction analysis", False,
                     "no pose file to analyse",
                     "The run left no pose behind, so the 3D view has nothing "
                     "to show.")

    prepared = Path(workspace) / "prepared"
    receptor = next(
        (prepared / name for name in ("receptor_clean.pdb",
                                      "receptor_protonated.pdb")
         if (prepared / name).is_file()),
        None,
    )
    started = time.perf_counter()
    try:
        scene = build_scene(receptor, pose.pose_file, result.box)
    except Exception as exc:                             # noqa: BLE001
        return Check("Interaction analysis", False,
                     f"{type(exc).__name__}: {exc}",
                     "The 3D scene could not be built from this run.")
    milliseconds = (time.perf_counter() - started) * 1000

    if not scene.has_protein:
        return Check("Interaction analysis", False,
                     "the receptor did not reach the 3D scene",
                     "The result view would show the docked pose alone. Check "
                     f"that {prepared / 'receptor_clean.pdb'} exists.")
    return Check(
        "Interaction analysis", True,
        f"receptor + pose analysed in {milliseconds:.0f} ms, "
        f"{len(scene.pocket_residues)} pocket residues, "
        f"{describe(scene.interactions)}",
    )


def _ensure_qapplication() -> None:
    """Create a headless QApplication if the process does not have one yet."""
    from PySide6.QtCore import QCoreApplication

    if QCoreApplication.instance() is not None:
        return
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication([])


def _box_for(pdb: Path) -> tuple[tuple[float, float, float],
                                 tuple[float, float, float]]:
    """A 22 A box around the structure's centroid (good enough for a probe)."""
    try:
        from .chemistry.protein import read_atoms

        atoms = read_atoms(pdb)
        if atoms:
            count = len(atoms)
            centre = (
                sum(a.x for a in atoms) / count,
                sum(a.y for a in atoms) / count,
                sum(a.z for a in atoms) / count,
            )
            return centre, (22.0, 22.0, 22.0)
    except Exception as exc:                             # noqa: BLE001
        log.debug("selftest box detection failed: %s", exc)
    return (0.0, 0.0, 0.0), (22.0, 22.0, 22.0)


# -- reporting --------------------------------------------------------------

def format_report(checks: Sequence[Check], title: str = "EnzyDock diagnostics") -> str:
    """Render :class:`Check` results as plain text (log- and email-friendly)."""
    failed = [c for c in checks if not c.ok]
    lines = [
        title,
        "=" * len(title),
        "",
        f"EnzyDock {__version__} on {platform.system()} {platform.machine()}",
        f"Python {platform.python_version()} - "
        f"{'packaged build' if is_frozen() else 'source checkout'}",
        "",
    ]
    for check in checks:
        lines.append(check.line())
    lines += [
        "",
        f"{len(checks) - len(failed)}/{len(checks)} checks passed.",
    ]
    if failed:
        lines.append("")
        lines.append("Failed: " + ", ".join(c.name for c in failed))
    return "\n".join(lines)


def write_report(checks: Sequence[Check], path: str | Path) -> Path:
    """Write the report to ``path`` and return it (for packaged builds)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_report(checks), encoding="utf-8")
    return target


@dataclass
class SelftestSummary:
    """Convenience wrapper returned by :func:`run_selftest`."""

    checks: list[Check] = field(default_factory=list)
    report_path: Path | None = None

    @property
    def ok(self) -> bool:
        """``True`` when every check passed."""
        return all(check.ok for check in self.checks)

    @property
    def report(self) -> str:
        """The formatted report."""
        return format_report(self.checks)


def run_selftest(
    options: SelftestOptions | None = None,
    report_path: str | Path | None = None,
    progress: ProgressFn | None = None,
) -> SelftestSummary:
    """Run the diagnostics, optionally writing the report, and summarise."""
    checks = run_checks(options, progress=progress)
    written = write_report(checks, report_path) if report_path else None
    return SelftestSummary(checks=checks, report_path=written)


__all__ = [
    "DEFAULT_SMILES",
    "Check",
    "SelftestOptions",
    "SelftestSummary",
    "format_report",
    "run_checks",
    "run_selftest",
    "write_report",
]
