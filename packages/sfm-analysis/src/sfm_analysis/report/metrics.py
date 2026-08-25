"""metrics.py — generic derived behavior metrics, computed from a RunData.

Everything here reads a single RunData and returns plain dataclasses; no
HTML is produced (that is sections/*.py's job — see schema.py). Splitting
metrics from rendering means the science is testable without parsing HTML,
and is also what a future ``--json`` export or an R/Python analysis script
consumes directly.

Central object: the dispense cycle (``Cycle``). Every timing metric in the
report — approach latency, dome latency, retrieval latency, handling time —
is a difference between two Cycle timestamps. See ``build_cycles``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Dict, List, Optional, Sequence, Tuple

from ..protocol import CanEvent, ServiceStatus
from .loader import LogRow
from .session import RunData
from .stats import iqr, median, safe_ratio
from .timezones import local_date, time_of_day

# CAN EVENT display names that mark a dispense-cycle phase (see
# protocol.CAN_EVENT_DISPLAY_NAME). Keyed by the *display* name since
# that's what's written to event_name; the underlying CanEvent still comes
# from LogRow.can_event where disambiguation matters (e.g. "Dome Opened").
_PHASE_FIELDS = {
    "Lowering": "lowering_t",
    "Loading": "loading_t",
    "Pellet OnPlate": "on_plate_t",
    "Raising": "raising_t",
    "Seeking": "seeking_t",
    "Dwelling": "dwelling_t",
}


def _is_dispense_command(row: LogRow) -> Optional[bool]:
    """
    True if `row` is a Dispense command that actually ran, False if it's a
    DispenseNoFeed that actually ran, None if it's not a (successful)
    dispense command at all (a veto like "... skipped ..." never starts
    motion, so it must not open a cycle).
    """
    if row.frame_type != "COMMAND":
        return None
    name = row.event_name
    if "skipped" in name.lower():
        return None
    if name.startswith("DispenseNoFeed"):
        return False
    if name.startswith("Dispense"):
        return True
    return None


@dataclass
class Cycle:
    """One dispense-and-retrieve cycle on one node."""

    node: int
    trial: int
    index: int                         # 0-based cycle index within this node's run
    cmd_t: Optional[float]             # None = cycle inferred from a Loaded with no preceding command
    fed: bool                          # True = Dispense (real pellet), False = DispenseNoFeed
    lowering_t: Optional[float] = None
    loading_t: Optional[float] = None
    on_plate_t: Optional[float] = None
    raising_t: Optional[float] = None
    seeking_t: Optional[float] = None
    dwelling_t: Optional[float] = None
    ready_t: Optional[float] = None        # Loaded (fed) or NoFeedPresented (no-feed)
    first_presence_t: Optional[float] = None
    first_dome_t: Optional[float] = None
    taken_t: Optional[float] = None
    end_t: float = 0.0
    censored: bool = False             # still open when the run ended, no outcome observed

    @property
    def cycle_duration(self) -> Optional[float]:
        if self.cmd_t is None or self.ready_t is None:
            return None
        return self.ready_t - self.cmd_t

    @property
    def approach_latency(self) -> Optional[float]:
        if self.ready_t is None or self.first_presence_t is None or self.first_presence_t < self.ready_t:
            return None
        return self.first_presence_t - self.ready_t

    @property
    def dome_latency(self) -> Optional[float]:
        if self.ready_t is None or self.first_dome_t is None or self.first_dome_t < self.ready_t:
            return None
        return self.first_dome_t - self.ready_t

    @property
    def handling_time(self) -> Optional[float]:
        if self.first_dome_t is None or self.taken_t is None or self.taken_t < self.first_dome_t:
            return None
        return self.taken_t - self.first_dome_t

    @property
    def retrieval_latency(self) -> Optional[float]:
        if self.ready_t is None or self.taken_t is None or self.taken_t < self.ready_t:
            return None
        return self.taken_t - self.ready_t

    @property
    def approached_without_dome(self) -> bool:
        return self.first_presence_t is not None and self.first_dome_t is None

    @property
    def dome_without_take(self) -> bool:
        return self.first_dome_t is not None and self.taken_t is None


def build_cycles(run: RunData) -> List[Cycle]:
    """
    Reconstruct every dispense cycle on every node in `run`.

    A cycle opens on a Dispense/DispenseNoFeed command that actually ran
    (source EXP or CAN, frame_type COMMAND — manual GUI dispenses are
    source=CAN and still open a cycle, just with no EXP fields_json), and
    closes on Pellet Taken, on the next dispense command for that node
    (superseded), or at run end (censored=True). A broadcast command
    (node_id == 0) opens one pending cycle per node named in `run.nodes`,
    since it targets all of them at once.

    A ``Loaded``/``NoFeedPresented`` event with no preceding command for
    that node (e.g. a very first cycle whose command row predates this
    run's window) still opens a cycle, with ``cmd_t=None`` — its
    ``cycle_duration`` is then undefined but its retrieval-side timings
    are still meaningful.
    """
    nodes = sorted(set(run.nodes) | {r.node_id for r in run.rows if r.node_id != 0})
    end_t = max((r.t for r in run.rows), default=run.duration_s)

    cycles: List[Cycle] = []
    for node in nodes:
        open_cycle: Optional[Cycle] = None
        idx = 0
        node_rows = [r for r in run.rows if r.node_id in (node, 0) and not r.post_session]
        node_rows.sort(key=lambda r: r.t)

        def close(cycle: Cycle, at_t: float, censored: bool) -> None:
            cycle.end_t = at_t
            cycle.censored = censored
            cycles.append(cycle)

        for row in node_rows:
            is_fed = _is_dispense_command(row)
            if is_fed is not None:
                if open_cycle is not None:
                    close(open_cycle, row.t, censored=False)
                open_cycle = Cycle(
                    node=node, trial=row.trial, index=idx, cmd_t=row.t, fed=is_fed,
                )
                idx += 1
                continue

            if row.frame_type != "EVENT":
                continue

            if row.event_name in _PHASE_FIELDS:
                target = row.event_name
                if open_cycle is None:
                    # A phase event with no open cycle (e.g. manual GUI
                    # dispense whose COMMAND row wasn't captured) still
                    # starts an inferred cycle so retrieval timing is not lost.
                    open_cycle = Cycle(node=node, trial=row.trial, index=idx, cmd_t=None, fed=True)
                    idx += 1
                setattr(open_cycle, _PHASE_FIELDS[target], row.t)
                continue

            if row.event_name == "Loaded":
                if open_cycle is None:
                    open_cycle = Cycle(node=node, trial=row.trial, index=idx, cmd_t=None, fed=True)
                    idx += 1
                if open_cycle.ready_t is None:
                    open_cycle.ready_t = row.t
                continue

            if row.event_name == "NoFeedPresented":
                if open_cycle is None:
                    open_cycle = Cycle(node=node, trial=row.trial, index=idx, cmd_t=None, fed=False)
                    idx += 1
                if open_cycle.ready_t is None:
                    open_cycle.ready_t = row.t
                continue

            if open_cycle is None:
                continue  # presence/dome/take outside any known cycle isn't attributable

            if row.event_name == "MousePresence Detected" and open_cycle.first_presence_t is None:
                open_cycle.first_presence_t = row.t
            elif row.event_name == "Dome Opened" and open_cycle.first_dome_t is None:
                open_cycle.first_dome_t = row.t
            elif row.event_name == "Pellet Taken":
                open_cycle.taken_t = row.t
                close(open_cycle, row.t, censored=False)
                open_cycle = None

        if open_cycle is not None:
            close(open_cycle, end_t, censored=True)

    cycles.sort(key=lambda c: (c.node, c.index))
    return cycles


@dataclass
class Bout:
    node: int
    t0: float
    t1: Optional[float]
    censored: bool = False

    @property
    def dur(self) -> Optional[float]:
        if self.t1 is None:
            return None
        return self.t1 - self.t0


@dataclass
class BoutIssues:
    orphan_closes: int = 0
    duplicate_opens: int = 0
    censored: int = 0


def bouts(
    rows: Sequence[LogRow],
    open_name: str,
    close_name: str,
    run_end_t: float,
) -> Tuple[List[Bout], BoutIssues]:
    """
    Pair open/close events into bouts, per node, in time order.

    Generic helper used for presence bouts (Detected/Cleared), dome bouts,
    and (via a different open/close pair) fault intervals. Never silently
    drops an unpaired edge — every anomaly increments a counter on the
    returned BoutIssues so a report's Data Quality section can surface it.
    """
    issues = BoutIssues()
    open_at: Dict[int, float] = {}
    out: List[Bout] = []

    ordered = sorted(rows, key=lambda r: r.t)
    for row in ordered:
        if row.event_name == open_name:
            if row.node_id in open_at:
                issues.duplicate_opens += 1
                continue  # keep the first open, ignore the redundant one
            open_at[row.node_id] = row.t
        elif row.event_name == close_name:
            if row.node_id not in open_at:
                issues.orphan_closes += 1
                continue
            t0 = open_at.pop(row.node_id)
            out.append(Bout(node=row.node_id, t0=t0, t1=row.t, censored=False))

    for node, t0 in open_at.items():
        issues.censored += 1
        out.append(Bout(node=node, t0=t0, t1=run_end_t, censored=True))

    out.sort(key=lambda b: (b.node, b.t0))
    return out, issues


def presence_bouts(run: RunData) -> Tuple[List[Bout], BoutIssues]:
    rows = run.by_event("MousePresence Detected", "MousePresence Cleared")
    end_t = max((r.t for r in run.rows), default=run.duration_s)
    return bouts(rows, "MousePresence Detected", "MousePresence Cleared", end_t)


def dome_bouts(run: RunData, dedup_window_s: float = 0.25) -> Tuple[List[Bout], BoutIssues]:
    """
    Dome-open bouts, deduplicating the CanEvent.DomeOpened milestone against
    the InputChanged dome edge when both name themselves "Dome Opened" for
    the same node within `dedup_window_s` (see loader.py's LogRow.can_event
    docstring — these are two different wire events for the same physical
    open, not two opens).
    """
    raw_opens = [r for r in run.rows if r.frame_type == "EVENT" and r.event_name == "Dome Opened"]
    closes = [r for r in run.rows if r.frame_type == "EVENT" and r.event_name == "Dome closed"]

    deduped: List[LogRow] = []
    last_by_node: Dict[int, float] = {}
    for r in sorted(raw_opens, key=lambda r: r.t):
        last_t = last_by_node.get(r.node_id)
        if last_t is not None and (r.t - last_t) <= dedup_window_s:
            continue  # same physical opening already counted
        last_by_node[r.node_id] = r.t
        deduped.append(r)

    end_t = max((r.t for r in run.rows), default=run.duration_s)
    return bouts(deduped + closes, "Dome Opened", "Dome closed", end_t)


@dataclass
class FaultInterval:
    node: int
    code: ServiceStatus
    t0: float
    t1: Optional[float]
    censored: bool = False
    inferred_close: bool = False

    @property
    def dur(self) -> Optional[float]:
        if self.t1 is None:
            return None
        return self.t1 - self.t0


def fault_intervals(run: RunData) -> List[FaultInterval]:
    """
    Pair Fault events with the next Recovered/node_recovered for that node,
    or infer a close at the node's next successful Loaded if no explicit
    recovery is logged.
    """
    open_faults: Dict[int, Tuple[float, ServiceStatus]] = {}
    out: List[FaultInterval] = []
    end_t = max((r.t for r in run.rows), default=run.duration_s)

    ordered = sorted(run.rows, key=lambda r: r.t)
    for row in ordered:
        if row.frame_type == "EVENT" and row.event_name.startswith("Fault:"):
            code = ServiceStatus.Ok
            if row.raw_data and len(row.raw_data) >= 2:
                try:
                    code = ServiceStatus(row.raw_data[1])
                except ValueError:
                    pass
            open_faults[row.node_id] = (row.t, code)
            continue
        if row.source == "EXP" and row.event_name in ("fault", "node_halted"):
            node = int(row.fields.get("node", row.node_id) or row.node_id)
            code_val = row.fields.get("fault_code")
            try:
                code = ServiceStatus(int(code_val)) if code_val is not None else ServiceStatus.Ok
            except (ValueError, TypeError):
                code = ServiceStatus.Ok
            open_faults.setdefault(node, (row.t, code))
            continue
        if row.source == "EXP" and row.event_name in ("recovered", "node_recovered"):
            node = int(row.fields.get("node", row.node_id) or row.node_id)
            if node in open_faults:
                t0, code = open_faults.pop(node)
                out.append(FaultInterval(node=node, code=code, t0=t0, t1=row.t))
            continue
        if row.frame_type == "EVENT" and row.event_name == "Loaded" and row.node_id in open_faults:
            t0, code = open_faults.pop(row.node_id)
            out.append(FaultInterval(node=row.node_id, code=code, t0=t0, t1=row.t, inferred_close=True))

    for node, (t0, code) in open_faults.items():
        out.append(FaultInterval(node=node, code=code, t0=t0, t1=end_t, censored=True))

    out.sort(key=lambda f: (f.node, f.t0))
    return out


@dataclass
class PelletAccounting:
    node: int
    presented: int = 0           # rows actually seen in the log
    no_feed_presented: int = 0
    taken: int = 0               # rows actually seen in the log
    feed_skipped: int = 0
    hb_presented_delta: Optional[int] = None
    hb_taken_delta: Optional[int] = None
    # Base-station ledger totals (pellet_ledger.py), gap-corrected against the
    # node counter. None for logs written before the ledger existed.
    ledger_presented: Optional[int] = None
    ledger_taken: Optional[int] = None
    missed_frames: int = 0       # event frames the ledger proved were dropped
    counter_restarts: int = 0    # node power-cycles seen mid-run

    @property
    def presented_total(self) -> int:
        """Best available count: the ledger when present, else counted rows."""
        return self.presented if self.ledger_presented is None else self.ledger_presented

    @property
    def taken_total(self) -> int:
        return self.taken if self.ledger_taken is None else self.ledger_taken

    @property
    def take_rate(self) -> Optional[float]:
        return safe_ratio(self.taken_total, self.presented_total)

    @property
    def bus_loss_presented(self) -> Optional[int]:
        if self.hb_presented_delta is None:
            return None
        return self.hb_presented_delta - self.presented

    @property
    def bus_loss_taken(self) -> Optional[int]:
        if self.hb_taken_delta is None:
            return None
        return self.hb_taken_delta - self.taken


def pellet_accounting(run: RunData) -> Dict[int, PelletAccounting]:
    """Per-node pellet counts, cross-checked against heartbeat counters."""
    out: Dict[int, PelletAccounting] = {}

    def acct(node: int) -> PelletAccounting:
        return out.setdefault(node, PelletAccounting(node=node))

    for row in run.by_event("Loaded"):
        acct(row.node_id).presented += 1
    for row in run.by_event("NoFeedPresented"):
        acct(row.node_id).no_feed_presented += 1
    for row in run.by_event("Pellet Taken"):
        acct(row.node_id).taken += 1
    for row in run.by_event("FeedSkipped"):
        acct(row.node_id).feed_skipped += 1
    for row in run.exp("feed_skipped"):
        node = row.fields.get("node")
        if isinstance(node, (int, float)):
            acct(int(node)).feed_skipped += 1

    # Base-station ledger. Every row that carried a node counter also carries
    # the gap-corrected session total, which is monotonic within a run — so the
    # high-water mark is the run total, including pellets whose own event frame
    # never arrived and that row counting above therefore cannot see.
    for row in run.rows:
        if not row.node_id:
            continue
        a = acct(row.node_id)
        sp = row.fields.get("session_pellets")
        if isinstance(sp, (int, float)):
            a.ledger_presented = max(a.ledger_presented or 0, int(sp))
        st = row.fields.get("session_taken")
        if isinstance(st, (int, float)):
            a.ledger_taken = max(a.ledger_taken or 0, int(st))
        if row.frame_type == "PELLET_AUDIT":
            missed = row.fields.get("missed_frames")
            if isinstance(missed, (int, float)):
                a.missed_frames += int(missed)
            if row.fields.get("node_counter_restarted"):
                a.counter_restarts += 1

    hb_by_node: Dict[int, List] = {}
    for hb in run.heartbeats:
        hb_by_node.setdefault(hb.node_id, []).append(hb)
    for node, hbs in hb_by_node.items():
        hbs.sort(key=lambda h: h.t)
        if len(hbs) >= 2:
            a = acct(node)
            a.hb_presented_delta = hbs[-1].pellets_presented - hbs[0].pellets_presented
            a.hb_taken_delta = hbs[-1].pellets_taken - hbs[0].pellets_taken

    return out


@dataclass
class InteractionFunnel:
    node: int
    presented: int = 0
    approached: int = 0          # cycles with a presence bout while ready
    dome_opened: int = 0         # cycles where the dome opened while ready
    taken: int = 0
    approach_without_dome: int = 0
    dome_without_take: int = 0


def interaction_funnel(cycles: Sequence[Cycle]) -> Dict[int, InteractionFunnel]:
    """Per-node conversion funnel, built directly from the reconstructed cycles."""
    out: Dict[int, InteractionFunnel] = {}
    for c in cycles:
        if c.ready_t is None:
            continue  # never actually presented (dispense failed / vetoed)
        f = out.setdefault(c.node, InteractionFunnel(node=c.node))
        f.presented += 1
        if c.first_presence_t is not None:
            f.approached += 1
        if c.first_dome_t is not None:
            f.dome_opened += 1
        if c.taken_t is not None:
            f.taken += 1
        if c.approached_without_dome:
            f.approach_without_dome += 1
        if c.dome_without_take:
            f.dome_without_take += 1
    return out


@dataclass
class LatencySummary:
    n: int
    median: Optional[float]
    q1: Optional[float]
    q3: Optional[float]
    values: List[float] = field(default_factory=list)


def latency_summary(values: Sequence[Optional[float]]) -> LatencySummary:
    clean = [v for v in values if v is not None]
    bounds = iqr(clean)
    return LatencySummary(
        n=len(clean),
        median=median(clean),
        q1=bounds[0] if bounds else None,
        q3=bounds[1] if bounds else None,
        values=clean,
    )


# Apparatus/script health event names surfaced verbatim as counts — see
# experiment/kit.py, context.py, script.py for where each is logged.
APPARATUS_HEALTH_EVENTS = [
    "dispense_skipped_halted",
    "dispense_skipped_in_flight",
    "dispense_skipped_pellet_present",
    "broadcast_dispense_partial_pellet_present",
    "feed_skipped",
    "plate_occupied_wait",
    "bandit_plate_occupied_wait",
    "paused_for_fault",
    "resumed_after_fault",
    "script_stalled",
    "script_timeout",
    "callback_error",
    "timer_error",
    "start_when_error",
    "end_when_error",
    "stop_requested",
    "bandit_trial_aborted",
]


def apparatus_health(run: RunData) -> Dict[str, List[LogRow]]:
    """Rows for each named health/veto event, in time order, keyed by name."""
    out: Dict[str, List[LogRow]] = {}
    for name in APPARATUS_HEALTH_EVENTS:
        rows = run.exp(name)
        if rows:
            out[name] = sorted(rows, key=lambda r: r.t)
    return out


@dataclass
class ThroughputPoint:
    t: float
    presented: int
    taken: int


def cumulative_throughput(run: RunData) -> List[ThroughputPoint]:
    """Cumulative presented/taken counts over run time, one point per event."""
    events = []
    for r in run.by_event("Loaded"):
        events.append((r.t, "presented"))
    for r in run.by_event("Pellet Taken"):
        events.append((r.t, "taken"))
    events.sort(key=lambda e: e[0])

    out: List[ThroughputPoint] = []
    presented = taken = 0
    for t, kind in events:
        if kind == "presented":
            presented += 1
        else:
            taken += 1
        out.append(ThroughputPoint(t=t, presented=presented, taken=taken))
    return out


DEFAULT_ACTIVITY_EVENTS = ("MousePresence Detected",)


@dataclass
class ActogramDay:
    """One row of an actogram: a calendar day (rig-local) and the
    time-of-day (0-24h) of every activity event that fell on it."""

    date: _date
    times: List[float]


def activity_by_day(
    run: RunData,
    *,
    event_names: Sequence[str] = DEFAULT_ACTIVITY_EVENTS,
) -> List[ActogramDay]:
    """
    Group activity timestamps by calendar day, in the rig's own local
    time (see ``timezones.py`` for why that needs no UTC offset) — the
    data an actogram plots. Days are pooled across every node: an
    actogram represents one subject's overall activity, not a per-node
    breakdown.

    ``event_names`` selects which CAN EVENT rows count as "activity";
    the default is presence-sensor onsets, the one activity proxy
    available across every experiment type. A day with zero matching
    events is simply absent from the result, not an empty row — callers
    that want a fully populated calendar (e.g. to draw a blank row for a
    day with no data) should fill the gaps themselves from the min/max
    date present.

    Returned in chronological order.
    """
    by_day: Dict[_date, List[float]] = {}
    for row in run.by_event(*event_names):
        by_day.setdefault(local_date(row), []).append(time_of_day(row))

    return [ActogramDay(date=d, times=sorted(times)) for d, times in sorted(by_day.items())]


@dataclass
class RunMetrics:
    """Bundle of every generic metric for one run, computed once and reused
    across sections (and the eventual --json export)."""

    run: RunData
    cycles: List[Cycle]
    pellets: Dict[int, PelletAccounting]
    presence: Tuple[List[Bout], BoutIssues]
    dome: Tuple[List[Bout], BoutIssues]
    faults: List[FaultInterval]
    funnel: Dict[int, InteractionFunnel]
    health: Dict[str, List[LogRow]]
    throughput: List[ThroughputPoint]
    activity_by_day: List[ActogramDay]


def compute_run_metrics(run: RunData) -> RunMetrics:
    cycles = build_cycles(run)
    return RunMetrics(
        run=run,
        cycles=cycles,
        pellets=pellet_accounting(run),
        presence=presence_bouts(run),
        dome=dome_bouts(run),
        faults=fault_intervals(run),
        funnel=interaction_funnel(cycles),
        health=apparatus_health(run),
        throughput=cumulative_throughput(run),
        activity_by_day=activity_by_day(run),
    )
