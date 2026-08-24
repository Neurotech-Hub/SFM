"""intervals.py — generic interval algebra for behavioral analysis.

Every "was X happening while Y" question a session log gets asked reduces
to one of these six functions. An interval is a plain ``(t0, t1)`` tuple
of run-relative seconds (the same ``t`` every RunData row and Bout/
FaultInterval/Cycle already carries) with ``t0 <= t1``; a set of intervals
is any ``Sequence`` of them, not necessarily sorted or merged.

Deliberately *not* method calls on Bout/FaultInterval: those objects can
be ``censored`` (``t1=None``, still open when the run ended), and this
module refuses to guess what a censored interval's end time should be —
see ``as_intervals`` below. Resolve that explicitly (usually to
``run.duration_s``) before handing data here, the same way
``metrics.bouts()`` already does internally.

Example::

    from sfm_analysis.analysis import intervals as iv

    presence, _ = m.presence                    # List[Bout]
    dome, _ = m.dome                             # List[Bout]
    presence_iv = iv.as_intervals(presence, run_end=run.duration_s)
    dome_iv = iv.as_intervals(dome, run_end=run.duration_s)

    # Dome opened while the mouse was present:
    len(iv.overlap(dome_iv, presence_iv))

    # Takes within 30s after a fault started:
    fault_iv = iv.as_intervals(m.faults, run_end=run.duration_s)
    take_times = [r.t for r in run.by_event("Pellet Taken")]
    iv.count_in(take_times, iv.around([f[0] for f in fault_iv], (0, 30)))
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

Interval = Tuple[float, float]


def as_intervals(objs: Sequence[Any], *, run_end: Optional[float] = None) -> List[Interval]:
    """
    Convert a sequence of Bout/FaultInterval-like objects (anything with
    ``.t0`` and ``.t1`` attributes) into plain ``(t0, t1)`` tuples.

    Raises ValueError on a censored object (``t1 is None``) unless
    ``run_end`` is given, in which case its ``t1`` becomes ``run_end`` —
    matching how the report's own ``metrics.bouts()`` resolves an
    unclosed bout. This is a required, explicit choice: silently treating
    a censored interval as zero-length or infinite would both be wrong,
    and differently wrong depending on the question being asked.
    """
    out: List[Interval] = []
    for obj in objs:
        t0 = float(obj.t0)
        t1 = obj.t1
        if t1 is None:
            if run_end is None:
                raise ValueError(
                    "censored interval (t1=None) with no run_end given — "
                    "pass run_end=run.duration_s to resolve it, or filter "
                    "out censored objects first if that's what you want"
                )
            t1 = run_end
        out.append((t0, float(t1)))
    return out


def _clip(a: Interval, b: Interval) -> Optional[Interval]:
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return (lo, hi) if lo < hi else None


def overlap(a: Sequence[Interval], b: Sequence[Interval]) -> List[Interval]:
    """Every sub-interval present in both ``a`` and ``b``."""
    out: List[Interval] = []
    for ia in a:
        for ib in b:
            clipped = _clip(ia, ib)
            if clipped is not None:
                out.append(clipped)
    return merge(out)


def subtract(a: Sequence[Interval], b: Sequence[Interval]) -> List[Interval]:
    """The parts of ``a`` not covered by any interval in ``b``."""
    b_merged = merge(b)
    out: List[Interval] = []
    for t0, t1 in a:
        cursor = t0
        for b0, b1 in b_merged:
            if b1 <= cursor or b0 >= t1:
                continue
            if b0 > cursor:
                out.append((cursor, min(b0, t1)))
            cursor = max(cursor, b1)
            if cursor >= t1:
                break
        if cursor < t1:
            out.append((cursor, t1))
    return out


def merge(intervals: Sequence[Interval], *, gap: float = 0.0) -> List[Interval]:
    """
    Merge overlapping (or, with ``gap > 0``, near-adjacent) intervals into
    the smallest equivalent set, sorted by start time.
    """
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    out: List[Interval] = [ordered[0]]
    for t0, t1 in ordered[1:]:
        last0, last1 = out[-1]
        if t0 <= last1 + gap:
            out[-1] = (last0, max(last1, t1))
        else:
            out.append((t0, t1))
    return out


def around(events: Sequence[float], window: Interval) -> List[Interval]:
    """
    A peri-event interval ``(t + window[0], t + window[1])`` around each
    event timestamp in ``events``. ``window`` is an offset pair, e.g.
    ``(-5, 30)`` for "5s before to 30s after".
    """
    lo, hi = window
    return [(t + lo, t + hi) for t in events]


def count_in(events: Sequence[float], intervals: Sequence[Interval]) -> int:
    """How many of ``events`` fall inside any of ``intervals`` (inclusive)."""
    merged = merge(intervals)
    return sum(1 for t in events if any(t0 <= t <= t1 for t0, t1 in merged))


def rate_in(events: Sequence[float], intervals: Sequence[Interval]) -> Optional[float]:
    """Events per second, over the total (merged) duration of ``intervals``.

    None if the total duration is zero, rather than raising or returning
    an infinite rate.
    """
    merged = merge(intervals)
    total = sum(t1 - t0 for t0, t1 in merged)
    if total <= 0:
        return None
    return count_in(events, merged) / total
