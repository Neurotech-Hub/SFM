"""loader.py — CSV → typed rows for report generation.

Reads a session CSV (and its sibling ``_heartbeats.csv``) written by the
SFM base station's ``LogManager`` (see ``sfm_analysis.logs.CSV_HEADER``
for the canonical 15-column header) and turns each line into a ``LogRow``
/ ``HeartbeatRow``. Also sniffs older
schemas still found on disk so a report tool can skip them cleanly instead
of crashing on a header mismatch.

Two things every caller must know before trusting a value:

  - ``elapsed_s`` in the CSV is *run-relative* and resets to 0 each time a
    session is reopened (new run_id). Never read it directly — recompute
    from ``timestamp_ms`` once rows are grouped into runs (see session.py).
  - The event name "Dome Opened" is written for two different wire events:
    the ``CanEvent.DomeOpened`` milestone (raw byte 0 = 0x03, carrying a
    pellet count) and a renamed ``InputChanged`` dome edge (raw byte 0 =
    0x06). Pairing dome-open/close bouts by event_name alone double-counts.
    Use ``LogRow.can_event`` to disambiguate.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..logs import CSV_HEADER, EXP6_HEADER, heartbeat_path_for
from ..protocol import CanEvent, DispenseState, ServiceStatus, parse_heartbeat


class LogSchema(str, Enum):
    """Which CSV header shape a file has."""

    UNIFIED = "unified"    # sfm_analysis.logs.CSV_HEADER — the only schema with fields_json
    LEGACY9 = "legacy9"    # timestamp_iso,timestamp_ms,direction,node_id,frame_type,
                            # event_name,raw_id_hex,raw_data_hex,details
    EXP6 = "exp6"           # sfm_analysis.logs.EXP6_HEADER — legacy 6-column
                            # experiment CSV (no longer written; sniffed so old files skip)
    UNKNOWN = "unknown"


LEGACY9_HEADER = [
    "timestamp_iso", "timestamp_ms", "direction", "node_id", "frame_type",
    "event_name", "raw_id_hex", "raw_data_hex", "details",
]


def sniff_schema(path: Path) -> LogSchema:
    """Identify a CSV's schema from its header row without reading the body."""
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), None)
    except OSError:
        return LogSchema.UNKNOWN
    if header == CSV_HEADER:
        return LogSchema.UNIFIED
    if header == LEGACY9_HEADER:
        return LogSchema.LEGACY9
    if header == EXP6_HEADER:
        return LogSchema.EXP6
    return LogSchema.UNKNOWN


@dataclass
class LogRow:
    """One row of a unified-schema session CSV."""

    idx: int                      # file order — stable-sort tiebreak when ts_ms ties
    ts_ms: int
    iso: str
    session: str
    run_id: int
    trial: int
    source: str                   # CAN | EXP | BNC | SYS
    direction: str                # TX | RX | SYS | LOCAL
    node_id: int
    frame_type: str
    event_name: str
    raw_id: int
    raw_data: bytes
    fields: Dict[str, Any] = field(default_factory=dict)   # parsed fields_json; {} elsewhere
    details: str = ""
    t: float = 0.0                 # run-relative seconds, filled in by session.split_runs
    post_session: bool = False     # True once a session_end row has been seen for this run

    @property
    def can_event(self) -> Optional[CanEvent]:
        """
        raw_data[0] decoded as a CanEvent, for CAN EVENT rows.

        Disambiguates the two wire events that both render as event_name
        "Dome Opened": the CanEvent.DomeOpened milestone (raw byte 0 =
        0x03) versus the renamed InputChanged dome edge (raw byte 0 =
        0x06, i.e. CanEvent.InputChanged). Returns None for anything that
        isn't a CAN EVENT row or whose first byte isn't a known CanEvent.
        """
        if self.frame_type != "EVENT" or not self.raw_data:
            return None
        try:
            return CanEvent(self.raw_data[0])
        except ValueError:
            return None


@dataclass
class HeartbeatRow:
    """One decoded row of a session's sibling ``_heartbeats.csv``."""

    ts_ms: int
    node_id: int
    t: float
    state: DispenseState
    presence: bool
    pellet: bool
    load_position: bool
    dome_open: bool
    fault: ServiceStatus
    pellets_presented: int
    pellets_taken: int


@dataclass
class SessionRef:
    """One discovered session file, before it is loaded and split into runs."""

    session: str
    path: Path
    heartbeats_path: Optional[Path]
    schema: LogSchema
    size_bytes: int
    mtime: float


def _parse_hex_bytes(raw_data_hex: str) -> bytes:
    if not raw_data_hex:
        return b""
    try:
        return bytes.fromhex(raw_data_hex.replace(" ", ""))
    except ValueError:
        return b""


