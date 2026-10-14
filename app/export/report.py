"""Human readable docking reports.

The report is written as Markdown on purpose: it opens everywhere (any text
editor, GitHub, VS Code, Word) and keeps EnzyDock free of a ``python-docx``
dependency, which would add weight to the PyInstaller bundle for no real
benefit - scientists paste these numbers into their own documents anyway.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .. import __version__
from ..chemistry.protein import read_atoms
from ..docking.docking_config import DockingResult
from ..utils.logger import get_logger
from ..visualization.interactions import (
    Interaction,
    find_interactions,
    group_counts,
)

log = get_logger(__name__)

REPORT_TITLE = "EnzyDock Docking Report"

#: Suffixes used to tell the archived receptor from the archived ligand.
RECEPTOR_INPUT_SUFFIXES: frozenset[str] = frozenset({".pdb", ".ent", ".cif", ".mmcif"})
LIGAND_INPUT_SUFFIXES: frozenset[str] = frozenset(
    {".sdf", ".sd", ".mol", ".mol2", ".smi", ".txt"}
)

#: Receptor files that can be re-read for the interaction analysis, best first.
VIEWABLE_RECEPTORS: tuple[str, ...] = (
    "receptor_clean.pdb", "receptor_protonated.pdb",
)

NOTES = (
    "Affinities are *predicted* values from the docking engine, not measured "
    "binding energies; treat them as a ranking, not as a Kd.\n"
    "RMSD columns describe the distance of each pose to the best pose "
    "(lower/upper bound over all symmetric copies of the molecule).\n"
    "Interactions are **geometric screens**, not a full interaction "
    "assignment: the pose carries no bond orders and at most the polar "
    "hydrogens, so an angle test is not available. A hydrogen bond is a "
    "polar-atom pair within 3.5 A, a salt bridge a charged side chain within "
    "4.0 A, and pi-stacking a flat ligand ring within 5.5 A of an aromatic "
    "side chain. Verify anything you plan to act on."
)


def _text(value: object, fallback: str = "n/a") -> str:
    """Render ``value`` as text, using ``fallback`` for empty/``None``."""
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _name(path: object, fallback: str = "n/a") -> str:
    """File name of ``path`` without the directory part."""
    if path is None:
        return fallback
    return Path(str(path)).name or fallback


def _vector(values: tuple[float, float, float] | None, spec: str = ".2f") -> str:
    if values is None:
        return "n/a"
    return ", ".join(format(float(value), spec) for value in values)


def _table(rows: list[tuple[str, str]], headers: tuple[str, str] = ("Item", "Value")) -> list[str]:
    """Render a two-column Markdown table."""
    lines = [f"| {headers[0]} | {headers[1]} |", "| --- | --- |"]
    lines += [f"| {key} | {value} |" for key, value in rows]
    return lines


def input_labels(result: DockingResult) -> tuple[str, str]:
    """Names of the receptor and ligand the user actually supplied.

    ``<workspace>/input`` keeps a copy of every original input, which is what a
    reader of the report cares about - ``receptor.pdbqt`` says nothing, while
    ``1UBQ.pdb`` does.  A SMILES ligand is rendered as the string itself.
    """
    if result.workspace is None:
        return "", ""
    input_dir = Path(result.workspace) / "input"
    if not input_dir.is_dir():
        return "", ""

    receptor = ""
    ligand = ""
    smiles = input_dir / "ligand.smi"
    if smiles.is_file():
        text = smiles.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            ligand = f"SMILES: {text}"

    for path in sorted(p for p in input_dir.iterdir() if p.is_file()):
        suffix = path.suffix.lower()
        if not receptor and suffix in RECEPTOR_INPUT_SUFFIXES:
            receptor = path.name
        elif not ligand and suffix in LIGAND_INPUT_SUFFIXES:
            ligand = path.name
    return receptor, ligand


def best_pose_interactions(result: DockingResult) -> list[Interaction]:
    """Interactions of the best pose, or ``[]`` when they cannot be derived.

    This re-reads the prepared receptor and the pose file, so it is a
    best-effort analysis: a report written after the workspace was moved or
    cleaned simply omits the section instead of failing.
    """
    pose = result.best_pose
    workspace = result.workspace
    if pose is None or pose.pose_file is None or workspace is None:
        return []

    pose_path = Path(pose.pose_file)
    prepared = Path(workspace) / "prepared"
    receptor_path = next(
        (prepared / name for name in VIEWABLE_RECEPTORS if (prepared / name).is_file()),
        None,
    )
    if receptor_path is None or not pose_path.is_file():
        log.debug("no structures available for the interaction analysis")
        return []

    try:
        receptor_atoms = read_atoms(receptor_path)
        ligand_atoms = read_atoms(pose_path)
    except Exception as exc:                           # pragma: no cover
        log.warning("could not analyse interactions: %s", exc)
        return []
    return find_interactions(receptor_atoms, ligand_atoms)


def build_report(result: DockingResult, title: str = REPORT_TITLE) -> str:
    """Return the full report for ``result`` as Markdown text.

    The report is complete on its own: it repeats the receptor, the ligand,
    the search box and every engine parameter, because a result file is worth
    little without the settings that produced it.
    """
    best = result.best_pose
    params = result.parameters
    box = result.box

    lines: list[str] = [f"# {title}", ""]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"Generated {stamp} by EnzyDock {__version__}.")
    lines.append("")

    # -- headline ----------------------------------------------------------
    lines.append("## Result")
    lines.append("")
    if best is None:
        lines.append("The run finished without producing any pose.")
    else:
        lines.append(
            f"**Best affinity: {best.affinity:.3f} kcal/mol** (pose {best.pose} "
            f"of {len(result.poses)})"
        )
        lines.append("")
        lines.append(
            f"Poses: {len(result.poses)} | Runtime: {result.elapsed_seconds:.1f} s | "
            f"Engine: {_text(result.engine, 'unknown')}"
        )
    lines.append("")

    # -- settings ---------------------------------------------------------
    cpu = "all detected cores" if params.cpu == 0 else f"{params.cpu}"
    seed = "random (engine default)" if params.seed is None else str(params.seed)
    input_receptor, input_ligand = input_labels(result)
    lines.append("## Run settings")
    lines.append("")
    lines += _table(
        [
            ("Receptor", input_receptor or _name(result.receptor)),
            ("Ligand", input_ligand or _name(result.ligand)),
            ("Engine", _text(result.engine, "unknown")),
            ("Box centre (A)", _vector(box.center) if box else "n/a"),
            ("Box size (A)", _vector(box.size, ".1f") if box else "n/a"),
            ("Box volume (A^3)", f"{box.volume:,.0f}" if box else "n/a"),
            ("Exhaustiveness", str(params.exhaustiveness)),
            ("Poses requested", str(params.num_poses)),
            ("Energy range (kcal/mol)", f"{params.energy_range:.1f}"),
            ("Seed", seed),
            ("CPU threads", cpu),
            ("Runtime (s)", f"{result.elapsed_seconds:.1f}"),
        ]
    )
    lines.append("")

    # -- pose table -------------------------------------------------------
    lines.append("## Poses")
    lines.append("")
    if not result.poses:
        lines.append("_No poses were produced._")
    else:
        lines.append(
            "| Pose | Affinity (kcal/mol) | RMSD l.b. (A) | RMSD u.b. (A) | Structure |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for pose in result.poses:
            marker = " (best)" if best is not None and pose.pose == best.pose else ""
            lines.append(
                f"| {pose.pose}{marker} | {pose.affinity:.3f} | "
                f"{pose.rmsd_lb:.3f} | {pose.rmsd_ub:.3f} | "
                f"{_name(pose.pose_file, '-')} |"
            )
    lines.append("")

    # -- interactions of the best pose -------------------------------------
    interactions = best_pose_interactions(result)
    if best is not None:
        lines.append(f"## Interactions of pose {best.pose} (best pose)")
        lines.append("")
        if not interactions:
            lines.append(
                "_No receptor-ligand contact within 4.5 A was found for this "
                "pose (or the structures are no longer available)."
            )
            lines.append("")
        else:
            counts = group_counts(interactions)
            summary = ", ".join(f"{count}x {kind}" for kind, count in counts.items())
            lines.append(f"{len(interactions)} possible interaction(s): {summary}.")
            lines.append("")
            lines.append("| Interaction | Receptor | Ligand | Distance (A) |")
            lines.append("| --- | --- | --- | --- |")
            for item in interactions:
                kind, protein_atom, ligand_atom, distance = item.to_row()
                lines.append(
                    f"| {kind} | {protein_atom} | {ligand_atom} | {distance} |"
                )
            lines.append("")

    # -- files ------------------------------------------------------------
    workspace = result.workspace
    lines.append("## Files")
    lines.append("")
    file_rows = [
        ("Engine output (PDBQT)", _name(result.output_file)),
        ("Engine log", _name(result.log_file)),
        ("Prepared receptor", _name(result.receptor)),
        ("Prepared ligand", _name(result.ligand)),
    ]
    if workspace is not None:
        file_rows.insert(0, ("Workspace", str(workspace)))
    lines += _table(file_rows, headers=("File", "Path"))
    lines.append("")

    # -- notes ------------------------------------------------------------
    lines.append("## Notes")
    lines.append("")
    lines.append(NOTES)
    lines.append("")

    return "\n".join(lines)


def write_report(result: DockingResult, path: str | Path) -> Path:
    """Write :func:`build_report` to ``path`` and return it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_report(result), encoding="utf-8")
    log.info("wrote docking report: %s", target)
    return target


__all__ = [
    "REPORT_TITLE",
    "VIEWABLE_RECEPTORS",
    "best_pose_interactions",
    "build_report",
    "input_labels",
    "write_report",
]
