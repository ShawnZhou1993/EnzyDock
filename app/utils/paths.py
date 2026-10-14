"""Filesystem path helpers for EnzyDock.

Every path is resolved **at call time**, never at import time, so the same
code works when running from a source checkout and when running from a
PyInstaller one-file bundle (``sys.frozen``).

Conventions
-----------
* :func:`resource_dir`   -> read-only assets shipped with the application.
* :func:`user_data_dir`  -> writable per-user directory (settings, logs).
* :func:`workspace_root` -> writable directory holding per-run workspaces.

All of them can be overridden through environment variables, which is handy
for tests and for portable installations:

``ENZYDOCK_WORKSPACE``
    Overrides the workspace root.
``ENZYDOCK_DATA_DIR``
    Overrides the per-user data directory.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

APP_NAME = "EnzyDock"
APP_AUTHOR = "EnzyDock"

ENV_WORKSPACE = "ENZYDOCK_WORKSPACE"
ENV_DATA_DIR = "ENZYDOCK_DATA_DIR"

#: Sub-directories created inside every run workspace.
WORKSPACE_SUBDIRS: tuple[str, ...] = (
    "input",
    "prepared",
    "docking",
    "results",
    "visualization",
)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def is_frozen() -> bool:
    """Return ``True`` when the application runs from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """Return the project checkout root (the directory holding ``app/``)."""
    return Path(__file__).resolve().parents[2]


def app_package_dir() -> Path:
    """Return the ``app`` package directory."""
    return Path(__file__).resolve().parents[1]


def resource_dir() -> Path:
    """Return the directory containing shipped, read-only resources."""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", project_root()))
        return base / "app" / "resources"
    return app_package_dir() / "resources"


def resource(*parts: str) -> Path:
    """Return a path inside the resource directory.

    Example::

        resource("viewer", "index.html")
    """
    return resource_dir().joinpath(*parts)


def user_data_dir(create: bool = True) -> Path:
    """Return the writable per-user directory used for settings and logs.

    macOS keeps application support data in ``~/Library/Application
    Support``; elsewhere a dot-directory in the home folder is conventional.
    """
    from .platforms import default_user_data_dir

    env = os.environ.get(ENV_DATA_DIR)
    path = Path(env) if env else default_user_data_dir()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_root(create: bool = True) -> Path:
    """Return the root directory that contains one folder per docking run."""
    from .platforms import default_workspace_root

    env = os.environ.get(ENV_WORKSPACE)
    if env:
        path = Path(env)
    elif is_frozen():
        path = default_workspace_root()
    else:
        path = project_root() / "workspace"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(text: str) -> str:
    """Turn arbitrary text into a filesystem-safe token."""
    return _SLUG_RE.sub("-", text.strip()).strip("-") or "run"


def create_workspace(tag: str | None = None, create: bool = True) -> Path:
    """Create a fresh, uniquely named workspace for a single docking run.

    Parameters
    ----------
    tag:
        Optional human readable suffix, e.g. a protein name.  It is slugified
        so that it is safe on every supported platform.
    create:
        When ``True`` (default) the directory tree is created on disk.

    Returns
    -------
    Path
        ``<workspace_root>/2026-10-08_143522_<tag>`` with the standard
        sub-directories (``input``, ``prepared``, ``docking``, ``results``,
        ``visualization``) already present.
    """
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name = f"{stamp}_{_slug(tag)}" if tag else stamp
    path = workspace_root(create=False) / name
    path = unique_path(path)
    if create:
        for sub in WORKSPACE_SUBDIRS:
            (path / sub).mkdir(parents=True, exist_ok=True)
    return path


def unique_path(path: Path) -> Path:
    """Return ``path``, or ``<stem>_1<suffix>``, ``<stem>_2<suffix>`` ...

    The counter is inserted *before* the extension so that file types stay
    recognisable (``pose_1.pdb`` rather than ``pose.pdb_1``).  Used to avoid
    clobbering an existing file or directory when two runs start within the
    same second.
    """
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to derive a unique path from {path!r}")


def ensure_dir(path: str | Path) -> Path:
    """Create ``path`` (including parents) if missing and return it as a Path."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def safe_copy(source: str | Path, destination_dir: str | Path) -> Path:
    """Copy ``source`` into ``destination_dir`` without ever overwriting.

    Original user input is treated as read-only throughout EnzyDock, so every
    input file is copied into the workspace before being touched.
    """
    import shutil

    src = Path(source)
    dest_dir = ensure_dir(destination_dir)
    target = unique_path(dest_dir / src.name)
    shutil.copy2(src, target)
    return target


def iter_files(directory: str | Path, suffixes: Iterable[str]) -> list[Path]:
    """Return sorted files in ``directory`` matching any of ``suffixes``."""
    wanted = {s.lower() if s.startswith(".") else f".{s.lower()}" for s in suffixes}
    if not Path(directory).is_dir():
        return []
    return sorted(p for p in Path(directory).iterdir()
                  if p.is_file() and p.suffix.lower() in wanted)