def load_rows(path: Path) -> Tuple[List[LogRow], LogSchema, List[str]]:
    """
    Load a session CSV into LogRow objects.

    Returns (rows, schema, warnings). Rows are returned in file order,
    unsorted — callers that need time order must stable-sort on
    ``(ts_ms, idx)`` themselves (see session.split_runs), since a handful of
    rows in real logs arrive out of order by a few milliseconds.

    Only LogSchema.UNIFIED files are actually parsed into LogRow; any other
    schema returns an empty row list plus a warning explaining why, so
    callers can skip the file rather than crash on a header mismatch.
    """
    warnings: List[str] = []
    schema = sniff_schema(path)
    if schema != LogSchema.UNIFIED:
        warnings.append(
            f"{path.name}: schema is '{schema.value}', not the unified 15-column "
            "schema — skipping (no fields_json / run_id to analyze)."
        )
        return [], schema, warnings

    rows: List[LogRow] = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, raw in enumerate(reader):
            fields: Dict[str, Any] = {}
            fields_json = raw.get("fields_json") or ""
            if fields_json:
                try:
                    parsed = json.loads(fields_json)
                    if isinstance(parsed, dict):
                        fields = parsed
                except (json.JSONDecodeError, TypeError):
                    warnings.append(
                        f"{path.name}: row {idx + 2}: malformed fields_json, "
                        "treated as empty"
                    )

            try:
                ts_ms = int(raw.get("timestamp_ms") or 0)
            except ValueError:
                ts_ms = 0
            try:
                run_id = int(raw.get("run_id") or 0)
            except ValueError:
                run_id = 0
            try:
                trial = int(raw.get("trial") or 0)
            except ValueError:
                trial = 0
            try:
                node_id = int(raw.get("node_id") or 0)
            except ValueError:
                node_id = 0
            raw_id_hex = raw.get("raw_id_hex") or "0x0"
            try:
                raw_id = int(raw_id_hex, 16)
            except ValueError:
                raw_id = 0

            rows.append(LogRow(
                idx=idx,
                ts_ms=ts_ms,
                iso=raw.get("timestamp_iso") or "",
                session=raw.get("session") or "",
                run_id=run_id,
                trial=trial,
                source=raw.get("source") or "",
                direction=raw.get("direction") or "",
                node_id=node_id,
                frame_type=raw.get("frame_type") or "",
                event_name=raw.get("event_name") or "",
                raw_id=raw_id,
                raw_data=_parse_hex_bytes(raw.get("raw_data_hex") or ""),
                fields=fields,
                details=raw.get("details") or "",
            ))

    return rows, schema, warnings


def load_heartbeats(path: Path) -> List[HeartbeatRow]:
    """
    Load a ``_heartbeats.csv`` sibling file and decode each payload.

    Silently skips rows with an unparseable payload (parse_heartbeat
    returns None on anything shorter than 6 bytes) rather than raising —
    a truncated last row on a live-mounted drive should not sink the
    report. Returns rows unsorted, same caveat as load_rows.
    """
    if not path.exists():
        return []

    out: List[HeartbeatRow] = []
    schema = sniff_schema(path)
    if schema != LogSchema.UNIFIED:
        return out

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            data = _parse_hex_bytes(raw.get("raw_data_hex") or "")
            hb = parse_heartbeat(data)
            if hb is None:
                continue
            try:
                ts_ms = int(raw.get("timestamp_ms") or 0)
            except ValueError:
                continue
            try:
                node_id = int(raw.get("node_id") or 0)
            except ValueError:
                continue
            out.append(HeartbeatRow(
                ts_ms=ts_ms,
                node_id=node_id,
                t=0.0,
                state=hb.dispense_state,
                presence=hb.mouse_presence,
                pellet=hb.pellet,
                load_position=hb.load_position,
                dome_open=hb.dome_open,
                fault=hb.fault_code,
                pellets_presented=hb.pellets_presented,
                pellets_taken=hb.pellets_taken,
            ))
    return out


def discover_sessions(log_dir: Path) -> List[SessionRef]:
    """
    List every session CSV in ``log_dir`` (non-recursive), newest first.

    Skips ``*_heartbeats.csv`` and ``*_export_*.csv`` files themselves —
    they are siblings/snapshots of a primary session file, not sessions in
    their own right. Each entry's ``schema`` lets a caller filter out
    legacy files before attempting to load them.
    """
    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        return []

    refs: List[SessionRef] = []
    for path in sorted(log_dir.glob("*.csv")):
        if path.name.endswith("_heartbeats.csv"):
            continue
        if "_export_" in path.stem:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        hb_path = heartbeat_path_for(path)
        refs.append(SessionRef(
            session=path.stem,
            path=path,
            heartbeats_path=hb_path if hb_path.exists() else None,
            schema=sniff_schema(path),
            size_bytes=stat.st_size,
            mtime=stat.st_mtime,
        ))

    refs.sort(key=lambda r: r.mtime, reverse=True)
    return refs
