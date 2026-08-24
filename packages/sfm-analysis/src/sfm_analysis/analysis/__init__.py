"""analysis — the curated Python API for custom session analysis.

Everything below is stable, documented, and safe to build on. It sits on
top of ``sfm_analysis.report`` (loading, run-splitting, metrics) without
requiring you to know that package's internals — see this package's
functions' docstrings for worked examples, and
``packages/sfm-analysis/README.md``'s "five things to know" section for
the pitfalls a session log has that aren't obvious from the CSV alone.

    from sfm_analysis.analysis import load_session

    s = load_session("EXP-Test-02")
    cycles = s.cycles_table()          # list[dict], one row per dispense cycle
    bouts = s.bouts_table()            # presence / dome / fault intervals

Everything here returns plain ``list[dict]`` / dataclasses / floats — no
pandas dependency. Call ``to_dataframe(rows)`` if you have pandas
installed and want one.
"""

from __future__ import annotations

from . import intervals
from .intervals import around, count_in, merge, overlap, rate_in, subtract
from .session import AmbiguousSessionError, Session, SessionNotFoundError, load_session
from .tables import bouts_table, cycles_table, events_table, to_dataframe, trials_table
from ..report.timezones import local_date, time_of_day, wall_clock, zeitgeber_time

__all__ = [
    "load_session",
    "Session",
    "SessionNotFoundError",
    "AmbiguousSessionError",
    "cycles_table",
    "bouts_table",
    "events_table",
    "trials_table",
    "to_dataframe",
    "intervals",
    "overlap",
    "subtract",
    "merge",
    "around",
    "count_in",
    "rate_in",
    "time_of_day",
    "local_date",
    "zeitgeber_time",
    "wall_clock",
]
