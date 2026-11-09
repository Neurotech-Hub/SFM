"""time_axis.py — one time axis for every report chart that plots time.

Two readings of the same numbers, switched together by the report-wide
Relative / Absolute control (see render.py and style.py):

  - **Relative** is seconds since this run's own t=0 (session start, or
    the alignment origin a combined chart already applied). Labels stay
    short (``m:ss``, ``h:mm``, or ``hh:mm`` plus a ``day N`` line) and
    the tick step grows with the span, so a multi-day run does not pack
    dozens of labels onto one axis.
  - **Absolute** is the rig's own local wall clock, reconstructed by
    adding those seconds to the run's first ``timestamp_iso``. The
    arithmetic is naive on purpose: it never calls ``timestamp()``,
    ``fromtimestamp()``, or ``astimezone()``, so the viewer's timezone
    cannot leak in. See timezones.py for why that matters. The date is
    a second line, and only on the first tick and where a midnight is
    crossed, so a long timestamp does not have to fit on one line.

Tick *count* comes from the pixel width of the plot (``max_ticks_for``),
not from the length of the run. That is what keeps labels from
overlapping on a long experiment.

The interactive timeline (explorer_render.py) ports the same step
ladder and the same two-line rules into its canvas. This module is the
reference; the JS copy should change with it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Tuple

# Steps that land on a clock boundary: they divide a minute, an hour, or
# a day, up through a week. Past the ladder, callers double the step so
# a very long span still stays inside its tick budget.
_STEPS: Tuple[int, ...] = (
    1, 2, 5, 10, 15, 30,
    60, 120, 300, 600, 900, 1800,
    3600, 2 * 3600, 3 * 3600, 4 * 3600, 6 * 3600, 12 * 3600,
    86400, 2 * 86400, 3 * 86400, 7 * 86400,
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Rig-local naive timestamp, as written by the base station. An offset
# suffix, if one is present, is ignored rather than applied — applying it
# would be a timezone conversion, which this module does not do.
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
)


@dataclass(frozen=True)
class Tick:
    """One axis tick. ``t`` is seconds in the chart's own x domain.

    ``line2`` is empty when this tick does not carry a second line (the
    date, or ``day N``). Callers draw ``line2`` only when it is non-empty,
    directly under ``line1``.
    """

    t: float
    line1: str
    line2: str = ""


@dataclass(frozen=True)
class TimeBase:
    """The rig-local clock at t=0. ``t0_iso`` empty means absolute labels
    are unavailable (the chart keeps relative labels in both toggle modes)."""

    t0_iso: Optional[str] = None

    @classmethod
    def from_iso(cls, iso: Optional[str]) -> "TimeBase":
        if _parse_naive(iso) is None:
            return cls(None)
        return cls(str(iso).strip())

    @classmethod
    def from_run(cls, run: object) -> "TimeBase":
        """The first row's ``iso``. ``split_runs`` time-sorts rows, so
        row 0 is this run's t=0 — the same string the explorer uses."""
        rows = getattr(run, "rows", None) or []
        iso = rows[0].iso if rows else None
        return cls.from_iso(iso)

    def wall(self, t: float) -> Optional[datetime]:
        """Naive rig-local datetime at ``t`` seconds. None if unknown.

        ``timedelta`` on a naive datetime adds exactly that many seconds.
        It does not consult the host timezone and does not apply DST.
        """
        origin = _parse_naive(self.t0_iso)
        if origin is None:
            return None
        return origin + timedelta(seconds=float(t))


