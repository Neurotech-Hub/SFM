"""Tests for sfm_analysis.report.explorer's payload builder and
explorer_render's embeddable widget -- and, at the report level, for the
timeline.explorer section it's embedded through (sections/timeline.py)."""

import html.parser
import json
import re

from report_fixtures import bandit_run, write_session

from sfm_analysis.report.loader import load_rows
from sfm_analysis.report.metrics import compute_run_metrics
from sfm_analysis.report.explorer import build_explorer_payload
from sfm_analysis.report.explorer_render import (
    EXPLORER_CSS,
    EXPLORER_JS,
    explorer_bootstrap_js,
    explorer_widget_html,
)
from sfm_analysis.report.render import render_report_html
from sfm_analysis.report.schema import resolve_design
from sfm_analysis.report.session import split_runs
from sfm_analysis.report import style

_URL_ATTR = re.compile(r"""(?:href|src|srcset|action|poster|background)\s*=\s*["']([^"']*)["']""", re.I)
_REMOTE_SCHEMES = ("http://", "https://", "//", "ftp://", "file://")


def _run_and_metrics(tmp_path, n_trials=4, session="EXP"):
    rows = bandit_run(n_trials=n_trials, session=session)
    path = write_session(tmp_path, rows, session=session)
    loaded, _, _ = load_rows(path)
    run = split_runs(loaded, [], path)[0]
    return run, compute_run_metrics(run)


def _runs(tmp_path, session="EXP", n_trials=4):
    rows = bandit_run(n_trials=n_trials, session=session)
    path = write_session(tmp_path, rows, session=session)
    loaded, _, _ = load_rows(path)
    return split_runs(loaded, [], path)


