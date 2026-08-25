"""
log_manager.py — In-memory event ring buffer with CSV auto-save.

Every CAN frame (received or sent), experiment row, BNC edge, and sync marker
is recorded as a LogEntry. The display buffer is a fixed-size deque (ring
buffer); the CSV file grows unbounded for the session and is never truncated.

Two kinds of CSV sink exist:

  - The daily activity sink opened by the constructor (and again after
    an experiment ends) — ``session_YYYYMMDD.csv``. One file per calendar
    day; later GUI sessions on the same day append. Catches discovery,
    registry, manual commands, and post-experiment traffic. Rows are
    stamped ``run_id=0``.
  - The named, append-on-reuse sink opened by ``open_session()`` — one
    file per experiment session name, carrying a ``run_id`` that increments
    each time the same name is reopened (in this process or a prior one).
    ``resume_daily()`` closes that file at experiment end so it stays
    experiment-only (last row is ``session_end``).

HEARTBEAT rows are always diverted to a sibling file
(``session_YYYYMMDD_heartbeats.csv`` / ``{session}_heartbeats.csv``) so the
main CSV stays event/command/experiment traffic only. Heartbeats still
enter the in-memory ring (hidden in the GUI by default).

``open_session()`` closes whichever sink is currently open and switches to
the named one; it is not a second, parallel writer, so daily traffic is
never merged into the experiment file.

Performance note:
  At 9 nodes × ~1/60 Hz heartbeat + events the CSV load is light.
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

from sfm_analysis.logs import CSV_HEADER as _CSV_HEADER
from sfm_analysis.logs import heartbeat_path_for as _heartbeat_path_for
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
        lm.add(LogEntry(...))                       # goes to today's session_YYYYMMDD.csv
        run_id = lm.open_session("cohortA_day3", "~/sfm_logs")
        lm.set_context(session="cohortA_day3", run_id=run_id, session_start_ts=time.time())
        lm.add(LogEntry(...))                       # goes to the named sink, stamped with run_id
        lm.resume_daily()                           # close experiment file; back to daily
        entries = lm.get_filtered(show_heartbeats=False)
        lm.export("~/Desktop/my_session.csv")
    """

    # The canonical 15-column schema now lives in sfm_analysis.logs so that
    # readers (the sfm-analysis report SDK) and this writer cannot drift
    # apart. Bound as a class attribute so LogManager.CSV_HEADER keeps
    # working for every existing caller.
    CSV_HEADER = _CSV_HEADER

    def __init__(
        self,
        max_entries: int = 1000,
        log_dir: str = "~/sfm_logs",
        auto_save: bool = True,
    ) -> None:
        self._max_entries = max_entries
        self._buffer: Deque[LogEntry] = deque(maxlen=max_entries)
        self._auto_save = auto_save
        self._log_dir = log_dir
        self._csv_path: Optional[Path] = None
        self._csv_file = None
        self._csv_writer = None
        self._hb_csv_path: Optional[Path] = None
        self._hb_csv_file = None
        self._hb_csv_writer = None

        # "daily" | "named" | None — which CSV pair is currently open.
        self._sink_kind: Optional[str] = None
        self._daily_date: Optional[str] = None  # YYYYMMDD of the open daily sink

        # Named-session context, applied to any entry that doesn't already
        # carry its own non-default value (see add()).
        self._session: str = ""
        self._run_id: int = 0
        self._trial: int = 0
        self._session_start_ts: Optional[float] = None

        if auto_save:
            self._open_daily(log_dir)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(self, entry: LogEntry) -> None:
        """
        Append an entry to the ring buffer and (optionally) CSV.

        HEARTBEAT rows go only to the sibling ``*_heartbeats.csv`` sink.
        All other types write to the main session CSV.

        Writes whenever a CSV sink is open — the constructor's ``auto_save``
        flag only controls whether the daily sink opens at construction; an
        explicit ``open_session()`` call always opens a writer, since naming
        a session is an explicit request to record it regardless of that
        constructor flag.
        """
        if self._sink_kind == "daily":
            self._rollover_daily_if_needed()
        self._stamp_context(entry)
        self._buffer.append(entry)
        if entry.frame_type == "HEARTBEAT":
            if self._hb_csv_writer is not None:
                self._hb_csv_writer.writerow(self._entry_to_row(entry))
                self._hb_csv_file.flush()
            return
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
    def heartbeat_csv_path(self) -> Optional[Path]:
        return self._hb_csv_path

    @property
    def session_name(self) -> str:
        return self._session

    @property
    def run_id(self) -> int:
        return self._run_id

    @property
    def is_named_session(self) -> bool:
        """True while the named experiment CSV is the active writer."""
        return self._sink_kind == "named" and self._csv_writer is not None

    # Also sourced from sfm_analysis.logs, for the same reason as
    # CSV_HEADER above. staticmethod() is required here: without it,
    # self.heartbeat_path_for(path) would pass self as main_path.
    heartbeat_path_for = staticmethod(_heartbeat_path_for)

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

        Closes whatever sink is currently open first — today's
        ``session_YYYYMMDD.csv`` opened by the constructor / ``resume_daily()``
        (daily traffic is never merged into the named file) or a
        previously-opened named sink. If a file for this name already
        exists with the current header, opens it for append and
        returns one past the highest ``run_id`` found in it; otherwise
        creates a fresh file with ``run_id = 1``. If a file of this name
        exists but is a stale/foreign format, diverts to a timestamped
        filename rather than corrupting or overwriting it.

        Returns the run_id that new entries will be stamped with.
        """
        self.close()

        sanitized = sanitize_session_name(session_name)
        if not sanitized:
            raise ValueError("session_name sanitizes to empty — refusing to open a session")

        self._log_dir = log_dir
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
        self._open_heartbeat_csv(self.heartbeat_path_for(path), mode)

        self._sink_kind = "named"
        self._daily_date = None
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

    def resume_daily(self, log_dir: Optional[str] = None) -> None:
        """
        Close the named experiment sink (if open) and append to today's
        ``session_YYYYMMDD.csv``. No-op when already writing that daily file.

        Call after ``session_end`` has been logged so the experiment CSV
        stays experiment-only. Subsequent CAN/commands/heartbeats go to
        the daily pair.
        """
        if log_dir is not None:
            self._log_dir = log_dir
        today = self._today_stamp()
        if (
            self._sink_kind == "daily"
            and self._daily_date == today
            and self._csv_writer is not None
        ):
            return
        self._open_daily(self._log_dir)

    def _close_named_csv(self) -> None:
        """Close the current sink (named or daily) before opening a new one."""
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
        """Flush and close the main and heartbeat CSV files."""
        if self._csv_file is not None:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None
        if self._hb_csv_file is not None:
            self._hb_csv_file.flush()
            self._hb_csv_file.close()
            self._hb_csv_file = None
            self._hb_csv_writer = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _today_stamp(self) -> str:
        return datetime.now().strftime("%Y%m%d")

    def _daily_stem(self, stamp: Optional[str] = None) -> str:
        return f"session_{stamp or self._today_stamp()}"

    def _rollover_daily_if_needed(self) -> None:
        """If the calendar day changed while on the daily sink, open today's file."""
        today = self._today_stamp()
        if self._daily_date == today:
            return
        self._open_daily(self._log_dir)

    def _open_daily(self, log_dir: Optional[str] = None) -> None:
        """Open (or append) ``session_YYYYMMDD.csv`` and its heartbeat sibling."""
        self.close()
        if log_dir is not None:
            self._log_dir = log_dir
        dir_path = Path(self._log_dir).expanduser().resolve()
        dir_path.mkdir(parents=True, exist_ok=True)
        stamp = self._today_stamp()
        stem = self._daily_stem(stamp)
        path = dir_path / f"{stem}.csv"

        mode = "w"
        if path.exists():
            _max_run_id, _row_count, header_ok = self._scan_existing(path)
            if header_ok:
                mode = "a"
            else:
                path = dir_path / f"{stem}_{datetime.now().strftime('%H%M%S')}.csv"
                mode = "w"

        self._csv_path = path
        self._csv_file = open(path, mode, newline="", buffering=1)
        self._csv_writer = csv.writer(self._csv_file)
        if mode == "w":
            self._csv_writer.writerow(self.CSV_HEADER)
        self._open_heartbeat_csv(self.heartbeat_path_for(path), mode)

        self._sink_kind = "daily"
        self._daily_date = stamp
        self._session = stem
        self._run_id = 0
        self._trial = 0
        self._session_start_ts = None

    def _open_heartbeat_csv(self, path: Path, mode: str) -> None:
        """
        Open the sibling heartbeat sink.

        For append mode, if an existing file has a foreign/stale header,
        fall back to create (truncate) rather than mixing schemas — heartbeats
        are diagnostic and safer to reset than to corrupt.
        """
        write_header = mode == "w"
        if mode == "a" and path.exists():
            _max_run, _count, header_ok = self._scan_existing(path)
            if not header_ok:
                mode = "w"
                write_header = True
        elif mode == "a" and not path.exists():
            mode = "w"
            write_header = True

        self._hb_csv_path = path
        self._hb_csv_file = open(path, mode, newline="", buffering=1)
        self._hb_csv_writer = csv.writer(self._hb_csv_file)
        if write_header:
            self._hb_csv_writer.writerow(self.CSV_HEADER)

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
