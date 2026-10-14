"""Convenience launcher for development: ``python run.py``.

Equivalent to ``python -m app.main``; kept at the project root so that new
contributors do not have to remember the ``-m`` form.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
