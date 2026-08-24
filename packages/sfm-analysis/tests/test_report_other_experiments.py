"""Tests for the probability_delivery, fixed_and_random, and free_feeding
analyses/sections/designs (M9)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from report_fixtures import exp_row, row, session_open_row, write_session  # noqa: E402

from base_station.report.analyses.fixed_random import (  # noqa: E402
    observed_random_rate, role_summary,
)
from base_station.report.analyses.free_feeding import (  # noqa: E402
    inter_take_intervals, meal_bouts, reload_latencies,
)
from base_station.report.analyses.probability import delivery_distribution  # noqa: E402
from base_station.report.loader import load_rows  # noqa: E402
from base_station.report.schema import resolve_design  # noqa: E402
from base_station.report.session import split_runs  # noqa: E402
from base_station.protocol import CanEvent  # noqa: E402
from report_fixtures import can_event_row  # noqa: E402


def _run(tmp_path, rows, session="X"):
    path = write_session(tmp_path, rows, session=session)
    loaded, _, _ = load_rows(path)
    return split_runs(loaded, [], path)[0]


class TestProbabilityDelivery:
    def test_delivery_distribution_matches_hand_computation(self, tmp_path):
        rows = [
            exp_row(0, "session_start", {"experiment": "probability_delivery", "nodes": [1, 2], "seed": 1}),
            exp_row(10, "probability_delivery_start", {"nodes": [1, 2], "weights": [50.0, 50.0], "trigger": "timer"}),
        ]
        # 10 picks node 1, 0 picks node 2 -> should show a large chi2 vs 50/50 expectation.
        for i in range(10):
            rows.append(row(ts_ms=1000 + i * 100, node_id=1, frame_type="EXPERIMENT", source="EXP",
                            direction="SYS", event_name="probability_pick"))
        run = _run(tmp_path, rows)
        fit = delivery_distribution(run)
        assert fit is not None
        assert fit.total == 10
        by_node = {n.node: n for n in fit.nodes}
        assert by_node[1].observed == 10
        assert by_node[1].expected == 5.0
        assert by_node[2].observed == 0
        assert fit.chi2 > 3.841   # exceeds the p<0.05 threshold for df=1

    def test_no_weights_returns_none(self, tmp_path):
        rows = [exp_row(0, "session_start", {"experiment": "probability_delivery", "nodes": [1, 2], "seed": 1})]
        run = _run(tmp_path, rows)
        assert delivery_distribution(run) is None

    def test_design_resolves_by_experiment_name(self):
        d = resolve_design("probability_delivery")
        assert d.name == "probability_delivery"
        refs = {s.ref for s in d.sections}
        assert "probability.delivery_fit" in refs


class TestFixedAndRandom:
    def test_role_summary_reads_string_keyed_roles_dict(self, tmp_path):
        rows = [
            exp_row(0, "session_start", {"experiment": "fixed_and_random", "nodes": [1, 2], "seed": 1}),
            exp_row(10, "fixed_and_random_start", {
                "nodes": [1, 2], "roles": {"1": "fixed", "2": "random"},
                "trigger": "timer", "random_prob": 0.5,
            }),
            can_event_row(1000, 1, CanEvent.Loaded),
            can_event_row(1100, 1, CanEvent.PelletTaken, bytes([1, 0, 1])),
        ]
        run = _run(tmp_path, rows)
        summary = role_summary(run)
        by_node = {r.node: r for r in summary}
        assert by_node[1].role == "fixed"
        assert by_node[1].presented == 1
        assert by_node[1].taken == 1
        assert by_node[2].role == "random"
        assert by_node[2].presented == 0

    def test_observed_random_rate_uses_node_id_column_not_fields(self, tmp_path):
        rows = [
            exp_row(0, "session_start", {"experiment": "fixed_and_random", "nodes": [1, 2], "seed": 1}),
            exp_row(10, "fixed_and_random_start", {
                "nodes": [1, 2], "roles": {"1": "fixed", "2": "random"},
                "trigger": "timer", "random_prob": 0.5,
            }),
        ]
        for i in range(4):
            rows.append(exp_row(1000 + i * 100, "trial", {"trial": i + 1}, trial=i + 1))
        # random_dispense rows carry node on the CSV column, fields_json empty (real-data shape).
        rows.append(row(ts_ms=1050, node_id=2, frame_type="EXPERIMENT", source="EXP",
                        direction="SYS", event_name="random_dispense"))
        rows.append(row(ts_ms=1250, node_id=2, frame_type="EXPERIMENT", source="EXP",
                        direction="SYS", event_name="random_dispense"))
        run = _run(tmp_path, rows)
        fit = observed_random_rate(run)
        assert fit is not None
        assert fit.k == 2
        assert fit.n == 4
        assert fit.configured == 0.5

    def test_design_resolves_by_experiment_name(self):
        d = resolve_design("fixed_and_random")
        assert d.name == "fixed_and_random"


class TestFreeFeeding:
    def test_reload_latency_pairs_take_with_next_reload(self, tmp_path):
        rows = [
            exp_row(0, "session_start", {"experiment": "free_feeding", "nodes": [1], "seed": 1}),
            exp_row(10, "free_feeding_start", {"nodes": [1], "reload_delay_s": 30}),
            can_event_row(1000, 1, CanEvent.PelletTaken, bytes([1, 0, 1])),
            exp_row(31000, "reload_dispense", {"node": 1}, node=1),
        ]
        run = _run(tmp_path, rows)
        lat = reload_latencies(run)
        assert 1 in lat
        assert abs(lat[1][0] - 30.0) < 1e-6

    def test_meal_bouts_clusters_on_gap_threshold(self, tmp_path):
        rows = [exp_row(0, "session_start", {"experiment": "free_feeding", "nodes": [1], "seed": 1})]
        # Two takes close together (one meal), then a big gap, then one more take (second meal).
        rows.append(can_event_row(1000, 1, CanEvent.PelletTaken, bytes([1, 0, 1])))
        rows.append(can_event_row(1050, 1, CanEvent.PelletTaken, bytes([2, 0, 1])))
        rows.append(can_event_row(1000 + 400_000, 1, CanEvent.PelletTaken, bytes([3, 0, 1])))  # +400s gap
        run = _run(tmp_path, rows)
        meals = meal_bouts(run, gap_s=300)
        assert len(meals[1]) == 2
        assert meals[1][0].pellets == 2
        assert meals[1][1].pellets == 1

    def test_inter_take_intervals(self, tmp_path):
        rows = [
            exp_row(0, "session_start", {"experiment": "free_feeding", "nodes": [1], "seed": 1}),
            can_event_row(1000, 1, CanEvent.PelletTaken, bytes([1, 0, 1])),
            can_event_row(6000, 1, CanEvent.PelletTaken, bytes([2, 0, 1])),
        ]
        run = _run(tmp_path, rows)
        gaps = inter_take_intervals(run)
        assert abs(gaps[1][0] - 5.0) < 1e-6

    def test_design_resolves_by_experiment_name(self):
        d = resolve_design("free_feeding")
        assert d.name == "free_feeding"
