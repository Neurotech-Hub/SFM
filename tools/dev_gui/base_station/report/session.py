"""session.py — group LogRow/HeartbeatRow into per-run behavioral records.

A single session CSV can hold multiple *runs*: reopening a session name
appends to the same file and increments ``run_id`` (see
log_manager.py:299-359). Every timing computation must be scoped to one
``(session, run_id)`` — a cumulative count, an inter-trial interval, or a
"time since session start" that crosses a run boundary is meaningless.

``split_runs`` is the single place that:
  - groups rows by ``(session, run_id)``
  - stable-sorts each group by ``(timestamp_ms, idx)`` (a few rows in real
    logs are written out of strict time order — see loader.py)
  - recomputes ``t`` (run-relative seconds) from ``timestamp_ms``, ignoring
    the CSV's own ``elapsed_s`` column, which resets per run and cannot be
    trusted across an appended file
  - locates the ``session_start`` / ``<experiment>_start`` / ``session_end``
    EXPERIMENT rows and copies their fields onto the RunData
  - flags every row after ``session_end`` as ``post_session=True``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .loader import HeartbeatRow, LogRow


@dataclass
class RunData:
    """All rows belonging to one ``(session, run_id)``."""

    session: str
    run_id: int
    source_path: Path
    rows: List[LogRow] = field(default_factory=list)
    heartbeats: List[HeartbeatRow] = field(default_factory=list)
    t0_ms: int = 0
    t_end_ms: int = 0
    experiment: str = "unknown"
    nodes: List[int] = field(default_factory=list)
    seed: Optional[int] = None
    params: Dict[str, Any] = field(default_factory=dict)     # <experiment>_start fields
    end_fields: Dict[str, Any] = field(default_factory=dict)  # session_end fields
    end_reason: Optional[str] = None
    has_session_end: bool = False
    notes: List[str] = field(default_factory=list)            # data-quality notes, surfaced verbatim

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.t_end_ms - self.t0_ms) / 1000.0)

    @property
    def run_label(self) -> str:
        return f"{self.session} · run {self.run_id}"

    def by_event(self, *names: str) -> List[LogRow]:
        """CAN EVENT rows (source == CAN, frame_type == EVENT) matching event_name."""
        wanted = set(names)
        return [r for r in self.rows if r.frame_type == "EVENT" and r.event_name in wanted]

    def by_node(self, node: int) -> List[LogRow]:
        return [r for r in self.rows if r.node_id == node]

    def exp(self, *names: str) -> List[LogRow]:
        """EXPERIMENT rows (source == EXP) matching event_name, in time order."""
        wanted = set(names)
        return [r for r in self.rows if r.source == "EXP" and r.event_name in wanted]

    def can_events(self, node: Optional[int] = None) -> List[LogRow]:
        """CAN EVENT rows, optionally scoped to one node."""
        out = [r for r in self.rows if r.frame_type == "EVENT"]
        if node is not None:
            out = [r for r in out if r.node_id == node]
        return out


def _stable_sort_key(row: LogRow) -> Tuple[int, int]:
    return (row.ts_ms, row.idx)


def split_runs(
    rows: List[LogRow],
    heartbeats: List[HeartbeatRow],
    source_path: Path,
) -> List[RunData]:
    """
    Group rows into one RunData per ``(session, run_id)``, time-sorted.

    Rows with ``run_id == 0`` (the daily ``session_YYYYMMDD`` activity log,
    or rows from a tool that never called ``open_session``) are grouped
    together under ``run_id=0`` rather than dropped, so nothing silently
    disappears; the caller decides whether to report on them.
    """
    groups: Dict[Tuple[str, int], List[LogRow]] = {}
    for row in rows:
        key = (row.session, row.run_id)
        groups.setdefault(key, []).append(row)

    runs: List[RunData] = []
    for (session, run_id), group_rows in groups.items():
        group_rows = sorted(group_rows, key=_stable_sort_key)
        t0_ms = group_rows[0].ts_ms
        t_end_ms = group_rows[-1].ts_ms

        run = RunData(
            session=session,
            run_id=run_id,
            source_path=source_path,
            t0_ms=t0_ms,
            t_end_ms=t_end_ms,
        )

        last_ms = None
        for row in group_rows:
            if last_ms is not None and row.ts_ms < last_ms:
                # Clamp monotone so downstream duration math never goes
                # negative; the raw ts_ms is preserved on the row itself.
                row.t = run.rows[-1].t if run.rows else 0.0
            else:
                row.t = (row.ts_ms - t0_ms) / 1000.0
            last_ms = max(last_ms or row.ts_ms, row.ts_ms)
            run.rows.append(row)

            if row.source == "EXP":
                if row.event_name == "session_start":
                    exp_name = row.fields.get("experiment")
                    if isinstance(exp_name, str) and exp_name:
                        run.experiment = exp_name
                    nodes = row.fields.get("nodes")
                    if isinstance(nodes, list):
                        run.nodes = [int(n) for n in nodes if isinstance(n, (int, float))]
                    seed = row.fields.get("seed")
                    if isinstance(seed, (int, float)):
                        run.seed = int(seed)
                elif row.event_name.endswith("_start") and row.event_name != "session_start":
                    run.params = dict(row.fields)
                elif row.event_name == "session_end":
                    run.end_fields = dict(row.fields)
                    reason = row.fields.get("reason")
                    run.end_reason = str(reason) if reason else None
                    run.has_session_end = True

        # Flag rows after session_end — needs its position, known only
        # once the run has been fully scanned above.
        end_idx = None
        for i, row in enumerate(run.rows):
            if row.source == "EXP" and row.event_name == "session_end":
                end_idx = i
                break
        if end_idx is not None:
            for row in run.rows[end_idx + 1:]:
                row.post_session = True

        if not run.has_session_end:
            run.notes.append(
                "No session_end row found — session may have been interrupted "
                "(power loss, crash, or the operator closed the GUI without Stop)."
            )

        runs.append(run)

    runs.sort(key=lambda r: (r.session, r.run_id))

    # Assign each heartbeat to the run whose [t0_ms, next_run.t0_ms) window
    # (same session, open-ended for the last run) contains it. Using this
    # run's *last main row* as the upper bound instead would drop
    # heartbeats that keep arriving after the last CAN/EXP row was logged
    # — which happens routinely, since heartbeats are periodic and
    # independent of behavioral traffic.
    runs_by_session: Dict[str, List[RunData]] = {}
    for run in runs:
        runs_by_session.setdefault(run.session, []).append(run)

    for session_runs in runs_by_session.values():
        session_runs.sort(key=lambda r: r.t0_ms)
        for i, run in enumerate(session_runs):
            upper = session_runs[i + 1].t0_ms if i + 1 < len(session_runs) else None
            for hb in heartbeats:
                if hb.ts_ms < run.t0_ms:
                    continue
                if upper is not None and hb.ts_ms >= upper:
                    continue
                hb.t = (hb.ts_ms - run.t0_ms) / 1000.0
                run.heartbeats.append(hb)
            run.heartbeats.sort(key=lambda h: h.ts_ms)

    return runs
