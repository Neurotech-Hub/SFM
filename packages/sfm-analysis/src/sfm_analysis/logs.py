"""logs.py — the canonical SFM session-log CSV schemas.

The single definition of both CSV schemas this package may encounter,
shared with every reader so a schema change can't silently drift:

  - ``CSV_HEADER``: the 15-column unified session-log schema, written by
    ``base_station.log_manager.LogManager`` and read by
    ``sfm_analysis.report.loader``. This is what the GUI writes today.
  - ``EXP6_HEADER``: a legacy 6-column experiment CSV from older
    standalone experiment runs. The loader still sniffs it so those
    files are skipped with a warning instead of crashing.

The report test suite's ``report_fixtures.py`` keys every synthetic row
off ``CSV_HEADER`` precisely so a drift breaks the report tests loudly
rather than silently.
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

EXP6_HEADER: List[str] = [
    "timestamp_iso",
    "timestamp_ms",
    "elapsed_s",
    "name",
    "node_id",
    "fields",
]


def heartbeat_path_for(main_path: Path) -> Path:
    """Sibling heartbeat CSV path: ``foo.csv`` -> ``foo_heartbeats.csv``."""
    return main_path.with_name(f"{main_path.stem}_heartbeats.csv")
