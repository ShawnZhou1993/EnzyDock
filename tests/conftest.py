"""Pytest configuration shared by every test module.

Qt widgets cannot be created without a ``QApplication`` and must never open a
real window during CI, so the offscreen platform plug-in is selected here
before any Qt import happens.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --- environment for headless GUI tests ----------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ENZYDOCK_NO_WEBENGINE", "1")   # keep tests fast and stable
os.environ.setdefault("ENZYDOCK_DATA_DIR", str(PROJECT_ROOT / ".pytest-data"))
os.environ.setdefault("ENZYDOCK_WORKSPACE", str(PROJECT_ROOT / ".pytest-workspace"))

from tests.factories import build_cif_text, build_pdb_lines  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch, tmp_path: Path) -> Path:
    """Give every test its own settings/log directory.

    Without this, widget tests would share one ``settings.ini`` and leak
    parameters (e.g. exhaustiveness) into each other.
    """
    target = tmp_path / "user-data"
    monkeypatch.setenv("ENZYDOCK_DATA_DIR", str(target))
    return target


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Root of the EnzyDock checkout."""
    return PROJECT_ROOT


@pytest.fixture()
def mini_pdb(tmp_path: Path) -> Path:
    """A tiny but spec-compliant PDB file (3 ALA, 1 water, 1 Zn)."""
    path = tmp_path / "mini.pdb"
    path.write_text("\n".join(build_pdb_lines()) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def multi_chain_pdb(tmp_path: Path) -> Path:
    """A PDB file with two chains (A and B)."""
    lines = ["HEADER    TWO CHAINS"]
    serial = 0
    for chain in ("A", "B"):
        for residue_index in range(1, 3):
            for name, (x, y, z) in {
                "N": (0.000, 0.000, 0.000),
                "CA": (1.458, 0.000, 0.000),
                "C": (2.009, 1.420, 0.000),
                "O": (3.228, 1.620, 0.000),
            }.items():
                serial += 1
                from tests.factories import pdb_atom_line

                lines.append(
                    pdb_atom_line(serial, name, "ALA", chain, residue_index,
                                  x + residue_index * 3.0,
                                  y + (0.0 if chain == "A" else 20.0),
                                  z, name[0])
                )
    lines.append("END")
    path = tmp_path / "two_chains.pdb"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def mini_cif(tmp_path: Path) -> Path:
    """The same structure expressed as mmCIF."""
    path = tmp_path / "mini.cif"
    path.write_text(build_cif_text(), encoding="utf-8")
    return path


@pytest.fixture()
def aspirin_mol(tmp_path: Path) -> Path:
    """Aspirin written as an MDL molfile by RDKit."""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mol = Chem.MolFromSmiles("CC(=O)Oc1ccccc1C(=O)O")
    assert mol is not None
    AllChem.Compute2DCoords(mol)
    path = tmp_path / "aspirin.mol"
    path.write_text(Chem.MolToMolBlock(mol), encoding="utf-8")
    return path


@pytest.fixture()
def qapp():
    """A process-wide ``QApplication`` for widget tests."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
