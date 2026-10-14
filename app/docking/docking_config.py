"""Pure data containers shared between the GUI and the docking backends.

Nothing in this module imports Qt, RDKit or any docking engine: it is the
*contract* both sides agree on.  Keeping it dependency free is what allows the
GUI to stay decoupled from the computational backend and lets new engines
(Vina, QVina2, Smina, GNINA ...) be added without touching a single widget.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# Sensible defaults for a first docking run (AutoDock Vina conventions).
DEFAULT_BOX_SIZE: tuple[float, float, float] = (22.0, 22.0, 22.0)
DEFAULT_EXHAUSTIVENESS = 8
DEFAULT_NUM_POSES = 10
DEFAULT_ENERGY_RANGE = 3.0


class DockingConfigError(ValueError):
    """Raised when user supplied docking parameters are invalid."""


@dataclass(frozen=True)
class BoxConfig:
    """Axis aligned docking box.

    Parameters
    ----------
    center:
        ``(x, y, z)`` centre of the search space in angstrom.
    size:
        ``(x, y, z)`` edge lengths in angstrom.  Vina is happy with anything
        above ~15 A; values above 30 A slow the search down considerably.
    """

    center: tuple[float, float, float]
    size: tuple[float, float, float] = DEFAULT_BOX_SIZE

    def __post_init__(self) -> None:
        if len(self.center) != 3 or len(self.size) != 3:
            raise DockingConfigError("Box center and size must each have 3 components.")
        for name, values in (("center", self.center), ("size", self.size)):
            for value in values:
                if not isinstance(value, (int, float)):
                    raise DockingConfigError(f"Box {name} must be numeric, got {value!r}.")
        for value in self.size:
            if value <= 0:
                raise DockingConfigError(
                    f"Box size must be positive (got {value}); a 0 A box cannot contain a ligand."
                )
            if value > 60:
                raise DockingConfigError(
                    f"Box size {value} A is unusually large; please keep each edge below 60 A."
                )

    @property
    def volume(self) -> float:
        """Box volume in cubic angstrom."""
        return self.size[0] * self.size[1] * self.size[2]

    def to_dict(self) -> dict[str, Any]:
        return {"center": list(self.center), "size": list(self.size)}

    def __str__(self) -> str:
        c, s = self.center, self.size
        return (f"center=({c[0]:.3f}, {c[1]:.3f}, {c[2]:.3f}) "
                f"size=({s[0]:.1f} x {s[1]:.1f} x {s[2]:.1f})")


@dataclass
class DockingParameters:
    """Engine independent docking parameters.

    ``engine`` selects the backend through :class:`app.docking.base.DockingEngine`
    registry; every other field maps onto the Vina command line today and must
    be translated by each adapter.
    """

    exhaustiveness: int = DEFAULT_EXHAUSTIVENESS
    num_poses: int = DEFAULT_NUM_POSES
    energy_range: float = DEFAULT_ENERGY_RANGE
    seed: int | None = None
    cpu: int = 0            # 0 -> let the engine use every available core
    engine: str = "AutoDock Vina"

    def validate(self) -> None:
        """Raise :class:`DockingConfigError` when a parameter is out of range."""
        if not 1 <= self.exhaustiveness <= 100:
            raise DockingConfigError(
                "Exhaustiveness must be between 1 and 100 (8 is a good default)."
            )
        if not 1 <= self.num_poses <= 100:
            raise DockingConfigError("Number of poses must be between 1 and 100.")
        if not 0.0 <= self.energy_range <= 20.0:
            raise DockingConfigError("Energy range must be between 0 and 20 kcal/mol.")
        if self.seed is not None and self.seed < 0:
            raise DockingConfigError("Seed must be a positive integer or left empty.")
        if self.cpu < 0:
            raise DockingConfigError("CPU count cannot be negative.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PoseResult:
    """A single docking pose as parsed from the engine output."""

    pose: int
    affinity: float                 # kcal/mol, negative = better
    rmsd_lb: float = 0.0            # RMSD lower bound vs. best pose
    rmsd_ub: float = 0.0            # RMSD upper bound vs. best pose
    pose_file: Path | None = None   # optional per-pose structure

    def to_dict(self) -> dict[str, Any]:
        return {
            "pose": self.pose,
            "affinity": self.affinity,
            "rmsd_lb": self.rmsd_lb,
            "rmsd_ub": self.rmsd_ub,
            "pose_file": str(self.pose_file) if self.pose_file else "",
        }


@dataclass
class DockingResult:
    """Complete outcome of one docking run."""

    receptor: Path | None = None
    ligand: Path | None = None
    engine: str = "unknown"
    box: BoxConfig | None = None
    parameters: DockingParameters = field(default_factory=DockingParameters)
    poses: list[PoseResult] = field(default_factory=list)
    log_file: Path | None = None
    output_file: Path | None = None
    workspace: Path | None = None
    elapsed_seconds: float = 0.0

    @property
    def best_pose(self) -> PoseResult | None:
        """Lowest-energy pose, or ``None`` when nothing was produced."""
        if not self.poses:
            return None
        return min(self.poses, key=lambda p: p.affinity)

    @property
    def best_affinity(self) -> float | None:
        best = self.best_pose
        return None if best is None else best.affinity

    def to_rows(self) -> list[dict[str, Any]]:
        """Rows ready to be written to CSV or shown in the result table."""
        return [pose.to_dict() for pose in self.poses]
