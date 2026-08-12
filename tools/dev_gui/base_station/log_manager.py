"""
log_manager.py — In-memory event ring buffer with CSV auto-save.

Every CAN frame (received or sent), experiment row, BNC edge, and sync marker
is recorded as a LogEntry. The display buffer is a fixed-size deque (ring
buffer); the CSV file grows unbounded for the session and is never truncated.

Two kinds of CSV sink exist:

  - The unnamed, per-process sink opened by the constructor
    (``session_%Y%m%d_%H%M%S.csv``) — catches discovery/registry/manual
    traffic before an operator names an experiment session.
  - The named, append-on-reuse sink opened by ``open_session()`` — one file
    per experiment session name, carrying a ``run_id`` that increments each
    time the same name is reopened (in this process or a prior one) so a
    session can be resumed across GUI restarts without losing history.

``open_session()`` closes whichever sink is currently open and switches to
the named one; it is not a second, parallel writer, so anything logged
before the first experiment session lives only in the unnamed file.

Performance note:
  At 9 nodes × 1 Hz heartbeat + events ≈ ~36 msgs/sec peak.
  A 1,000-entry deque update is O(1) amortized.  CSV writes use line-buffered
  IO so each append is a single fwrite — no performance concern.
"""

from __future__ import annotations

import csv
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional


