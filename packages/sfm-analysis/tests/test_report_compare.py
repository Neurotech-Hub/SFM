"""Tests for base_station.report.align and sections.compare."""

import os
import sys
import xml.etree.ElementTree as ET
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from report_fixtures import bandit_run, write_session  # noqa: E402

from base_station.report.align import compute_offsets  # noqa: E402
from base_station.report.loader import load_rows  # noqa: E402
from base_station.report.metrics import compute_run_metrics  # noqa: E402
from base_station.report.schema import SectionContext  # noqa: E402
from base_station.report.sections.compare import (  # noqa: E402
    cohort_table_section, cumulative_overlay_section, learning_curve_section,
    node_preference_section, quality_matrix_section, subject_spread_section,
)
from base_station.report.session import split_runs  # noqa: E402


def _run_from(tmp_path, session, t0_ms, n_trials=3, day=None):
    name = f"cohortA_{session}_d{day}" if day is not None else session
    rows = bandit_run(n_trials=n_trials, session=name, t0_ms=t0_ms)
    path = write_session(tmp_path, rows, session=name)
    loaded, _, _ = load_rows(path)
    return split_runs(loaded, [], path)[0]


def _ctx(tmp_path, sessions):
    runs = [_run_from(tmp_path, **kw) for kw in sessions]
    metrics = [compute_run_metrics(r) for r in runs]
    return SectionContext(runs=runs, metrics=metrics, combined=True, align="relative", opts={})


class TestAlignOffsets:
    def test_relative_and_trial_are_no_ops(self, tmp_path):
        runs = [_run_from(tmp_path, session="A", t0_ms=1_700_000_000_000),
                _run_from(tmp_path, session="B", t0_ms=1_700_100_000_000)]
        for mode in ("relative", "trial"):
            offsets = compute_offsets(runs, mode)
            assert all(v == 0.0 for v in offsets.values())

    def test_wall_shifts_to_shared_earliest_origin(self, tmp_path):
        runs = [_run_from(tmp_path, session="A", t0_ms=1_700_000_000_000),
                _run_from(tmp_path, session="B", t0_ms=1_700_000_010_000)]
        offsets = compute_offsets(runs, "wall")
        vals = sorted(offsets.values())
        assert vals[0] == 0.0
        assert abs(vals[1] - 10.0) < 1e-6

    def test_event_alignment_shifts_to_first_matching_row(self, tmp_path):
        run = _run_from(tmp_path, session="A", t0_ms=1_700_000_000_000, n_trials=2)
        offsets = compute_offsets([run], "event:trial")
        first_trial_t = run.exp("trial")[0].t
        assert abs(offsets[id(run)] - (-first_trial_t)) < 1e-6

    def test_unmatched_event_name_falls_back_to_zero(self, tmp_path):
        run = _run_from(tmp_path, session="A", t0_ms=1_700_000_000_000)
        offsets = compute_offsets([run], "event:nonexistent_event_xyz")
        assert offsets[id(run)] == 0.0


class TestCompareSections:
    def test_cohort_table_has_one_row_per_run(self, tmp_path):
        ctx = _ctx(tmp_path, [
            dict(session="A", t0_ms=1_700_000_000_000),
            dict(session="B", t0_ms=1_700_100_000_000),
        ])
        result = cohort_table_section(ctx)
        assert result is not None and not result.empty
        assert result.html.count("<tr>") == 3  # 1 header row + 2 data rows

    def test_learning_curve_needs_at_least_two_points_per_subject(self, tmp_path):
        ctx = _ctx(tmp_path, [
            dict(session="M014", t0_ms=1_700_000_000_000, day=1),
            dict(session="M014", t0_ms=1_700_100_000_000, day=2),
        ])
        # bandit_run's fixture doesn't emit a "Loaded" CAN event, so
        # take_rate (the default metric) is always None; p_choose_rich is
        # the metric this fixture actually supports.
        ctx.opts = {"metric": "p_choose_rich"}
        result = learning_curve_section(ctx)
        assert result is not None
        assert not result.empty

    def test_learning_curve_empty_with_single_session_per_subject(self, tmp_path):
        ctx = _ctx(tmp_path, [dict(session="M014", t0_ms=1_700_000_000_000, day=1)])
        result = learning_curve_section(ctx)
        assert result.empty is True

    def test_subject_spread_needs_two_subjects(self, tmp_path):
        ctx = _ctx(tmp_path, [
            dict(session="M014", t0_ms=1_700_000_000_000, day=1),
            dict(session="M015", t0_ms=1_700_100_000_000, day=1),
        ])
        ctx.opts = {"metric": "p_choose_rich"}
        result = subject_spread_section(ctx)
        assert not result.empty

    def test_node_preference_renders_heatmap(self, tmp_path):
        ctx = _ctx(tmp_path, [
            dict(session="A", t0_ms=1_700_000_000_000),
            dict(session="B", t0_ms=1_700_100_000_000),
        ])
        result = node_preference_section(ctx)
        if not result.empty:
            svgs = re.findall(r"<svg.*?</svg>", result.html, re.DOTALL)
            for s in svgs:
                ET.fromstring(s)

    def test_quality_matrix_renders_for_multiple_runs(self, tmp_path):
        ctx = _ctx(tmp_path, [
            dict(session="A", t0_ms=1_700_000_000_000),
            dict(session="B", t0_ms=1_700_100_000_000),
        ])
        result = quality_matrix_section(ctx)
        assert not result.empty
        svgs = re.findall(r"<svg.*?</svg>", result.html, re.DOTALL)
        for s in svgs:
            ET.fromstring(s)

    def test_cumulative_overlay_respects_align_option(self, tmp_path):
        ctx = _ctx(tmp_path, [
            dict(session="A", t0_ms=1_700_000_000_000),
            dict(session="B", t0_ms=1_700_100_000_000),
        ])
        ctx.align = "event:trial"
        result = cumulative_overlay_section(ctx)
        assert not result.empty
        assert "event:trial" in result.html
        svgs = re.findall(r"<svg.*?</svg>", result.html, re.DOTALL)
        for s in svgs:
            ET.fromstring(s)

    def test_single_run_sections_are_empty_not_erroring(self, tmp_path):
        ctx = _ctx(tmp_path, [dict(session="A", t0_ms=1_700_000_000_000)])
        for fn in (node_preference_section, quality_matrix_section, cumulative_overlay_section):
            result = fn(ctx)
            assert result.empty is True
