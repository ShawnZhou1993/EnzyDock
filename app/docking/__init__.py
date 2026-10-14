"""Docking backends.

Importing this package registers every concrete engine in
:class:`app.docking.base.DockingEngine`, so GUI code can simply ask for
``DockingEngine.available_engines()`` without importing Vina (or Smina, or
GNINA) by name.
"""

from __future__ import annotations

from . import engines                                  # noqa: F401

__all__ = ["engines"]