def _script_tag_events(doc: str):
    """Real <script> start/end tags as the HTML parser sees them -- not a
    substring count, which can't tell a real tag from harmless text
    sitting inside a script element's own content (a literal "<script>"
    with no leading slash inside a script element's text does not open a
    nested element or end anything; only "</script" closes it)."""
    starts, ends = [], []

    class _Counter(html.parser.HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "script":
                starts.append(True)

        def handle_endtag(self, tag):
            if tag == "script":
                ends.append(True)

    _Counter(convert_charrefs=True).feed(doc)
    return starts, ends


class TestBuildExplorerPayload:
    def test_is_json_serializable(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        json.dumps(payload)  # raises on anything not JSON-safe

    def test_meta_matches_the_run(self, tmp_path):
        run, m = _run_and_metrics(tmp_path, session="MyRun")
        payload = build_explorer_payload(run, m)
        meta = payload["meta"]
        assert meta["session"] == "MyRun"
        assert meta["run_id"] == run.run_id
        assert meta["duration_s"] == max(run.duration_s, 1.0)
        assert meta["t0_iso"] == run.rows[0].iso
        assert meta["nodes"] == sorted(set(run.nodes))

    def test_lanes_match_timeline_data(self, tmp_path):
        from sfm_analysis.report.timeline_data import build_lanes
        run, m = _run_and_metrics(tmp_path)
        nodes = sorted(set(run.nodes) | {r.node_id for r in run.rows if r.node_id != 0})
        payload = build_explorer_payload(run, m)
        assert payload["meta"]["lanes"] == build_lanes(nodes)

    def test_span_and_mark_counts_match_panel_marks_spans(self, tmp_path):
        from sfm_analysis.report.timeline_data import panel_marks_spans
        run, m = _run_and_metrics(tmp_path)
        nodes = sorted(set(run.nodes) | {r.node_id for r in run.rows if r.node_id != 0})
        duration = max(run.duration_s, 1.0)
        spans, marks = panel_marks_spans(run, m, nodes, 0.0, duration)
        payload = build_explorer_payload(run, m)
        assert len(payload["spans"]) == len(spans)
        assert len(payload["marks"]) == len(marks)

    def test_palette_matches_style_module(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        assert payload["style"]["palette"] == list(style.PALETTE)

    def test_span_titles_are_interned_not_repeated(self, tmp_path):
        run, m = _run_and_metrics(tmp_path, n_trials=6)
        payload = build_explorer_payload(run, m)
        # "presence" appears on every presence span -- confirm it's a
        # single string-pool entry referenced by index, not repeated text.
        assert payload["strings"].count("presence") <= 1
        title_indices = {sp[6] for sp in payload["spans"]}
        assert all(0 <= i < len(payload["strings"]) for i in title_indices)

    def test_mark_title_indices_are_valid(self, tmp_path):
        run, m = _run_and_metrics(tmp_path, n_trials=6)
        payload = build_explorer_payload(run, m)
        for mk in payload["marks"]:
            title_idx = mk[4]
            assert 0 <= title_idx < len(payload["strings"])

    def test_span_fields_are_lane_t0_t1_key_hatch_openended_titleidx(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        for sp in payload["spans"]:
            assert len(sp) == 7
            lane, t0, t1, key, hatch, open_ended, title_idx = sp
            assert isinstance(lane, int)
            assert t0 <= t1
            assert hatch in (0, 1)
            assert open_ended in (0, 1)

    def test_mark_fields_are_lane_t_glyph_key_titleidx(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        for mk in payload["marks"]:
            assert len(mk) == 5
            lane, t, glyph, key, title_idx = mk
            assert isinstance(lane, int)
            assert isinstance(glyph, str)

    def test_utc_offset_none_when_unknown(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        assert payload["meta"]["utc_offset_s"] is None


class TestExplorerWidget:
    """Unit tests on the reusable widget module itself (explorer_render.py),
    independent of how a section assembles several instances into a page."""

    def test_widget_html_scoped_under_dom_id(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        markup = explorer_widget_html(payload, dom_id="sfm-explorer-0")
        assert 'id="sfm-explorer-0"' in markup
        assert 'class="sfm-explorer"' in markup
        # No inline <script>/<style> of its own -- those are assembled
        # once per document by explorer_section, not per widget.
        assert "<script" not in markup
        assert "<style" not in markup

    def test_css_scoped_under_sfm_explorer(self):
        # Every rule must be scoped so N widgets (and the report's own
        # unrelated styles) can't bleed into each other.
        for rule in re.findall(r"([^\n{}]+)\s*\{", EXPLORER_CSS):
            rule = rule.strip()
            if not rule:
                continue
            assert ".sfm-explorer" in rule, f"unscoped rule: {rule!r}"

    def test_bootstrap_js_round_trips_entries(self, tmp_path):
        run, m = _run_and_metrics(tmp_path, session="RoundTrip")
        payload = build_explorer_payload(run, m)
        entries = [{"dom_id": "sfm-explorer-0", "payload": payload}]
        js = explorer_bootstrap_js(entries)
        match = re.search(r"window\.__SFM_EXPLORERS__ = \(window\.__SFM_EXPLORERS__ \|\| \[\]\)\.concat\((.*?)\);\n",
                           js, re.DOTALL)
        assert match is not None
        decoded = json.loads(match.group(1))
        assert decoded[0]["dom_id"] == "sfm-explorer-0"
        assert decoded[0]["payload"]["meta"]["session"] == "RoundTrip"

    def test_bootstrap_js_escapes_dangerous_script_close(self, tmp_path):
        """The payload's string pool can carry arbitrary event/detail text
        (from CSV fields_json, not under this code's control) -- a title
        containing the literal text "</script" must not be able to close
        the enclosing <script> element early, regardless of it sitting
        inside a JS string literal, since the HTML parser tokenizes the
        closing tag before any JS ever runs."""
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        payload["strings"][0] = "</script><script>window.pwned=1</script>"
        entries = [{"dom_id": "sfm-explorer-0", "payload": payload}]
        js = explorer_bootstrap_js(entries)

        doc = f"<html><body><script>{EXPLORER_JS}{js}</script></body></html>"
        starts, ends = _script_tag_events(doc)
        assert len(starts) == 1 and len(ends) == 1, "the injected text opened/closed a real script element"

        match = re.search(r"\.concat\((.*?)\);\n", js, re.DOTALL)
        decoded = json.loads(match.group(1).replace("<\\/script", "</script").replace("<\\!--", "<!--"))
        assert decoded[0]["payload"]["strings"][0] == "</script><script>window.pwned=1</script>"


class TestExplorerSectionInReport:
    """Integration tests at the level a real report is built -- one
    <script> for the whole document, one widget per run, and the
    session_raster/timeline.explorer interplay (print-only wrapping,
    --no-explorer)."""

    def _render(self, runs, *, include_explorer=True):
        design = resolve_design("default")
        return render_report_html(runs, design, include_explorer=include_explorer)

    def test_exactly_one_inline_script(self, tmp_path):
        runs = _runs(tmp_path)
        html_doc = self._render(runs)
        starts, ends = _script_tag_events(html_doc)
        assert len(starts) == 1 and len(ends) == 1

    def test_no_remote_origins(self, tmp_path):
        runs = _runs(tmp_path)
        html_doc = self._render(runs)
        assert "javascript:" not in html_doc.lower()
        assert "@import" not in html_doc
        assert not re.search(r"url\(\s*['\"]?(?:https?:)?//", html_doc, re.I)
        for m in _URL_ATTR.finditer(html_doc):
            url = m.group(1).strip().lower()
            assert not url.startswith(_REMOTE_SCHEMES), f"external resource: {url!r}"

    def test_one_widget_per_run_with_unique_dom_ids(self, tmp_path):
        # Two real runs (each with its own nodes/trials), not one real run
        # plus an abortive no-node restart -- explorer_section skips a run
        # with no nodes, same as session_raster_section does.
        rows_a = bandit_run(n_trials=2, session="Multi", run_id=1, t0_ms=1_700_000_000_000)
        rows_b = bandit_run(n_trials=2, session="Multi", run_id=2, t0_ms=1_800_000_000_000)
        path = write_session(tmp_path, rows_a + rows_b, session="Multi")
        loaded, _, _ = load_rows(path)
        runs = split_runs(loaded, [], path)
        assert len(runs) == 2

        html_doc = self._render(runs)
        dom_ids = re.findall(r'class="sfm-explorer" id="([^"]+)"', html_doc)
        assert len(dom_ids) == len(runs)
        assert len(set(dom_ids)) == len(dom_ids)

    def test_no_explorer_flag_yields_zero_scripts(self, tmp_path):
        runs = _runs(tmp_path)
        html_doc = self._render(runs, include_explorer=False)
        starts, ends = _script_tag_events(html_doc)
        assert starts == [] and ends == []
        assert "sfm-explorer" not in html_doc

    def test_session_raster_is_print_only_when_explorer_active(self, tmp_path):
        runs = _runs(tmp_path)
        with_explorer = self._render(runs, include_explorer=True)
        without_explorer = self._render(runs, include_explorer=False)

        assert "Session Timeline (printed panels)" in with_explorer
        assert 'class="print-only"' in with_explorer

        assert "Session Timeline (printed panels)" not in without_explorer
        assert "<h2>Session Timeline</h2>" in without_explorer

    def test_deterministic(self, tmp_path):
        runs = _runs(tmp_path)
        html1 = self._render(runs)
        html2 = self._render(runs)
        assert html1 == html2

    def test_well_formed_enough_for_html_parser(self, tmp_path):
        runs = _runs(tmp_path)
        doc = self._render(runs)

        class _Checker(html.parser.HTMLParser):
            def error(self, message):  # pragma: no cover - py<3.10 shim, unused on 3.9+
                raise AssertionError(message)

        _Checker(convert_charrefs=True).feed(doc)  # raises on malformed markup
