"""Tests for sfm_analysis.targeting -- shared by the CLI and the analysis API."""

from report_fixtures import bandit_run, write_session

from sfm_analysis.targeting import resolve_targets


def test_exact_session_name_resolves(tmp_path):
    p = write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
    assert resolve_targets(["A"], tmp_path) == [p]


def test_explicit_path_resolves(tmp_path):
    p = write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
    assert resolve_targets([str(p)], tmp_path) == [p]


def test_glob_matches_multiple_in_sorted_order(tmp_path):
    p_a = write_session(tmp_path, bandit_run(n_trials=1, session="cohortA_1"), session="cohortA_1")
    p_b = write_session(tmp_path, bandit_run(n_trials=1, session="cohortA_2"), session="cohortA_2")
    write_session(tmp_path, bandit_run(n_trials=1, session="cohortB_1"), session="cohortB_1")
    assert resolve_targets(["cohortA_*"], tmp_path) == sorted([p_a, p_b])


def test_no_match_returns_empty(tmp_path):
    assert resolve_targets(["nonexistent"], tmp_path) == []


def test_duplicate_matches_across_targets_appear_once(tmp_path):
    p = write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
    assert resolve_targets(["A", "A", "A*"], tmp_path) == [p]


def test_preserves_target_order(tmp_path):
    p_a = write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
    p_b = write_session(tmp_path, bandit_run(n_trials=1, session="B"), session="B")
    assert resolve_targets(["B", "A"], tmp_path) == [p_b, p_a]
