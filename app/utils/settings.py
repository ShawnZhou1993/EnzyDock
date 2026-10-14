"""Persistent application settings.

Settings are stored with :class:`PySide6.QtCore.QSettings` in INI format
under the per-user data directory, which keeps the Windows registry clean and
makes the file easy to inspect or reset manually.

Only *user preferences* belong here.  Per-run scientific parameters are owned
by the workspace (see :mod:`app.docking.docking_config`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings

from . import paths
from .logger import get_logger

log = get_logger(__name__)

ORGANISATION = "EnzyDock"
APPLICATION = "EnzyDock"

#: Default values, used when a key has never been written.
DEFAULTS: dict[str, Any] = {
    "paths/last_input_dir": "",
    "paths/last_export_dir": "",
    "protein/remove_waters": True,
    "protein/remove_hetero": True,
    "protein/keep_chains": "",
    "box/size_x": 22.0,
    "box/size_y": 22.0,
    "box/size_z": 22.0,
    "docking/exhaustiveness": 8,
    "docking/num_poses": 10,
    "docking/energy_range": 3.0,
    "docking/seed": -1,          # -1 == "let Vina pick a random seed"
    "docking/cpu": 0,            # 0 == "use all detected cores"
    "docking/engine": "AutoDock Vina",
    "view/show_protein": True,
    "view/show_ligand": True,
    "view/show_pocket": False,
    "view/show_hbonds": False,
    "general/ask_before_overwrite": True,
    "general/high_detail_viewer": True,
}


class AppSettings:
    """Thin, typed wrapper around :class:`QSettings`."""

    def __init__(self, settings: QSettings | None = None) -> None:
        if settings is not None:
            self._settings = settings
        else:
            ini_path = paths.user_data_dir(create=False) / "settings.ini"
            ini_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings = QSettings(str(ini_path), QSettings.Format.IniFormat)
        log.debug("settings file: %s", self.file_path())

    # -- low level ---------------------------------------------------------
    def value(self, key: str, default: Any = None) -> Any:
        """Read ``key``; fall back to ``DEFAULTS`` then ``default``."""
        fallback = DEFAULTS.get(key, default)
        return self._settings.value(key, fallback)

    def set_value(self, key: str, value: Any) -> None:
        """Persist ``key``."""
        self._settings.setValue(key, value)

    def remove(self, key: str) -> None:
        self._settings.remove(key)

    def sync(self) -> None:
        self._settings.sync()

    def file_path(self) -> Path:
        return Path(self._settings.fileName())

    def reset(self) -> None:
        """Restore every documented default."""
        self._settings.clear()
        for key, value in DEFAULTS.items():
            self._settings.setValue(key, value)
        self.sync()

    # -- typed helpers -----------------------------------------------------
    def get_int(self, key: str) -> int:
        try:
            return int(self.value(key))
        except (TypeError, ValueError):
            return int(DEFAULTS[key])

    def get_float(self, key: str) -> float:
        try:
            return float(self.value(key))
        except (TypeError, ValueError):
            return float(DEFAULTS[key])

    def get_bool(self, key: str) -> bool:
        raw = self.value(key)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)

    def get_str(self, key: str) -> str:
        raw = self.value(key)
        return "" if raw is None else str(raw)

    # -- convenience -------------------------------------------------------
    def last_input_dir(self) -> Path:
        """Directory used to open the next file dialog; home as fallback."""
        stored = self.get_str("paths/last_input_dir")
        candidate = Path(stored) if stored else Path.home()
        return candidate if candidate.is_dir() else Path.home()

    def remember_input_dir(self, path: str | Path) -> None:
        path = Path(path)
        directory = path if path.is_dir() else path.parent
        self.set_value("paths/last_input_dir", str(directory))

    def last_export_dir(self) -> Path:
        stored = self.get_str("paths/last_export_dir")
        candidate = Path(stored) if stored else Path.home()
        return candidate if candidate.is_dir() else Path.home()

    def remember_export_dir(self, path: str | Path) -> None:
        path = Path(path)
        directory = path if path.is_dir() else path.parent
        self.set_value("paths/last_export_dir", str(directory))


#: Process-wide singleton used by the GUI.
_settings: AppSettings | None = None


def settings() -> AppSettings:
    """Return (and lazily create) the global :class:`AppSettings` instance."""
    global _settings
    if _settings is None:
        _settings = AppSettings()
    return _settings
