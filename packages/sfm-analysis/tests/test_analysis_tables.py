"""Tests for sfm_analysis.analysis.tables."""

import pytest
from report_fixtures import bandit_run, write_session

from sfm_analysis.analysis import load_session, to_dataframe


@pytest.fixture
def session(tmp_path):
    rows = bandit_run(n_trials=4, session="T")
    write_session(tmp_path, rows, session="T")
    return load_session("T", log_dir=tmp_path)


class TestCyclesTable:
    def test_one_row_per_cycle_matches_metrics(self, session):
        table = session.cycles_table()
        expected = len(session.metrics_for().cycles)
        assert len(table) == expected
        assert expected > 0

    def test_every_row_has_the_same_keys(self, session):
        table = session.cycles_table()
        keys = {frozenset(row.keys()) for row in table}
        assert len(keys) == 1

    def test_session_and_run_id_columns_present(self, session):
        table = session.cycles_table()
        assert all(row["session"] == "T" for row in table)
        assert all(row["run_id"] == session.run().run_id for row in table)

    def test_run_id_filter_narrows_to_that_run(self, session):
        all_rows = session.cycles_table()
        filtered = session.cycles_table(run_id=session.run().run_id)
        assert filtered == all_rows  # single-run fixture: identical


class TestBoutsTable:
    def test_kinds_are_presence_dome_or_fault(self, session):
        table = session.bouts_table()
        assert set(row["kind"] for row in table) <= {"presence", "dome", "fault"}

    def test_row_count_matches_metrics_sum(self, session):
        m = session.metrics_for()
        presence_bouts, _ = m.presence
        dome_bouts, _ = m.dome
        expected = len(presence_bouts) + len(dome_bouts) + len(m.faults)
        assert len(session.bouts_table()) == expected

    def test_non_fault_rows_have_none_code(self, session):
        table = session.bouts_table()
        for row in table:
            if row["kind"] != "fault":
                assert row["code"] is None


class TestEventsTable:
    def test_unfiltered_matches_run_row_count(self, session):
        table = session.events_table()
        assert len(table) == len(session.run().rows)

    def test_event_name_filter(self, session):
        table = session.events_table(event_name="trial")
        assert table
        assert all(row["event_name"] == "trial" for row in table)

    def test_source_filter(self, session):
        table = session.events_table(source="EXP")
        assert table
        assert all(row["source"] == "EXP" for row in table)

    def test_fields_is_a_dict(self, session):
        table = session.events_table(event_name="trial")
        assert all(isinstance(row["fields"], dict) for row in table)


class TestTrialsTable:
    def test_row_count_matches_trial_events(self, session):
        expected = len(session.run().exp("trial"))
        assert len(session.trials_table()) == expected
        assert expected == 4  # bandit_run(n_trials=4)


class TestToDataframe:
    def test_raises_clear_error_without_pandas(self, session, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pandas":
                raise ImportError("no pandas here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError, match="sfm-analysis\\[pandas\\]"):
            to_dataframe(session.cycles_table())

    def test_works_if_pandas_is_installed(self, session):
        pd = pytest.importorskip("pandas")
        df = to_dataframe(session.cycles_table())
        assert isinstance(df, pd.DataFrame)
        assert len(df) == len(session.cycles_table())
