"""Tests for the shared time axis (report/time_axis.py).

The assertions that matter are the ones a long experiment gets wrong
when each chart invents its own ticks: the label count stays inside the
pixel budget, absolute ticks sit on the rig's clock rather than on an
offset from t=0, the date is a second line only when it changes, and
none of the arithmetic consults the machine running the report.
"""

import re

from sfm_analysis.report.time_axis import (
    TimeBase,
    abs_ticks,
    fmt_abs,
    fmt_abs_range,
    fmt_rel,
    max_ticks_for,
    rel_ticks,
)

_SPANS = (60, 2 * 3600, 24 * 3600, 3 * 86400, 7 * 86400)
_DATE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}$")


def _base():
    return TimeBase("2026-10-28T14:17:42")


class TestTickBudget:
    def test_width_sets_the_budget_not_the_duration(self):
        assert max_ticks_for(822) == 11
        assert max_ticks_for(10) == 2
        assert max_ticks_for(0) == 2

    def test_tick_count_stays_inside_the_budget_for_short_and_long_runs(self):
        base = _base()
        for span in _SPANS:
            rel = rel_ticks(0, span, 8)
            ab = abs_ticks(base, 0, span, 8)
            assert 1 <= len(rel) <= 8, span
            assert 1 <= len(ab) <= 8, span


class TestRelativeLabels:
    def test_a_later_window_does_not_restart_at_zero(self):
        ticks = rel_ticks(7200, 14400, 8)
        assert ticks[0].line1 == "2:00"
        assert all(t.line1 != "0:00" for t in ticks)

    def test_under_an_hour_is_minutes_and_seconds(self):
        ticks = rel_ticks(0, 60, 8)
        assert ticks[0].line1 == "0:00"
        assert all(t.line2 == "" for t in ticks)
        assert any(t.line1 == "1:00" for t in ticks)  # one minute, not one hour

    def test_day_line_only_on_the_first_tick_and_day_changes(self):
        ticks = rel_ticks(0, 3 * 86400, 8)
        assert ticks[0].line2 == "day 1"
        assert any(t.line2 == "" for t in ticks[1:])
        assert any(t.line2 == "day 2" for t in ticks)
        assert ticks[0].line1 == "00:00"

    def test_full_form_drops_zero_seconds(self):
        assert fmt_rel(0) == "0s"
        assert fmt_rel(90) == "1m 30s"
        assert fmt_rel(7200) == "2h 00m"
        assert fmt_rel(3661) == "1h 01m 01s"
        assert fmt_rel(-60) == "-1m"


class TestAbsoluteLabels:
    def test_ticks_land_on_calendar_boundaries(self):
        base = _base()
        for span in _SPANS:
            ticks = abs_ticks(base, 0, span, 8)
            if len(ticks) < 2:
                continue
            step = min(b.t - a.t for a, b in zip(ticks, ticks[1:]))
            for tick in ticks:
                dt = base.wall(tick.t)
                assert dt is not None and dt.tzinfo is None
                if step >= 60 - 1e-6:
                    assert dt.second == 0
                if step >= 3600 - 1e-6:
                    assert dt.minute == 0 and dt.second == 0
                if step >= 86400 - 1e-6:
                    assert dt.hour == 0 and dt.minute == 0

    def test_date_line_only_on_the_first_tick_and_midnight_crossings(self):
        base = TimeBase("2026-10-28T22:00:00")
        ticks = abs_ticks(base, 0, 3 * 86400, 8)
        assert _DATE.match(ticks[0].line2)
        for prev, tick in zip(ticks, ticks[1:]):
            crossed = base.wall(prev.t).date() != base.wall(tick.t).date()
            if crossed:
                assert _DATE.match(tick.line2)
            else:
                assert tick.line2 == ""

    def test_seconds_appear_only_when_the_step_is_under_a_minute(self):
        base = TimeBase("2026-10-28T14:05:09")
        fine = abs_ticks(base, 0, 40, 8)
        coarse = abs_ticks(base, 0, 6 * 3600, 8)
        assert any(t.line1.count(":") == 2 for t in fine)
        assert coarse and all(t.line1.count(":") == 1 for t in coarse)

    def test_clock_comes_from_the_iso_text_not_the_host_timezone(self):
        base = TimeBase("2026-10-28T14:05:09")
        assert base.wall(0).tzinfo is None
        assert fmt_abs(base, 0) == "Oct 28 14:05:09"
        assert fmt_abs(base, 3600) == "Oct 28 15:05:09"
        assert fmt_abs(base, 10 * 3600) == "Oct 29 00:05:09"
        # An offset suffix is ignored, not applied. Applying it would show
        # 19:05 on a machine that converted -05:00 to UTC.
        shifted = TimeBase.from_iso("2026-10-28T14:05:09-05:00")
        assert fmt_abs(shifted, 0) == "Oct 28 14:05:09"
        zulu = TimeBase.from_iso("2026-10-28T14:05:09Z")
        assert fmt_abs(zulu, 0) == "Oct 28 14:05:09"
        assert TimeBase.from_iso("not a date").t0_iso is None
        assert fmt_abs(TimeBase(None), 0) == "—"

    def test_zero_seconds_drop_out_of_a_range_on_the_same_day(self):
        base = TimeBase("2026-10-28T14:00:00")
        assert fmt_abs(base, 0, seconds=False) == "Oct 28 14:00"
        assert fmt_abs_range(base, 0, 2 * 3600) == "Oct 28 14:00–16:00"
        # A non-zero second is kept, so the clock is not rounded away.
        odd = TimeBase("2026-10-28T14:05:09")
        assert fmt_abs(odd, 0, seconds=False) == "Oct 28 14:05:09"

    def test_from_run_uses_the_first_rows_iso(self):
        class Row:
            iso = "2026-10-28T14:05:09"

        class Run:
            rows = [Row()]

        assert TimeBase.from_run(Run()).wall(3600).hour == 15
        assert TimeBase.from_run(type("Empty", (), {"rows": []})()).t0_iso is None