def max_ticks_for(px_width: float, min_label_px: float = 72) -> int:
    """How many x labels fit in ``px_width`` without colliding.

    72px is enough for a two-line label (``00:00`` over ``Oct 28``) at
    the chart's 10px font. The floor of 2 keeps a degenerate width from
    requesting zero ticks.
    """
    if px_width <= 0 or min_label_px <= 0:
        return 2
    return max(2, int(px_width // min_label_px))


def rel_ticks(d0: float, d1: float, max_ticks: int) -> List[Tick]:
    """Relative ticks on [d0, d1], labeled from t=0 — not from d0.

    A detail panel covering 2h–4h therefore reads ``2:00``, ``2:30``, …
    rather than restarting at ``0:00``. Positions are multiples of the
    step measured from zero, so adjacent panels share a grid.
    """
    if d1 < d0:
        d0, d1 = d1, d0
    span = d1 - d0
    if span <= 0 or max_ticks < 1:
        return [Tick(d0, _rel_line1(d0, step=60, mode="minute"), "")]

    # The label shape follows the window, not each tick: a 2h panel is
    # h:mm even at its first tick, and only a window longer than a day
    # grows a "day N" line. A window that starts before t=0 (event-aligned
    # overlay) stays in h:mm, because "day N" is counted from the session.
    if d0 >= -1e-6 and span > 86400:
        mode = "day"
    elif span >= 3600:
        mode = "hour"
    else:
        mode = "minute"

    def build(step: int) -> List[Tick]:
        times = _aligned_times(d0, d1, step)
        ticks: List[Tick] = []
        prev_day: Optional[int] = None
        for i, t in enumerate(times):
            if mode == "day":
                day = int(round(t)) // 86400 + 1
                show_day = i == 0 or day != prev_day
                prev_day = day
                line2 = f"day {day}" if show_day else ""
            else:
                line2 = ""
            ticks.append(Tick(t, _rel_line1(t, step=step, mode=mode), line2))
        return ticks

    return _fit(build, span, max_ticks)


def abs_ticks(base: TimeBase, d0: float, d1: float, max_ticks: int) -> List[Tick]:
    """Absolute ticks on rig-local clock boundaries inside [d0, d1].

    An empty list means absolute labels are unavailable (no parseable
    start time). Otherwise every tick falls on a second, minute, hour,
    or midnight boundary of the rig clock, and the step is coarse enough
    that ``len(result) <= max_ticks``.
    """
    origin = _parse_naive(base.t0_iso)
    if origin is None:
        return []
    if d1 < d0:
        d0, d1 = d1, d0
    span = d1 - d0
    if span <= 0 or max_ticks < 1:
        dt = origin + timedelta(seconds=d0)
        line1, line2 = _abs_lines(dt, step=60, show_date=True)
        return [Tick(d0, line1, line2)]

    def build(step: int) -> List[Tick]:
        start_dt = origin + timedelta(seconds=d0)
        end_dt = origin + timedelta(seconds=d1)
        cursor = _ceil_aligned(start_dt, step)
        found: List[Tuple[float, datetime]] = []
        guard = 0
        while cursor <= end_dt and guard < 10000:
            rel = (cursor - origin).total_seconds()
            if rel >= d0 - 1e-3:
                found.append((rel, cursor))
            cursor = cursor + timedelta(seconds=step)
            guard += 1
        if not found:
            found = [(d0, start_dt)]
        ticks: List[Tick] = []
        prev_date = None
        for i, (rel, dt) in enumerate(found):
            date = dt.date()
            show = i == 0 or date != prev_date
            line1, line2 = _abs_lines(dt, step=step, show_date=show)
            ticks.append(Tick(rel, line1, line2))
            prev_date = date
        return ticks

    return _fit(build, span, max_ticks)


def fmt_rel(t: float) -> str:
    """Full relative time for a tooltip or table cell: ``2h 05m``, ``1m 30s``, ``9s``.

    Zero seconds are dropped once the value is at least a minute, so a
    window caption reads ``2h 00m–4h 00m`` rather than ``2h 00m 00s``.
    """
    total = int(round(t))
    sign = "-" if total < 0 else ""
    a = abs(total)
    h, rem = divmod(a, 3600)
    m, s = divmod(rem, 60)
    if h and s:
        return f"{sign}{h}h {m:02d}m {s:02d}s"
    if h:
        return f"{sign}{h}h {m:02d}m"
    if m and s:
        return f"{sign}{m}m {s:02d}s"
    if m:
        return f"{sign}{m}m"
    return f"{sign}{s}s"


def fmt_abs(base: TimeBase, t: float, *, seconds: bool = True) -> str:
    """Full rig-local timestamp: ``Oct 28 14:05:09``. ``—`` if unknown.

    ``seconds=False`` drops a zero seconds field (``Oct 28 14:00``) for
    captions. A non-zero second is still shown, so the clock is not rounded
    away. The year is omitted; the session header already carries the date
    context, and the second label line on the axis repeats the month and day.
    """
    dt = base.wall(t)
    if dt is None:
        return "—"
    if dt.microsecond >= 500_000:
        dt = (dt + timedelta(seconds=1)).replace(microsecond=0)
    else:
        dt = dt.replace(microsecond=0)
    clock = f"{dt.hour:02d}:{dt.minute:02d}"
    if seconds or dt.second:
        clock = f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    return f"{_MONTHS[dt.month - 1]} {dt.day} {clock}"


def fmt_abs_range(base: TimeBase, t0: float, t1: float) -> str:
    """``Oct 28 14:00–16:00``, or with both dates when the range crosses midnight."""
    a = fmt_abs(base, t0, seconds=False)
    b = fmt_abs(base, t1, seconds=False)
    if a == "—" or b == "—":
        return "—"
    da, _, ca = a.rpartition(" ")
    db, _, cb = b.rpartition(" ")
    if da == db:
        return f"{da} {ca}–{cb}"
    return f"{a}–{b}"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _parse_naive(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    m = _ISO_RE.match(str(iso).strip())
    if not m:
        return None
    year, month, day, hh, mm, ss, frac = m.groups()
    micro = int((frac + "000000")[:6]) if frac else 0
    try:
        return datetime(int(year), int(month), int(day), int(hh), int(mm), int(ss), micro)
    except ValueError:
        return None


def _choose_step(span: float, max_ticks: int) -> int:
    # Gaps, not ticks: N labels need N-1 intervals, and both ends can land
    # on a boundary, so budgeting max_ticks-1 gaps keeps the count inside
    # max_ticks. _fit() still bumps the step if alignment overshoots.
    gaps = max(1, max_ticks - 1)
    raw = span / gaps if span > 0 else _STEPS[0]
    for s in _STEPS:
        if s >= raw:
            return s
    step = _STEPS[-1]
    while span / step > gaps:
        step *= 2
    return step


def _bump_step(step: int) -> int:
    for s in _STEPS:
        if s > step:
            return s
    return step * 2


def _fit(build, span: float, max_ticks: int) -> List[Tick]:
    step = _choose_step(span, max_ticks)
    ticks = build(step)
    guard = 0
    while len(ticks) > max(1, max_ticks) and guard < 24:
        step = _bump_step(step)
        ticks = build(step)
        guard += 1
    return ticks


def _aligned_times(d0: float, d1: float, step: int) -> List[float]:
    """Multiples of ``step`` inside [d0, d1], measured from t=0."""
    if step <= 0:
        return [d0]
    k = math.ceil((d0 - 1e-9) / step)
    times: List[float] = []
    guard = 0
    while guard < 10000:
        t = k * step
        if t > d1 + 1e-6:
            break
        if t >= d0 - 1e-6:
            times.append(float(t))
        k += 1
        guard += 1
    return times or [d0]


def _rel_line1(t: float, *, step: int, mode: str) -> str:
    """``mode`` is ``minute`` (span < 1h), ``hour`` (up to 1 day), or ``day``.

    Seconds appear in the minute mode always (``m:ss``), and in the hour
    mode only when the step itself is under a minute. A minute-mode tick
    that sits past the first hour — a short panel late in a long run —
    keeps the hours, so it does not read as thousands of minutes.
    """
    total = int(round(t))
    sign = "-" if total < 0 else ""
    a = abs(total)
    if mode == "day":
        within = a % 86400
        hh, rem = divmod(within, 3600)
        return f"{hh:02d}:{rem // 60:02d}"
    if mode == "minute" and a < 3600:
        mm, ss = divmod(a, 60)
        return f"{sign}{mm}:{ss:02d}"
    hh, rem = divmod(a, 3600)
    mm, ss = divmod(rem, 60)
    if step < 60:
        return f"{sign}{hh}:{mm:02d}:{ss:02d}"
    return f"{sign}{hh}:{mm:02d}"


def _abs_lines(dt: datetime, *, step: int, show_date: bool) -> Tuple[str, str]:
    if step < 60:
        line1 = f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    else:
        line1 = f"{dt.hour:02d}:{dt.minute:02d}"
    line2 = f"{_MONTHS[dt.month - 1]} {dt.day}" if show_date else ""
    return line1, line2


def _ceil_aligned(dt: datetime, step: int) -> datetime:
    """Earliest naive datetime >= ``dt`` on a local-clock boundary of ``step`` seconds.

    Sub-second leftovers round up to the next whole second first, so a
    start of 14:00:00.100 does not claim the 14:00:00 tick behind it.
    Day-or-longer steps ceil to midnight (then the caller advances by
    ``step``, which is a whole number of days). Shorter steps ceil to the
    next second, minute, or hour that the step divides.
    """
    if dt.microsecond:
        dt = dt.replace(microsecond=0) + timedelta(seconds=1)

    if step >= 86400:
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if dt != midnight:
            return midnight + timedelta(days=1)
        return midnight

    if step >= 3600:
        hours = step // 3600
        base = dt.replace(minute=0, second=0, microsecond=0)
        if dt != base:
            base = base + timedelta(hours=1)
        rem = base.hour % hours
        if rem:
            base = base + timedelta(hours=(hours - rem))
        return base

    if step >= 60:
        minutes = step // 60
        base = dt.replace(second=0, microsecond=0)
        if dt != base:
            base = base + timedelta(minutes=1)
        rem = base.minute % minutes
        if rem:
            base = base + timedelta(minutes=(minutes - rem))
        return base

    base = dt.replace(microsecond=0)
    rem = base.second % step
    if rem:
        base = base + timedelta(seconds=(step - rem))
    return base
