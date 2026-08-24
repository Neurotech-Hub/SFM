"""demo — a real, bundled session CSV for `sfm-report --demo` and for
this package's own real-data smoke test.

``Bandit_test_01.csv`` (+ its ``_heartbeats.csv`` sibling) is a genuine
field-recorded two_armed_bandit session: a 9-row aborted restart (run 1)
followed by the real 512-row, ~1291.8s session (run 2), with a fault
interval, a dome milestone/edge dedup case, and rows after ``session_end``
(a trailing heartbeat) -- see ``tests/test_report_smoke.py``'s docstring
for the exact, independently verified numbers this exercises.

This is a proper subpackage (not a bare directory) for the same reason as
``report/designs/``: setuptools' non-namespace package discovery only
finds real packages, so without ``__init__.py`` the CSVs would silently
not ship in a built wheel while an editable install kept working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def _packaged_demo_dir() -> Path:
    fallback = Path(__file__).resolve().parent
    try:
        from importlib.resources import files
    except ImportError:  # pragma: no cover
        return fallback
    try:
        resolved = Path(str(files(__package__)))
    except (ImportError, ModuleNotFoundError, TypeError, ValueError, OSError):
        return fallback
    return resolved if resolved.is_dir() else fallback


_DEMO_DIR: Path = _packaged_demo_dir()

DEMO_SESSION_NAME = "Bandit_test_01"
DEMO_SESSION_PATH: Path = _DEMO_DIR / f"{DEMO_SESSION_NAME}.csv"
DEMO_HEARTBEATS_PATH: Optional[Path] = _DEMO_DIR / f"{DEMO_SESSION_NAME}_heartbeats.csv"
