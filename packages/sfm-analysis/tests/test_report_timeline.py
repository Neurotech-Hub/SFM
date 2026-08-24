"""Tests for sfm_analysis.report.sections.timeline."""

import re
import xml.etree.ElementTree as ET

from report_fixtures import bandit_run, write_session

from sfm_analysis.report.loader import load_rows
from sfm_analysis.report.metrics import compute_run_metrics
from sfm_analysis.report.schema import SectionContext
from sfm_analysis.report.sections.timeline import session_raster_section
from sfm_analysis.report.session import split_runs


def _ctx(tmp_path, n_trials=3, window_s=600, t0_ms=1_700_000_000_000):
    rows = bandit_run(n_trials=n_trials, session="TL", t0_ms=t0_ms)
    path = write_session(tmp_path, rows, session="TL")
    loaded, _, _ = load_rows(path)
    runs = split_runs(loaded, [], path)
    metrics = [compute_run_metrics(r) for r in runs]
    return SectionContext(runs=runs, metrics=metrics, combined=False, align="relative",
                           opts={"window_s": window_s})


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
