"""Tests for sfm_analysis.report.explorer's payload builder and
explorer_render's HTML/JS document."""

import json
import re

from report_fixtures import bandit_run, write_session

from sfm_analysis.report.loader import load_rows
from sfm_analysis.report.metrics import compute_run_metrics
from sfm_analysis.report.explorer import build_explorer_payload
from sfm_analysis.report.explorer_render import render_explorer_html
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


class TestRenderExplorerHtml:
    def test_exactly_one_inline_script(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        html = render_explorer_html(payload)
        assert html.count("<script") == 1
        assert html.count("</script>") == 1

    def test_no_remote_origins(self, tmp_path):
        """Self-contained means offline-capable, not link-free -- unlike
        the printed report, this document legitimately ships a <script>
        (that's the whole point of it). The invariant is scoped to
        "reaches no remote origin", same reasoning as
        test_report_render.py's scheme-based check."""
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        html = render_explorer_html(payload, report_href="Weird_run1_report.html")

        assert "javascript:" not in html.lower()
        assert "@import" not in html
        assert not re.search(r"url\(\s*['\"]?(?:https?:)?//", html, re.I)
        for m2 in _URL_ATTR.finditer(html):
            url = m2.group(1).strip().lower()
            assert not url.startswith(_REMOTE_SCHEMES), f"external resource: {url!r}"

    def test_payload_json_round_trips(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        html = render_explorer_html(payload)
        match = re.search(r"const DATA = (.*?);\n", html, re.DOTALL)
        assert match is not None
        parsed = json.loads(match.group(1))
        assert parsed["meta"]["session"] == run.session
        assert len(parsed["spans"]) == len(payload["spans"])

    def test_deterministic(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        html1 = render_explorer_html(payload, report_href="r.html")
        html2 = render_explorer_html(payload, report_href="r.html")
        assert html1 == html2

    def test_title_contains_session_and_run(self, tmp_path):
        run, m = _run_and_metrics(tmp_path, session="MyExplorerSession")
        payload = build_explorer_payload(run, m)
        html = render_explorer_html(payload)
        assert "MyExplorerSession" in html
        assert f"run {run.run_id}" in html

    def test_report_href_omitted_when_not_given(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        html = render_explorer_html(payload)
        assert "printable report" not in html

    def test_report_href_link_present_when_given(self, tmp_path):
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        html = render_explorer_html(payload, report_href="Weird_run1_report.html")
        assert 'href="Weird_run1_report.html"' in html

    def test_session_name_is_escaped_in_title_and_heading(self, tmp_path):
        # "&" and "<" reach the filesystem fine as a session name (unlike
        # "/", which report_fixtures.write_session uses as a path
        # separator, so no closing tag like "</em>" here) -- this
        # exercises the <title>/<h1> escaping. The same raw text
        # legitimately (and correctly) appears un-escaped inside the
        # <script> element's JSON payload -- JSON string data, not HTML
        # body text -- so the check is scoped to the HTML outside it.
        run, m = _run_and_metrics(tmp_path, session="A&B<em>not-italic")
        payload = build_explorer_payload(run, m)
        html = render_explorer_html(payload)
        before_script = html.split("<script", 1)[0]
        assert "A&amp;B" in before_script
        assert "<em>not-italic" not in before_script

    def _script_tag_events(self, doc: str):
        """Real <script> start/end tags as the HTML parser sees them --
        not a substring count, which can't tell a real tag from harmless
        text sitting inside the one genuine script element's content
        (see the test this backs: a literal "<script>" with no leading
        slash inside a script element's text does not open a nested
        element or end anything; only "</script" closes it)."""
        import html.parser
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

    def test_a_title_string_cannot_terminate_the_script_tag_early(self, tmp_path):
        """The payload's string pool can carry arbitrary event/detail
        text (from CSV fields_json, not under this code's control) -- a
        title containing the literal text "</script" must not be able to
        close the <script> element early regardless of it sitting inside
        a JS string literal, since the HTML parser tokenizes the closing
        tag before any JS ever runs. Built directly (not via a session
        name, which becomes a filename and can't contain "/")."""
        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        payload["strings"][0] = "</script><script>window.pwned=1</script>"
        html = render_explorer_html(payload)

        starts, ends = self._script_tag_events(html)
        assert len(starts) == 1 and len(ends) == 1, "the injected text opened/closed a real script element"

        # The escaped payload must still be valid JSON once unescaped,
        # and the dangerous text must survive intact as DATA rather than
        # being dropped or mangled.
        match = re.search(r"const DATA = (.*?);\n", html, re.DOTALL)
        decoded = json.loads(match.group(1).replace("<\\/script", "</script").replace("<\\!--", "<!--"))
        assert decoded["strings"][0] == "</script><script>window.pwned=1</script>"

    def test_well_formed_enough_for_html_parser(self, tmp_path):
        import html.parser

        run, m = _run_and_metrics(tmp_path)
        payload = build_explorer_payload(run, m)
        doc = render_explorer_html(payload, report_href="r.html")

        class _Checker(html.parser.HTMLParser):
            def error(self, message):  # pragma: no cover - py<3.10 shim, unused on 3.9+
                raise AssertionError(message)

        _Checker(convert_charrefs=True).feed(doc)  # raises on malformed markup


class TestBuildSessionExplorer:
    def test_writes_a_file_and_links_back_to_the_report(self, tmp_path):
        from sfm_analysis.report import build_session_explorer

        rows = bandit_run(n_trials=3, session="BuildExplorerTest")
        path = write_session(tmp_path, rows, session="BuildExplorerTest")
        out = build_session_explorer(path, out_path=tmp_path / "explorer.html", report_href="report.html")
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "BuildExplorerTest" in content
        assert 'href="report.html"' in content

    def test_default_out_path_includes_session_and_run(self, tmp_path):
        from sfm_analysis.report import build_session_explorer

        rows = bandit_run(n_trials=2, session="DefaultPathTest")
        path = write_session(tmp_path, rows, session="DefaultPathTest")
        out = build_session_explorer(path)
        assert out.name == "DefaultPathTest_run1_explorer.html"
        assert out.exists()

    def test_picks_the_run_with_the_most_rows_by_default(self, tmp_path):
        from report_fixtures import session_open_row
        from sfm_analysis.report import build_session_explorer

        rows = bandit_run(n_trials=4, session="MultiRun", t0_ms=1_700_000_000_000)
        abortive = [session_open_row(1_800_000_000_000, "MultiRun", run_id=2)]
        path = write_session(tmp_path, rows + abortive, session="MultiRun")

        out = build_session_explorer(path, out_path=tmp_path / "explorer.html")
        content = out.read_text(encoding="utf-8")
        assert "run 1" in content  # the real run, not the 1-row abortive run 2
