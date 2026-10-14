"""Ligand preparation: 2D input -> 3D, energy-minimised, docking-ready PDBQT.

This module implements the pipeline sketched in the project brief::

    SMILES / file
        -> parse                      (app.chemistry.ligand, Step 2)
        -> pick the relevant fragment (optional)
        -> add hydrogens
        -> embed 3D coordinates
        -> force-field optimisation
        -> AutoDock typing + charges  (app.chemistry.conversion)
        -> ligand.pdbqt

Everything is side-effect free with respect to the user's files: results are
written into a run workspace, never next to the original input.

The GUI stays unaware of the details - it passes an RDKit molecule plus an
options object and receives a :class:`PreparedLigand`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem, rdMolDescriptors

from ..utils.logger import get_logger
from ..utils.paths import ensure_dir
from .conversion import ConversionError, ProgressCallback, count_torsions, get_converter
from .ligand import (
    LigandError,
    LigandSummary,
    molecule_from_file,
    molecule_from_smiles,
    summarize_molecule,
)

log = get_logger(__name__)
RDLogger.DisableLog("rdApp.*")

#: Force fields EnzyDock can use for geometry optimisation.
FORCE_FIELDS = ("MMFF94", "UFF", "none")


class LigandPreparationError(LigandError):
    """Raised when a ligand cannot be turned into a docking-ready structure.

    Subclasses :class:`~app.chemistry.ligand.LigandError` so the GUI can show
    every ligand problem through the same dialog.
    """


@dataclass
class PreparationOptions:
    """Knobs for the ligand preparation pipeline.

    The defaults are deliberately conservative: they reproduce what a careful
    modeller would do by hand, and every field maps onto something a user can
    understand if it needs changing.
    """

    add_hydrogens: bool = True
    #: ``"MMFF94"`` (falls back to UFF when MMFF parameters are missing),
    #: ``"UFF"`` or ``"none"`` to skip optimisation entirely.
    force_field: str = "MMFF94"
    max_iterations: int = 2000
    random_seed: int = 42
    #: Conformers to generate; the lowest-energy one is kept. ``1`` is enough
    #: because Vina samples torsions itself during docking.
    num_conformers: int = 1
    #: Keep 3D coordinates that came with an input file (crystal / docked pose)
    #: instead of re-embedding them.
    keep_input_3d: bool = True
    #: Still run a short minimisation on file-provided 3D coordinates.
    optimize_input_3d: bool = False
    #: When a file contains salts or solvent, dock only the largest fragment.
    keep_largest_fragment: bool = False
    #: PDBQT backend: ``None`` picks the best installed one.
    converter: str | None = None
    #: Merge non-polar hydrogens onto their parent (AutoDock convention).
    merge_nonpolar_hydrogens: bool = True

    def __post_init__(self) -> None:
        if self.force_field not in FORCE_FIELDS:
            raise ValueError(
                f"Unknown force field {self.force_field!r}; "
                f"choose one of {', '.join(FORCE_FIELDS)}."
            )
        if self.num_conformers < 1:
            raise ValueError("num_conformers must be at least 1.")
        if self.max_iterations < 0:
            raise ValueError("max_iterations cannot be negative.")


@dataclass
class PreparedLigand:
    """Everything downstream (docking, report, viewer) needs about a ligand."""

    name: str = "ligand"
    smiles: str = ""
    #: Optimised molecule *with* hydrogens and a 3D conformer.
    mol: Chem.Mol | None = None
    summary: LigandSummary | None = None
    pdbqt_path: Path | None = None
    mol_path: Path | None = None           # 3D structure with hydrogens (.sdf)
    pdbqt_text: str = ""
    rotatable_bonds: int = 0               # torsions declared in the PDBQT
    force_field_used: str = "none"
    energy: float | None = None            # kcal/mol, final force-field energy
    converter: str = ""
    warnings: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def is_ready(self) -> bool:
        """``True`` when a PDBQT file was produced and exists on disk."""
        return self.pdbqt_path is not None and Path(self.pdbqt_path).is_file()

    def to_rows(self) -> list[tuple[str, str]]:
        """``(label, value)`` rows for the ligand panel / report.

        The summary's own "Rotatable bonds" row is replaced by the torsion
        count from the PDBQT file: that is the number of bonds AutoDock will
        actually sample, and showing both numbers side by side only confuses
        users (RDKit's strict count is usually smaller).
        """
        rows: list[tuple[str, str]] = []
        if self.summary is not None:
            rows.extend((label, value) for label, value in self.summary.to_rows()
                        if label != "Rotatable bonds")
        rows.append(("Rotatable bonds (docking)", str(self.rotatable_bonds)))
        rows.append(("Force field", self.force_field_used))
        if self.energy is not None:
            rows.append(("Minimised energy", f"{self.energy:.2f} kcal/mol"))
        rows.append(("PDBQT writer", self.converter or "-"))
        rows.append(("Docking input",
                     self.pdbqt_path.name if self.pdbqt_path else "not prepared"))
        return rows


# --------------------------------------------------------------------- steps
def has_3d(mol: Chem.Mol) -> bool:
    """Return ``True`` when ``mol`` already carries a 3D conformer."""
    return bool(mol is not None and mol.GetNumConformers()) and mol.GetConformer().Is3D()


def add_hydrogens(mol: Chem.Mol, with_coords: bool = True) -> Chem.Mol:
    """Return a copy of ``mol`` with explicit hydrogens.

    ``with_coords`` matters for molecules that already have 3D coordinates:
    RDKit then places the new hydrogens in sensible positions instead of
    leaving them at the origin.
    """
    try:
        return Chem.AddHs(mol, addCoords=with_coords)
    except Exception as exc:                             # pragma: no cover
        raise LigandPreparationError(
            "Hydrogens could not be added to this ligand.",
            f"Chem.AddHs failed: {type(exc).__name__}: {exc}",
        ) from exc


def select_fragment(mol: Chem.Mol, keep_largest: bool) -> tuple[Chem.Mol, bool]:
    """Handle multi-fragment molecules (salts, solvent, co-crystallised ions).

    Returns ``(molecule, was_split)``.  When ``keep_largest`` is ``False`` the
    molecule is returned unchanged and the caller is expected to warn instead.
    """
    try:
        fragments = Chem.GetMolFrags(mol, asMols=True, sanitizeFrags=True)
    except Exception:                                    # pragma: no cover
        return mol, False
    if len(fragments) <= 1:
        return mol, False
    if not keep_largest:
        return mol, True
    best = max(fragments, key=lambda m: m.GetNumHeavyAtoms())
    log.info("kept the largest of %d fragments (%d heavy atoms)",
             len(fragments), best.GetNumHeavyAtoms())
    return best, True


def embed_conformer(mol: Chem.Mol, options: PreparationOptions) -> Chem.Mol:
    """Generate 3D coordinate(s) for ``mol``.

    A first attempt uses RDKit's distance-geometry embedding; if that fails
    (common for macrocycles and very strained systems) a second attempt starts
    from random coordinates, which is slower but far more forgiving.

    When ``options.num_conformers`` is greater than one, several conformers are
    embedded at once; the caller is expected to score them afterwards.

    Raises
    ------
    LigandPreparationError
        When neither attempt produces coordinates.
    """
    params = AllChem.ETKDGv3()
    params.randomSeed = options.random_seed
    params.useSmallRingTorsions = True
    count = max(1, options.num_conformers)

    try:
        if count == 1:
            status = AllChem.EmbedMolecule(mol, params)
            produced = 1 if status == 0 else 0
        else:
            produced = len(AllChem.EmbedMultipleConfs(mol, count, params))
    except Exception as exc:
        raise LigandPreparationError(
            "Unable to generate a 3D structure for this ligand.",
            f"RDKit embedding raised {type(exc).__name__}: {exc}",
        ) from exc

    if not produced:
        log.debug("ETKDG embedding failed, retrying from random coordinates")
        params.useRandomCoords = True
        try:
            if count == 1:
                status = AllChem.EmbedMolecule(mol, params)
                produced = 1 if status == 0 else 0
            else:
                produced = len(AllChem.EmbedMultipleConfs(mol, count, params))
        except Exception as exc:                     # pragma: no cover
            raise LigandPreparationError(
                "Unable to generate a 3D structure for this ligand.",
                f"RDKit embedding raised {type(exc).__name__}: {exc}",
            ) from exc

    if not produced:
        raise LigandPreparationError(
            "Unable to generate a 3D structure for this ligand. "
            "Please check whether the SMILES is chemically valid - very large "
            "rings, impossible valences or unsupported elements usually cause "
            "this.",
            "RDKit embedding returned -1 (ETKDGv3 and random coordinates)",
        )
    return mol


def optimize_conformer(
    mol: Chem.Mol, options: PreparationOptions
) -> tuple[Chem.Mol, str, float | None, bool]:
    """Energy-minimise ``mol`` in place and report what happened.

    Returns ``(mol, force_field_used, energy, converged)``.  MMFF94 is tried
    first; molecules containing elements MMFF cannot parameterise fall back to
    UFF automatically.  A non-converged minimisation is *not* an error - it is
    reported so the caller can warn the user.
    """
    if options.force_field == "none":
        return mol, "none", None, True

    converged = True
    energy: float | None = None
    used = options.force_field

    try:
        if options.force_field == "MMFF94":
            properties = AllChem.MMFFGetMoleculeProperties(mol)
            if properties is not None:
                force_field = AllChem.MMFFGetMoleculeForceField(
                    mol, properties, confId=0
                )
                force_field.Initialize()
                result = force_field.Minimize(maxIts=options.max_iterations)
                converged = result == 0
                energy = float(force_field.CalcEnergy())
                return mol, "MMFF94", energy, converged
            log.info("MMFF94 parameters unavailable, falling back to UFF")
            used = "UFF"

        if used == "UFF":
            force_field = AllChem.UFFGetMoleculeForceField(mol, confId=0)
            if force_field is None:                      # pragma: no cover
                return mol, "none", None, True
            force_field.Initialize()
            result = force_field.Minimize(maxIts=options.max_iterations)
            converged = result == 0
            energy = float(force_field.CalcEnergy())
            return mol, "UFF", energy, converged
    except Exception as exc:                             # pragma: no cover
        raise LigandPreparationError(
            "The 3D structure of this ligand could not be optimised.",
            f"force-field minimisation failed: {type(exc).__name__}: {exc}",
        ) from exc

    return mol, "none", energy, converged


def _force_field_energy(mol: Chem.Mol, force_field: str) -> float | None:
    """Recompute the single-point energy of an already minimised molecule."""
    try:
        if force_field == "MMFF94":
            properties = AllChem.MMFFGetMoleculeProperties(mol)
            if properties is None:
                return None
            return float(AllChem.MMFFGetMoleculeForceField(
                mol, properties, confId=0).CalcEnergy())
        if force_field == "UFF":
            ff = AllChem.UFFGetMoleculeForceField(mol, confId=0)
            return None if ff is None else float(ff.CalcEnergy())
    except Exception:                                    # pragma: no cover
        return None
    return None


@dataclass
class BuiltLigand3D:
    """Outcome of the geometry stage (hydrogens + embedding + optimisation)."""

    mol: Chem.Mol
    force_field: str = "none"
    energy: float | None = None
    converged: bool = True
    #: ``True`` when coordinates that came with the input file were kept.
    reused_input_coordinates: bool = False
    warnings: list[str] = field(default_factory=list)


def build_3d(
    mol: Chem.Mol,
    options: PreparationOptions | None = None,
    progress: ProgressCallback | None = None,
) -> BuiltLigand3D:
    """Add hydrogens, embed 3D coordinates and energy-minimise ``mol``.

    This is the reusable half of :func:`prepare_ligand`: the *Generate 3D*
    button uses it on its own (so the user can look at the conformer before
    docking), while docking preparation runs it and then converts to PDBQT.

    Raises
    ------
    LigandPreparationError
        When no 3D structure can be built.
    """
    options = options or PreparationOptions()

    def report(percent: int, message: str) -> None:
        log.debug("[3D] %d%% %s", percent, message)
        if progress is not None:
            progress(percent, message)

    if mol is None:
        raise LigandPreparationError("There is no ligand to build.",
                                     "build_3d received None")

    report(5, "Inspecting ligand ...")
    working = Chem.Mol(mol)                              # never mutate the input
    warnings: list[str] = []

    working, split = select_fragment(working, options.keep_largest_fragment)
    if split:
        warnings.append(
            "Only the largest fragment of this ligand was kept."
            if options.keep_largest_fragment else
            "This ligand contains more than one molecule (for example a salt or "
            "a solvent molecule), which can make docking results hard to interpret."
        )

    input_has_3d = has_3d(working)
    if options.add_hydrogens:
        report(15, "Adding hydrogens ...")
        working = add_hydrogens(working, with_coords=input_has_3d)

    reuse = input_has_3d and options.keep_input_3d and not options.optimize_input_3d
    if input_has_3d and options.keep_input_3d:
        log.info("keeping the 3D coordinates supplied with the ligand file")
    else:
        report(30, "Generating a 3D structure ...")
        working = embed_conformer(working, options)

    if reuse:
        report(55, "Keeping the supplied 3D coordinates ...")
        used, energy, converged = "none", None, True
    elif options.num_conformers > 1:
        report(55, "Optimising conformations ...")
        working, used, energy = optimize_all_conformers(working, options)
        converged = True
    else:
        report(55, "Optimising the 3D structure ...")
        working, used, energy, converged = optimize_conformer(working, options)
        if not converged:
            warnings.append(
                "The geometry optimisation did not fully converge; the structure "
                "is usable but slightly less relaxed than ideal."
            )

    if not has_3d(working):                              # pragma: no cover
        raise LigandPreparationError(
            "Unable to generate a 3D structure for this ligand.",
            "molecule still has no 3D conformer after embedding",
        )

    return BuiltLigand3D(
        mol=working,
        force_field=used,
        energy=energy,
        converged=converged,
        reused_input_coordinates=reuse,
        warnings=warnings,
    )


# ------------------------------------------------------------------ pipeline
def prepare_ligand(
    mol: Chem.Mol,
    name: str = "ligand",
    output_dir: str | Path | None = None,
    options: PreparationOptions | None = None,
    progress: ProgressCallback | None = None,
) -> PreparedLigand:
    """Turn a parsed molecule into a docking-ready PDBQT file.

    Parameters
    ----------
    mol:
        RDKit molecule from :func:`app.chemistry.ligand.molecule_from_smiles`
        or :func:`~app.chemistry.ligand.molecule_from_file`.  It is never
        mutated: a copy is prepared instead.
    name:
        Used for logging and the report; also the PDBQT ``REMARK`` name.
    output_dir:
        Workspace directory (``<workspace>/prepared``).  When given,
        ``ligand.pdbqt`` and ``ligand_prepared.sdf`` are written there.
    options:
        See :class:`PreparationOptions`; defaults are used when omitted.
    progress:
        Optional ``(percent, message)`` callback for the GUI progress bar.

    Raises
    ------
    LigandPreparationError
        With a user-readable message for every failure mode.
    """
    options = options or PreparationOptions()
    started = time.perf_counter()

    def report(percent: int, message: str) -> None:
        log.debug("[%s] %d%% %s", name, percent, message)
        if progress is not None:
            progress(percent, message)

    if mol is None:
        raise LigandPreparationError("There is no ligand to prepare.",
                                     "prepare_ligand received None")

    # --- geometry (hydrogens + 3D + optimisation) --------------------------
    built = build_3d(mol, options, progress)
    working = built.mol
    warnings = list(built.warnings)
    used, energy = built.force_field, built.energy

    # --- PDBQT -------------------------------------------------------------
    report(80, "Assigning AutoDock atom types ...")
    try:
        converter = get_converter(options.converter)
        pdbqt_text = converter.to_pdbqt_string(working)
    except ConversionError as exc:
        raise LigandPreparationError(exc.message, exc.details) from exc

    if not pdbqt_text.strip():                           # pragma: no cover
        raise LigandPreparationError(
            "The docking input file for this ligand came out empty.",
            "PDBQT conversion produced no text",
        )

    report(90, "Writing docking input ...")
    mol_path: Path | None = None
    pdbqt_path: Path | None = None
    if output_dir is not None:
        directory = ensure_dir(output_dir)
        mol_path = directory / "ligand_prepared.sdf"
        pdbqt_path = directory / "ligand.pdbqt"
        try:
            writer = Chem.SDWriter(str(mol_path))
            writer.write(working)
            writer.close()
        except Exception as exc:                         # pragma: no cover
            log.warning("could not write %s: %s", mol_path, exc)
            mol_path = None
        pdbqt_path.write_text(pdbqt_text, encoding="utf-8")

    try:
        summary = summarize_molecule(working, name=name, source="prepared")
    except LigandError:                                  # pragma: no cover
        summary = None

    torsion_count = count_torsions(pdbqt_text)
    declared = rdMolDescriptors.CalcNumRotatableBonds(working)
    if declared and torsion_count == 0:
        warnings.append(
            "The ligand looks flexible but no rotatable bonds were detected; "
            "docking will treat it as rigid."
        )

    prepared = PreparedLigand(
        name=name,
        smiles=_canonical_smiles(working),
        mol=working,
        summary=summary,
        pdbqt_path=pdbqt_path,
        mol_path=mol_path,
        pdbqt_text=pdbqt_text,
        rotatable_bonds=torsion_count,
        force_field_used=used,
        energy=energy,
        converter=converter.name,
        warnings=warnings,
        elapsed_seconds=time.perf_counter() - started,
    )
    report(100, f"Ligand ready ({torsion_count} rotatable bonds)")
    log.info("prepared ligand %s: %d atoms, %d torsions, %.2fs, writer=%s",
             name, working.GetNumAtoms(), torsion_count,
             prepared.elapsed_seconds, converter.name)
    return prepared


def optimize_all_conformers(
    mol: Chem.Mol, options: PreparationOptions
) -> tuple[Chem.Mol, str, float | None]:
    """Minimise every conformer and keep the lowest-energy one.

    Returns ``(single_conformer_mol, force_field, energy)``.
    """
    if options.force_field == "none":
        return Chem.Mol(mol, confId=0), "none", None

    try:
        if options.force_field == "UFF":
            results = AllChem.UFFOptimizeMoleculeConfs(
                mol, maxIters=options.max_iterations
            )
            used = "UFF"
        else:
            results = AllChem.MMFFOptimizeMoleculeConfs(
                mol, maxIters=options.max_iterations
            )
            used = "MMFF94"
    except Exception as exc:                             # pragma: no cover
        raise LigandPreparationError(
            "Alternative 3D conformations could not be optimised for this ligand.",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    if not results:                                      # pragma: no cover
        log.warning("no conformer energies were returned; keeping the first one")
        return Chem.Mol(mol, confId=0), used, None

    best_id, best_energy = min(
        ((int(conf_id), float(energy)) for conf_id, energy in results),
        key=lambda item: item[1],
    )
    log.info("kept conformer %d of %d (energy %.2f)", best_id, len(results), best_energy)
    return Chem.Mol(mol, confId=best_id), used, best_energy


def prepare_from_smiles(
    smiles: str,
    output_dir: str | Path | None = None,
    options: PreparationOptions | None = None,
    progress: ProgressCallback | None = None,
) -> PreparedLigand:
    """Parse ``smiles`` and prepare it for docking in one call."""
    mol = molecule_from_smiles(smiles)
    return prepare_ligand(mol, name=_short_name(smiles), output_dir=output_dir,
                          options=options, progress=progress)


def prepare_from_file(
    path: str | Path,
    output_dir: str | Path | None = None,
    options: PreparationOptions | None = None,
    progress: ProgressCallback | None = None,
) -> PreparedLigand:
    """Read a ligand file and prepare it for docking in one call."""
    path = Path(path)
    mol = molecule_from_file(path)
    return prepare_ligand(mol, name=path.stem, output_dir=output_dir,
                          options=options, progress=progress)


def _canonical_smiles(mol: Chem.Mol) -> str:
    """Canonical SMILES without hydrogens, or ``""`` on failure."""
    try:
        return Chem.MolToSmiles(Chem.RemoveHs(Chem.Mol(mol)))
    except Exception:                                    # pragma: no cover
        return ""


def _short_name(smiles: str) -> str:
    """Display name derived from a SMILES string."""
    token = smiles.strip().split()[0] if smiles.strip() else "ligand"
    return token[:24]


__all__ = [
    "FORCE_FIELDS",
    "BuiltLigand3D",
    "LigandPreparationError",
    "PreparationOptions",
    "PreparedLigand",
    "add_hydrogens",
    "build_3d",
    "embed_conformer",
    "has_3d",
    "optimize_all_conformers",
    "optimize_conformer",
    "prepare_from_file",
    "prepare_from_smiles",
    "prepare_ligand",
    "select_fragment",
]
