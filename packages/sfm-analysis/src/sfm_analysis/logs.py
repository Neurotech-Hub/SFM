"""logs.py — the canonical VFM session-log CSV schema.

This is the single definition of the 15-column unified schema, shared by
the writer (``base_station.log_manager.LogManager``, which binds
``CSV_HEADER`` as a class attribute) and every reader
(``sfm_analysis.report.loader``). A change here is a schema change: it must
be made in lockstep with the writer, and the report test suite's
``report_fixtures.py`` keys every synthetic row off this list precisely so
a drift breaks the report tests loudly rather than silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

CSV_HEADER: List[str] = [
    "timestamp_iso",
    "timestamp_ms",
    "elapsed_s",
    "session",
    "run_id",
    "trial",
    "source",
    "direction",
    "node_id",
    "frame_type",
    "event_name",
    "raw_id_hex",
    "raw_data_hex",
    "fields_json",
    "details",
]


def heartbeat_path_for(main_path: Path) -> Path:
    """Sibling heartbeat CSV path: ``foo.csv`` -> ``foo_heartbeats.csv``."""
    return main_path.with_name(f"{main_path.stem}_heartbeats.csv")
