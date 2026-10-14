"""Result export: pose structures, CSV tables and readable reports.

Everything in this package is plain Python - no Qt, no RDKit - so exports can
be produced from a script, a test or a future command line interface without
a running GUI.  :mod:`app.gui.main_window` only asks the user *where* to write
and then delegates here.

Exports offered to the user:

=========================  ===================================================
Action                      Produces
=========================  ===================================================
Export Best Pose            the lowest-energy pose as ``.pdb`` (+ ``.pdbqt``)
Export All Poses            every pose plus ``summary.csv`` and ``report.md``
Export CSV                  the pose table (``pose, affinity, rmsd``)
Export Report               a Markdown report of the whole run
=========================  ===================================================

Failures raise :class:`ExportError`, whose ``message`` is written for a bench
scientist and whose ``details`` feeds the "Show Technical Details" dialog -
the same convention as :class:`app.docking.base.EngineError`.
"""

from __future__ import annotations

from .exporters import (
    ExportError,
    default_export_stem,
    export_all_poses,
    export_best_pose,
    export_csv,
    export_report,
    pose_files,
    resolve_pose_files,
)
from .report import build_report, input_labels, write_report

__all__ = [
    "ExportError",
    "build_report",
    "input_labels",
    "default_export_stem",
    "export_all_poses",
    "export_best_pose",
    "export_csv",
    "export_report",
    "pose_files",
    "resolve_pose_files",
    "write_report",
]
