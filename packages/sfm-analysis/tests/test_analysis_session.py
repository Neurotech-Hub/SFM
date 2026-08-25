"""Tests for sfm_analysis.analysis.session (load_session, Session)."""

import pytest
from report_fixtures import bandit_run, session_open_row, write_session

from sfm_analysis.analysis import AmbiguousSessionError, SessionNotFoundError, load_session


class TestLoadSession:
    def test_loads_by_exact_name(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=3, session="A"), session="A")
        s = load_session("A", log_dir=tmp_path)
        assert s.name == "A"
        assert len(s.runs) == 1

    def test_loads_by_explicit_path(self, tmp_path):
        p = write_session(tmp_path, bandit_run(n_trials=2, session="B"), session="B")
        s = load_session(str(p), log_dir=tmp_path)
        assert s.name == "B"

    def test_no_match_raises_session_not_found(self, tmp_path):
        with pytest.raises(SessionNotFoundError):
            load_session("nonexistent", log_dir=tmp_path)

    def test_glob_matching_multiple_raises_ambiguous(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=1, session="cohortA_1"), session="cohortA_1")
        write_session(tmp_path, bandit_run(n_trials=1, session="cohortA_2"), session="cohortA_2")
        with pytest.raises(AmbiguousSessionError):
            load_session("cohortA_*", log_dir=tmp_path)

    def test_metrics_precomputed_for_every_run(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=3, session="A"), session="A")
        s = load_session("A", log_dir=tmp_path)
        assert len(s.metrics) == len(s.runs)


class TestSessionRunPicking:
    def test_single_run_returned_directly(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=2, session="A"), session="A")
        s = load_session("A", log_dir=tmp_path)
        assert s.run().run_id == s.runs[0].run_id

    def test_explicit_run_id_selects_that_run(self, tmp_path):
        rows = bandit_run(n_trials=2, session="A", t0_ms=1_700_000_000_000)
        abortive_restart = [session_open_row(1_800_000_000_000, "A", run_id=2)]
        write_session(tmp_path, rows + abortive_restart, session="A")

        s = load_session("A", log_dir=tmp_path)
        assert {r.run_id for r in s.runs} == {1, 2}
        assert s.run(run_id=1).run_id == 1
        assert s.run(run_id=2).run_id == 2

    def test_unknown_run_id_raises(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
        s = load_session("A", log_dir=tmp_path)
        with pytest.raises(LookupError):
            s.run(run_id=999)

    def test_picks_the_run_with_the_most_rows_by_default(self, tmp_path):
        """A short aborted restart shouldn't win over the real session."""
        rows = bandit_run(n_trials=5, session="A", t0_ms=1_700_000_000_000)
        abortive_restart = [session_open_row(1_800_000_000_000, "A", run_id=2)]
        write_session(tmp_path, rows + abortive_restart, session="A")

        s = load_session("A", log_dir=tmp_path)
        picked = s.run()
        assert picked.run_id == 1
        assert len(picked.rows) > 1