def sanitize_session_name(name: str) -> str:
    """
    Turn an operator-entered session name into a filesystem-safe stem.

    Collapses any run of characters other than letters/digits/dot/dash/
    underscore into a single underscore, then trims leading/trailing
    separators and caps the length. Returns "" for a name that sanitizes to
    nothing (blank, or punctuation-only) — callers must treat that as
    "no session name entered".
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return cleaned.strip("._-")[:64]


@dataclass
class LogEntry:
    """One logged CAN frame, experiment row, BNC edge, or sync marker."""

    timestamp: float                # time.time()
    direction: str                  # "TX", "RX", "SYS", or "LOCAL"
    node_id: int                    # 0 = broadcast / discovery / base-station-local
    frame_type: str                 # "COMMAND", "EVENT", "HEARTBEAT", "DISCOVERY", "BNC", "SYNC", "EXPERIMENT", ...
    event_name: str                 # Human-readable name e.g. "Dispense", "OnPlate"
    raw_id: int                     # CAN arbitration ID
    raw_data: bytes                 # CAN payload
    details: str = ""               # Extra human-readable context
    run_id: int = 0                 # Which run of the current session this row belongs to (0 = none)
    trial: int = 0                  # Experiment trial number at the time this row was written (0 = none)
    source: str = "CAN"             # "CAN", "EXP", "BNC", "SYS"
    fields: Dict[str, Any] = field(default_factory=dict)  # Structured payload (e.g. experiment log fields)

    @property
    def timestamp_str(self) -> str:
        dt = datetime.fromtimestamp(self.timestamp)
        return dt.strftime("%H:%M:%S.%f")[:-3]   # HH:MM:SS.mmm

    @property
    def timestamp_iso(self) -> str:
        return datetime.fromtimestamp(self.timestamp).isoformat(timespec="milliseconds")

    @property
    def timestamp_ms(self) -> int:
        return int(self.timestamp * 1000)

    @property
    def raw_data_hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.raw_data)

    @property
    def raw_id_hex(self) -> str:
        return f"0x{self.raw_id:03X}"


class LogManager:
    """
    In-memory log buffer + CSV auto-save, with an optional named session sink.

    Usage::

        lm = LogManager(log_dir="~/sfm_logs", auto_save=True)
        lm.add(LogEntry(...))                       # goes to the unnamed sink
        run_id = lm.open_session("cohortA_day3", "~/sfm_logs")
        lm.set_context(session="cohortA_day3", run_id=run_id, session_start_ts=time.time())
        lm.add(LogEntry(...))                       # goes to the named sink, stamped with run_id
        entries = lm.get_filtered(show_heartbeats=False)
        lm.export("~/Desktop/my_session.csv")
    """

    CSV_HEADER = [
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

    def __init__(
        self,
        max_entries: int = 1000,
        log_dir: str = "~/sfm_logs",
        auto_save: bool = True,
    ) -> None:
        self._max_entries = max_entries
        self._buffer: Deque[LogEntry] = deque(maxlen=max_entries)
        self._auto_save = auto_save
        self._csv_path: Optional[Path] = None
        self._csv_file = None
        self._csv_writer = None

        # Named-session context, applied to any entry that doesn't already
        # carry its own non-default value (see add()).
        self._session: str = ""
        self._run_id: int = 0
        self._trial: int = 0
        self._session_start_ts: Optional[float] = None

        if auto_save:
            self._open_csv(log_dir)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(self, entry: LogEntry) -> None:
        """
        Append an entry to the ring buffer and (optionally) CSV.

        Writes whenever a CSV sink is open — the constructor's ``auto_save``
        flag only controls whether the *unnamed* per-process sink opens; an
        explicit ``open_session()`` call always opens a writer, since naming
        a session is an explicit request to record it regardless of that
        constructor flag.
        """
        self._stamp_context(entry)
        self._buffer.append(entry)
        if self._csv_writer is not None:
            self._csv_writer.writerow(self._entry_to_row(entry))
            self._csv_file.flush()

    def _stamp_context(self, entry: LogEntry) -> None:
        """Fill in run_id/trial from the current session context when unset."""
        if entry.run_id == 0 and self._run_id:
            entry.run_id = self._run_id
        if entry.trial == 0 and self._trial:
            entry.trial = self._trial

    # ------------------------------------------------------------------
    # Read / filter
    # ------------------------------------------------------------------

    def get_filtered(
        self,
        node_id: Optional[int] = None,
        frame_type: Optional[str] = None,
        show_heartbeats: bool = False,
    ) -> List[LogEntry]:
        """
        Return a filtered view of the buffer (newest-first).

        Args:
            node_id:        Filter to a specific node; None = all nodes.
            frame_type:     Filter to a type ("EVENT", "COMMAND", etc.); None = all.
            show_heartbeats: Include HEARTBEAT frames (hidden by default).
        """
        result = []
        for entry in reversed(self._buffer):
            if not show_heartbeats and entry.frame_type == "HEARTBEAT":
                continue
            if node_id is not None and entry.node_id != node_id:
                continue
            if frame_type is not None and entry.frame_type != frame_type:
                continue
            result.append(entry)
        return result

    def all_entries(self) -> List[LogEntry]:
        """All buffered entries, oldest-first."""
        return list(self._buffer)

    @property
    def total_count(self) -> int:
        return len(self._buffer)

    @property
    def csv_path(self) -> Optional[Path]:
        return self._csv_path

    @property
    def session_name(self) -> str:
        return self._session

    @property
    def run_id(self) -> int:
        return self._run_id

    # ------------------------------------------------------------------
    # Named session management
    # ------------------------------------------------------------------

    def set_context(
        self,
        session: Optional[str] = None,
        run_id: Optional[int] = None,
        trial: Optional[int] = None,
        session_start_ts: Optional[float] = None,
    ) -> None:
        """Update the ambient session/run/trial context stamped onto new rows."""
        if session is not None:
            self._session = session
        if run_id is not None:
            self._run_id = run_id
        if trial is not None:
            self._trial = trial
        if session_start_ts is not None:
            self._session_start_ts = session_start_ts

    def preview_session_path(self, session_name: str, log_dir: str) -> Dict[str, Any]:
        """
        Resolve what open_session(session_name, log_dir) would do, without
        opening anything — used to render the "appending to ... (run N,
        M rows)" hint in the UI as the operator types.
        """
        sanitized = sanitize_session_name(session_name)
        if not sanitized:
            return {"valid": False, "path": None, "will_create": True, "next_run_id": 1, "row_count": 0}
        dir_path = Path(log_dir).expanduser().resolve()
        path = dir_path / f"{sanitized}.csv"
        if not path.exists():
            return {"valid": True, "path": path, "will_create": True, "next_run_id": 1, "row_count": 0}
        max_run_id, row_count, header_ok = self._scan_existing(path)
        if not header_ok:
            # A stale-format file of the same name — preview_session_path
            # mirrors open_session()'s decision to divert rather than clobber.
            diverted = dir_path / f"{sanitized}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            return {"valid": True, "path": diverted, "will_create": True, "next_run_id": 1, "row_count": 0}
        return {
            "valid": True,
            "path": path,
            "will_create": False,
            "next_run_id": max_run_id + 1,
            "row_count": row_count,
        }

    def open_session(self, session_name: str, log_dir: str) -> int:
        """
        Open (or resume) the named CSV sink for an experiment session.

        Closes whatever sink is currently open first — the unnamed,
        per-process ``session_<timestamp>.csv`` opened by the constructor
        (which keeps whatever was written to it before this call; that
        traffic is never dropped, just not merged into the named file) or a
        previously-opened named sink. If a file for this name already
        exists with the current header, opens it for append and
        returns one past the highest ``run_id`` found in it; otherwise
        creates a fresh file with ``run_id = 1``. If a file of this name
        exists but is a stale/foreign format, diverts to a timestamped
        filename rather than corrupting or overwriting it.

        Returns the run_id that new entries will be stamped with.
        """
        self._close_named_csv()

        sanitized = sanitize_session_name(session_name)
        if not sanitized:
            raise ValueError("session_name sanitizes to empty — refusing to open a session")

        dir_path = Path(log_dir).expanduser().resolve()
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / f"{sanitized}.csv"

        run_id = 1
        mode = "w"
        if path.exists():
            max_run_id, _row_count, header_ok = self._scan_existing(path)
            if header_ok:
                mode = "a"
                run_id = max_run_id + 1
            else:
                path = dir_path / f"{sanitized}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                mode = "w"

        self._csv_path = path
        self._csv_file = open(path, mode, newline="", buffering=1)
        self._csv_writer = csv.writer(self._csv_file)
        if mode == "w":
            self._csv_writer.writerow(self.CSV_HEADER)

        self._session = sanitized
        self._run_id = run_id
        self._trial = 0
        self._session_start_ts = time.time()

        self.add(LogEntry(
            timestamp=self._session_start_ts,
            direction="SYS",
            node_id=0,
            frame_type="SESSION_OPEN",
            event_name="SessionOpen",
            raw_id=0,
            raw_data=b"",
            details=f"session={sanitized} run={run_id} mode={'append' if mode == 'a' else 'create'} path={path}",
        ))
        return run_id

    @staticmethod
    def _scan_existing(path: Path) -> tuple:
        """
        Return (max_run_id, row_count, header_matches_current_schema).

        A non-matching header means this file predates the unified schema
        (or belongs to something else entirely) — open_session() must not
        append rows of a different shape onto it.
        """
        max_run_id = 0
        row_count = 0
        header_ok = False
        try:
            with open(path, "r", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                header_ok = header == LogManager.CSV_HEADER
                if not header_ok:
                    return 0, 0, False
                run_id_idx = LogManager.CSV_HEADER.index("run_id")
                for row in reader:
                    row_count += 1
                    if len(row) > run_id_idx:
                        try:
                            max_run_id = max(max_run_id, int(row[run_id_idx]))
                        except ValueError:
                            pass
        except OSError:
            return 0, 0, False
        return max_run_id, row_count, header_ok

    def _close_named_csv(self) -> None:
        """Close the current sink (named or unnamed) before opening a new one."""
        self.close()

    # ------------------------------------------------------------------
    # Management
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Clear the display buffer (CSV file is NOT truncated — it keeps growing)."""
        self._buffer.clear()

    def export(self, filepath: str) -> Path:
        """
        Write the current buffer to a new CSV file.

        Returns the Path of the written file.
        """
        path = Path(filepath).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(self.CSV_HEADER)
            for entry in self._buffer:
                writer.writerow(self._entry_to_row(entry))
        return path

    def close(self) -> None:
        """Flush and close the CSV file."""
        if self._csv_file is not None:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _open_csv(self, log_dir: str) -> None:
        dir_path = Path(log_dir).expanduser().resolve()
        dir_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._csv_path = dir_path / f"session_{timestamp}.csv"
        self._csv_file = open(self._csv_path, "w", newline="", buffering=1)
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(self.CSV_HEADER)

    def _entry_to_row(self, entry: LogEntry) -> list:
        elapsed_s = ""
        if self._session_start_ts is not None and entry.run_id == self._run_id:
            elapsed_s = f"{entry.timestamp - self._session_start_ts:.3f}"
        return [
            entry.timestamp_iso,
            entry.timestamp_ms,
            elapsed_s,
            self._session,
            entry.run_id,
            entry.trial,
            entry.source,
            entry.direction,
            entry.node_id,
            entry.frame_type,
            entry.event_name,
            entry.raw_id_hex,
            entry.raw_data_hex,
            json.dumps(entry.fields, default=str) if entry.fields else "",
            entry.details,
        ]
