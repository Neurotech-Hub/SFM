"""Tests for sfm_analysis.report.timezones."""

from datetime import datetime, timedelta

from report_fixtures import exp_row, row, write_session

from sfm_analysis.report.loader import load_rows
from sfm_analysis.report.session import split_runs
from sfm_analysis.report.timezones import (
    local_date, time_of_day, wall_clock, zeitgeber_time,
)


def _row_at(iso_local_ms: int):
    """A single row whose timestamp_iso we can control precisely by
    picking ts_ms such that fromtimestamp(ts_ms/1000) lands on a known
    wall-clock time. Since fromtimestamp() uses the *test machine's* own
    timezone, we don't assert against a hardcoded clock time -- we build
    the row, load it back, and compare time_of_day() against a
    fromisoformat() of the same iso string, which is what the function
    under test is defined to do.
    """
    return row(ts_ms=iso_local_ms)


class TestTimeOfDay:
    def test_matches_the_rows_own_iso_field(self, tmp_path):
        r = _row_at(1_700_000_000_000)
        path = write_session(tmp_path, [r], session="S")
        loaded, _, _ = load_rows(path)
        run = split_runs(loaded, [], path)[0]
        lr = run.rows[0]

        expected = datetime.fromisoformat(lr.iso)
        expected_hours = expected.hour + expected.minute / 60 + expected.second / 3600
        assert time_of_day(lr) == expected_hours

    def test_is_always_in_0_24_range(self, tmp_path):
        rows = [row(ts_ms=1_700_000_000_000 + i * 3_600_000) for i in range(30)]
        path = write_session(tmp_path, rows, session="S")
        loaded, _, _ = load_rows(path)
        run = split_runs(loaded, [], path)[0]
        for lr in run.rows:
            tod = time_of_day(lr)
            assert 0.0 <= tod < 24.0


class TestLocalDate:
    def test_matches_the_isos_own_date(self, tmp_path):
        r = _row_at(1_700_000_000_000)
        path = write_session(tmp_path, [r], session="S")
        loaded, _, _ = load_rows(path)
        run = split_runs(loaded, [], path)[0]
        lr = run.rows[0]

        assert local_date(lr) == datetime.fromisoformat(lr.iso).date()


class TestZeitgeberTime:
    def test_zt0_at_lights_on(self, tmp_path):
        rows = [row(ts_ms=1_700_000_000_000 + i * 3_600_000) for i in range(24)]
        path = write_session(tmp_path, rows, session="S")
        loaded, _, _ = load_rows(path)
        run = split_runs(loaded, [], path)[0]
        for lr in run.rows:
            tod = time_of_day(lr)
            zt = zeitgeber_time(lr, lights_on=tod)
            assert zt == 0.0

    def test_wraps_around_midnight(self):
        # A row whose time_of_day is 1.0 and lights_on=23.0 should give
        # zt=2.0 (1.0 - 23.0 = -22.0, wrapped mod 24 -> 2.0).
        class _FakeRow:
            iso = "2026-01-01T01:00:00"
        assert zeitgeber_time(_FakeRow(), lights_on=23.0) == 2.0

    def test_default_lights_on_is_6am(self):
        class _FakeRow:
            iso = "2026-01-01T06:00:00"
        assert zeitgeber_time(_FakeRow()) == 0.0


class TestWallClock:
    def test_naive_when_utc_offset_unknown(self, tmp_path):
        path = write_session(tmp_path, [_row_at(1_700_000_000_000)], session="S")
        loaded, _, _ = load_rows(path)
        run = split_runs(loaded, [], path)[0]
        assert run.utc_offset_s is None
        dt = wall_clock(run.rows[0], run)
        assert dt.tzinfo is None

    def test_aware_when_utc_offset_known(self, tmp_path):
        rows = [
            exp_row(1_700_000_000_000, "session_start",
                    fields={"experiment": "x", "nodes": [1], "seed": 1, "utc_offset_s": -18000.0}),
            _row_at(1_700_000_001_000),
        ]
        path = write_session(tmp_path, rows, session="S")
        loaded, _, _ = load_rows(path)
        run = split_runs(loaded, [], path)[0]
        assert run.utc_offset_s == -18000.0

        data_row = next(r for r in run.rows if r.event_name != "session_start")
        dt = wall_clock(data_row, run)
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(seconds=-18000.0)
