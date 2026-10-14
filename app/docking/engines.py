"""Registry bootstrap: import every concrete docking backend.

Adding a new engine (QVina2, Smina, GNINA, an AI rescoring backend) means
writing its module and adding one import here - no GUI or pipeline code has
to change, because they only ever talk to
:class:`app.docking.base.DockingEngine`.
"""

from __future__ import annotations

from .base import DockingEngine
from .vina_runner import VinaEngine

__all__ = ["DockingEngine", "VinaEngine", "available_engines"]


def available_engines() -> list[str]:
    """Names of every registered engine."""
    return DockingEngine.available_engines()
