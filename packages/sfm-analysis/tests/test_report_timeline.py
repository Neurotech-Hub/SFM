"""Tests for sfm_analysis.report.sections.timeline."""

import re
import xml.etree.ElementTree as ET

from report_fixtures import bandit_run, can_event_row, input_changed_row, write_session
from sfm_analysis.protocol import CanEvent

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


def _duration_ctx(tmp_path, duration_s, opts=None, t0_ms=1_700_000_000_000):
    """A run spanning exactly duration_s, with no explicit window_s in
    opts unless the caller supplies one -- for exercising the adaptive
    default in session_raster_section, which _ctx() above always
    overrides."""
    rows = [
        input_changed_row(t0_ms, 1, 4, True, "MousePresence Detected"),
        input_changed_row(t0_ms + int(duration_s * 1000), 1, 4, False, "MousePresence Cleared"),
    ]
    path = write_session(tmp_path, rows, session="DUR")
    loaded, _, _ = load_rows(path)
    runs = split_runs(loaded, [], path)
    metrics = [compute_run_metrics(r) for r in runs]
    return SectionContext(runs=runs, metrics=metrics, combined=False, align="relative",
                           opts=opts if opts is not None else {})


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


class TestAdaptiveWindow:
    def test_short_run_uses_the_600s_floor(self, tmp_path):
        # duration/12 = 300, below the 600s floor -> window_s=600, same
        # as the old fixed default.
        ctx = _duration_ctx(tmp_path, duration_s=3600)
        result = session_raster_section(ctx)
        # 1 overview + int(3600//600)=6 detail panels.
        assert result.html.count("<figure>") == 1 + 6

    def test_long_run_caps_at_twelve_detail_panels(self, tmp_path):
        # 86400 / 12 = 7200s window_s, exactly 12 panels -- this is the
        # case that used to be 144 panels at a fixed 600s.
        ctx = _duration_ctx(tmp_path, duration_s=86400)
        result = session_raster_section(ctx)
        assert result.html.count("<figure>") == 1 + 12

    def test_explicit_window_s_overrides_adaptive_scaling(self, tmp_path):
        ctx = _duration_ctx(tmp_path, duration_s=86400, opts={"window_s": 600})
        result = session_raster_section(ctx)
        assert result.html.count("<figure>") == 1 + 144

    def test_min_window_s_raises_the_floor_for_short_runs(self, tmp_path):
        ctx = _duration_ctx(tmp_path, duration_s=3600, opts={"min_window_s": 900})
        result = session_raster_section(ctx)
        # window_s = max(900, 300) = 900 -> int(3600//900)=4 panels.
        assert result.html.count("<figure>") == 1 + 4

    def test_min_window_s_does_not_apply_once_duration_scaling_wins(self, tmp_path):
        # duration/12 = 7200 > min_window_s=900, so the floor is
        # irrelevant here -- still exactly 12 panels.
        ctx = _duration_ctx(tmp_path, duration_s=86400, opts={"min_window_s": 900})
        result = session_raster_section(ctx)
        assert result.html.count("<figure>") == 1 + 12


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
        assert result.title == "Actogram — MousePresence Detected"

    def test_heading_names_the_plotted_event(self, tmp_path):
        """A printed page must say which CAN EVENT the ticks are, not
        just 'Actogram'. The section heading carries it, and the SVG's
        own <desc> restates it for anyone reading the figure alone."""
        ctx = _multi_day_ctx(tmp_path, n_days=3)
        result = actogram_section(ctx)
        assert result.title.startswith("Actogram — ")
        assert "MousePresence Detected" in result.title
        assert "Each tick is a MousePresence Detected event" in result.html

    def test_all_svgs_parse(self, tmp_path):
        ctx = _multi_day_ctx(tmp_path, n_days=3)
        result = actogram_section(ctx)
        svgs = re.findall(r"<svg.*?</svg>", result.html, re.DOTALL)
        assert len(svgs) == 1
        for s in svgs:
            ET.fromstring(s)

    def test_no_light_dark_shading(self, tmp_path):
        """The rig never records the facility's light schedule, so the
        actogram must not draw a light/dark cycle: shading from a fixed
        clock-time assumption would read as measured data. Time of day is
        on the axis; a reader applies their own light cycle to it."""
        ctx = _multi_day_ctx(tmp_path, n_days=3)
        result = actogram_section(ctx)
        assert "actogram-night" not in result.html
        assert "lights off" not in result.html.lower()
        assert "<rect" not in result.html

    def test_stale_lights_options_are_ignored_not_rendered(self, tmp_path):
        """A design JSON written before the shading was removed must not
        resurrect it (or fail) — the options are simply unread now."""
        ctx = _multi_day_ctx(tmp_path, n_days=3, opts={"lights_on": 7.0, "lights_off": 19.0})
        result = actogram_section(ctx)
        assert result.empty is False
        assert "actogram-night" not in result.html
        assert "19:00" not in result.html
        assert "07:00" not in result.html

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
        assert "MousePresence Detected" in result.html

    def test_event_names_option_plots_a_different_proxy(self, tmp_path):
        day_ms = 24 * 3600 * 1000
        t0 = 1_700_000_000_000
        rows = [
            input_changed_row(t0 + day * day_ms, 1, 4, True, "MousePresence Detected")
            for day in range(4)
        ] + [
            can_event_row(t0 + day * day_ms + 3600 * 1000, 1, CanEvent.PelletTaken, bytes([1, 0, 1]))
            for day in range(4)
        ]
        path = write_session(tmp_path, rows, session="MD")
        loaded, _, _ = load_rows(path)
        runs = split_runs(loaded, [], path)
        metrics = [compute_run_metrics(r) for r in runs]
        presence = SectionContext(runs=runs, metrics=metrics, combined=False,
                                  align="relative", opts={})
        takes = SectionContext(runs=runs, metrics=metrics, combined=False,
                               align="relative",
                               opts={"event_names": ["Pellet Taken"]})
        presence_html = actogram_section(presence).html
        takes_result = actogram_section(takes)
        takes_html = takes_result.html
        assert takes_result.title == "Actogram — Pellet Taken"
        assert "MousePresence Detected" in presence_html
        assert "Pellet Taken" in takes_html
        assert "MousePresence Detected" not in takes_html
        # 4 days × 1 take each, plus the SVG title — not the 4 presence ticks.
        assert takes_html.count("<title>") == 4 + 1
        assert presence_html.count("<title>") == 4 + 1

    def test_event_names_union_pools_several_events_as_one_series(self, tmp_path):
        day_ms = 24 * 3600 * 1000
        t0 = 1_700_000_000_000
        rows = [
            can_event_row(t0 + day * day_ms, 1, CanEvent.PelletTaken, bytes([1, 0, 1]))
            for day in range(3)
        ] + [
            can_event_row(t0 + day * day_ms + 1800 * 1000, 1, CanEvent.DomeOpened, bytes([1, 0, 1]))
            for day in range(3)
        ]
        path = write_session(tmp_path, rows, session="MD")
        loaded, _, _ = load_rows(path)
        runs = split_runs(loaded, [], path)
        metrics = [compute_run_metrics(r) for r in runs]
        ctx = SectionContext(
            runs=runs, metrics=metrics, combined=False, align="relative",
            opts={"event_names": ["Pellet Taken", "Dome Opened"]},
        )
        result = actogram_section(ctx)
        assert result.empty is False
        assert result.title == "Actogram — Pellet Taken, Dome Opened"
        assert "Pellet Taken, Dome Opened" in result.html
        # Both event types pooled into one tick series, not two.
        assert result.html.count("<title>") == 3 * 2 + 1

    def test_event_names_with_no_matching_events_is_empty(self, tmp_path):
        ctx = _multi_day_ctx(tmp_path, n_days=4, opts={"event_names": ["Pellet Taken"]})
        result = actogram_section(ctx)
        assert result.empty is True
