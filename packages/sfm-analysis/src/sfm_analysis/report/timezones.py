"""timezones.py — local wall-clock time-of-day, honestly.

Two different needs, two different pieces of data:

  - **Time-of-day** ("what hour of the day did this happen"), the thing an
    actogram bins on, comes from each ``LogRow``'s own ``iso`` field
    directly. The base station writes ``timestamp_iso`` with
    ``datetime.fromtimestamp(...)`` *on the rig itself* (see
    ``log_manager.py``), so it is already the rig's local wall-clock time
    at the moment that row was written — correct regardless of what
    timezone the machine doing the analysis is in, and regardless of
    whether the rig's real-world UTC offset is known at all.

  - **A genuinely timezone-aware datetime** (needed only if you want to
    convert a rig's local time to *another* zone, or combine sessions
    recorded in different real-world timezones on one absolute clock) is
    a different question, and needs ``RunData.utc_offset_s`` — captured
    from ``session_start``'s fields since the base station started
    logging it. Logs recorded before that have ``utc_offset_s=None``:
    the wall-clock value in ``timestamp_iso`` is still correct rig-local
    time, this module just can't tell you how it relates to UTC.

The bug this exists to prevent: calling ``datetime.fromtimestamp()`` (no
arguments) or ``.astimezone()`` on data from this package implicitly uses
*your own machine's* timezone, not the rig's. On a cross-platform SDK
where analysis routinely happens on a different machine than the one that
recorded the log, that's a silent, wrong answer every time analyst and
rig are in different zones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .loader import LogRow
    from .session import RunData


def time_of_day(row: "LogRow") -> float:
    """
    Hours since local midnight (``0.0`` to just under ``24.0``) for this
    row, in the rig's own local time — the x-axis value an actogram bins
    on. Uses ``row.iso`` directly; see this module's docstring for why
    that's already correct without needing ``utc_offset_s``.
    """
    dt = datetime.fromisoformat(row.iso)
    return dt.hour + dt.minute / 60 + dt.second / 3600 + dt.microsecond / 3_600_000_000


def local_date(row: "LogRow"):
    """The calendar date (a ``datetime.date``) this row falls on, in the
    rig's own local time — the row an actogram bins into."""
    return datetime.fromisoformat(row.iso).date()


def zeitgeber_time(row: "LogRow", *, lights_on: float = 6.0) -> float:
    """
    Zeitgeber time in hours (``0.0`` to just under ``24.0``, wrapped),
    relative to ``lights_on`` — ZT0 is lights-on, ZT12 is nominally
    lights-off under a standard 12:12 light/dark cycle.

    ``lights_on`` is hours since local midnight (default ``6.0`` =
    06:00); pass your facility's actual light-cycle start time. Light
    schedules are set in local wall-clock time by the people running the
    facility, so — same reasoning as ``time_of_day`` — no UTC offset is
    needed here either.
    """
    return (time_of_day(row) - lights_on) % 24.0


def wall_clock(row: "LogRow", run: "RunData") -> datetime:
    """
    A ``datetime`` for this row: timezone-*aware* if ``run.utc_offset_s``
    is known, naive otherwise. The naive case is not a failure — the
    value itself (from ``row.iso``) is still correct rig-local time — but
    a naive datetime is unsafe to pass to ``.astimezone()`` or compare
    against another timezone's data, which is exactly what an aware one
    (when the offset is known) makes safe.
    """
    naive = datetime.fromisoformat(row.iso)
    if run.utc_offset_s is None:
        return naive
    return naive.replace(tzinfo=timezone(timedelta(seconds=run.utc_offset_s)))
