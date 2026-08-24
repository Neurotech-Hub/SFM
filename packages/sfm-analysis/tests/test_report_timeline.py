"""Tests for sfm_analysis.report.sections.timeline."""

import re
import xml.etree.ElementTree as ET

from report_fixtures import bandit_run, input_changed_row, write_session

from sfm_analysis.report.loader import load_rows
from sfm_analysis.report.metrics import compute_run_metrics
from sfm_analysis.report.schema import SectionContext
from sfm_analysis.report.sections.timeline import actogram_section, session_raster_section
from sfm_analysis.report.session import split_runs


def _ctx(tmp_path, n_trials=3, window_s=600, t0_ms=1_700_000_000_000):
    rows = bandit_run(n_trials=n_trials, session="TL", t0_ms=t0_ms)
    path = write_session(tmp_path, rows, session="TL")
    loaded, _, _ = load_rows(path)
    runs = split_runs(loaded, [], path)
    metrics = [compute_run_metrics(r) for r in runs]
    return SectionContext(runs=runs, metrics=metrics, combined=False, align="relative",
                           opts={"window_s": window_s})


def _multi_day_ctx(tmp_path, n_days=4, opts=None):
    day_ms = 24 * 3600 * 1000
    rows = [
        input_changed_row(1_700_000_000_000 + day * day_ms + hour * 3600 * 1000,
                           1, 4, True, "MousePresence Detected")
        for day in range(n_days) for hour in (8, 20)
    ]
    path = write_session(tmp_path, rows, session="MD")
    loaded, _, _ = load_rows(path)
    runs = split_runs(loaded, [], path)
    metrics = [compute_run_metrics(r) for r in runs]
    return SectionContext(runs=runs, metrics=metrics, combined=False, align="relative",
                           opts=opts or {})


class TestSessionRaster:
    def test_short_run_has_only_an_overview_panel(self, tmp_path):
        ctx = _ctx(tmp_path, n_trials=2, window_s=600)
        result = session_raster_section(ctx)
        assert result is not None
        assert not result.empty
        assert result.html.count("<figure>") == 1  # short run: just the overview

    def test_all_svgs_parse(self, tmp_path):
        ctx = _ctx(tmp_path, n_trials=2)
        result = session_raster_section(ctx)
        svgs = re.findall(r"<svg.*?</svg>", result.html, re.DOTALL)
        assert len(svgs) >= 1
        for s in svgs:
            ET.fromstring(s)

    def test_page_break_before_is_set(self, tmp_path):
        ctx = _ctx(tmp_path, n_trials=1)
        result = session_raster_section(ctx)
        assert result.page_break_before is True

    def test_no_nodes_yields_empty_result(self, tmp_path):
        from sfm_analysis.report.session import RunData
        empty_run = RunData(session="Empty", run_id=1, source_path=tmp_path / "e.csv")
        from sfm_analysis.report.metrics import compute_run_metrics as crm
        ctx = SectionContext(runs=[empty_run], metrics=[crm(empty_run)], combined=False,
                              align="relative", opts={})
        result = session_raster_section(ctx)
        assert result.empty is True


class TestActogram:
    def test_single_day_run_is_empty(self, tmp_path):
        ctx = _ctx(tmp_path, n_trials=2)  # bandit_run: minutes, not days
        result = actogram_section(ctx)
        assert result.empty is True

    def test_multi_day_run_renders_one_row_per_day(self, tmp_path):
        ctx = _multi_day_ctx(tmp_path, n_days=4)
        result = actogram_section(ctx)
        assert result.empty is False
        # 4 lane labels rendered by charts.raster's own lane-label pass.
        assert result.html.count('text-anchor="end"') == 4

    def test_all_svgs_parse(self, tmp_path):
        ctx = _multi_day_ctx(tmp_path, n_days=3)
        result = actogram_section(ctx)
        svgs = re.findall(r"<svg.*?</svg>", result.html, re.DOTALL)
        assert len(svgs) == 1
        for s in svgs:
            ET.fromstring(s)

    def test_lights_on_off_options_change_the_caption(self, tmp_path):
        ctx = _multi_day_ctx(tmp_path, n_days=3, opts={"lights_on": 7.0, "lights_off": 19.0})
        result = actogram_section(ctx)
        assert "19:00" in result.html
        assert "07:00" in result.html

    def test_no_page_break_before(self, tmp_path):
        # Follows directly after session_raster's own page break; it
        # should not force a second page break of its own.
        ctx = _multi_day_ctx(tmp_path, n_days=3)
        result = actogram_section(ctx)
        assert result.page_break_before is False

    def test_marks_count_matches_activity_events(self, tmp_path):
        ctx = _multi_day_ctx(tmp_path, n_days=4)  # 2 events/day * 4 days
        result = actogram_section(ctx)
        # +1 for the SVG's own <title> (charts.svg's title= argument).
        assert result.html.count("<title>") == 4 * 2 + 1
