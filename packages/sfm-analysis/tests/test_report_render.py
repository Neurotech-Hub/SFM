"""Tests for sfm_analysis.report.render and the report/__init__.py public API."""

import re

from report_fixtures import bandit_run, write_session

from sfm_analysis.report import build_session_report
from sfm_analysis.report.loader import load_rows
from sfm_analysis.report.render import render_report_html
from sfm_analysis.report.schema import (
    ReportDef, SectionSpec, resolve_design,
)
from sfm_analysis.report.session import split_runs


def _runs(tmp_path, session="Weird&Session<Name>"):
    rows = bandit_run(n_trials=3, session=session)
    path = write_session(tmp_path, rows, session=session)
    loaded, _, _ = load_rows(path)
    return split_runs(loaded, [], path)


_URL_ATTR = re.compile(r"""(?:href|src|srcset|action|poster|background)\s*=\s*["']([^"']*)["']""", re.I)
_REMOTE_SCHEMES = ("http://", "https://", "//", "ftp://", "file://")


class TestRenderReportHtml:
    def test_self_contained_no_script_or_external_resources(self, tmp_path):
        """The printed report must render with the network unplugged.

        Checked by URL *scheme*, not by substring -- what render.py's
        module docstring actually promises is "no remote origin", not
        "no links at all". A relative link to a sibling explorer file
        (explorer_href) is allowed by design; anything that reaches
        off-machine is not.
        """
        runs = _runs(tmp_path)
        design = resolve_design("two_armed_bandit")
        html = render_report_html(runs, design)

        assert "<script" not in html.lower()
        assert "javascript:" not in html.lower()
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handler"
        assert "@import" not in html
        assert not re.search(r"url\(\s*['\"]?(?:https?:)?//", html, re.I)

        for m in _URL_ATTR.finditer(html):
            url = m.group(1).strip().lower()
            assert not url.startswith(_REMOTE_SCHEMES), f"external resource: {url!r}"

    def test_no_links_at_all_without_an_explicit_explorer_href(self, tmp_path):
        """Tripwire: today, with no explorer_href, the report emits zero
        <a> tags. This is not the real invariant (the scheme-based test
        above is) -- it just documents that the explorer link is fully
        opt-in, so a future caller that forgets to pass explorer_href
        gets the old fully-linkless document, not a broken one."""
        runs = _runs(tmp_path)
        html = render_report_html(runs, resolve_design("default"))
        assert "<a " not in html

    def test_explorer_href_is_embedded_as_a_relative_link(self, tmp_path):
        runs = _runs(tmp_path)
        html = render_report_html(runs, resolve_design("default"), explorer_href="Weird_run1_explorer.html")
        assert 'href="Weird_run1_explorer.html"' in html
        # The relative link must still pass the scheme check above.
        for m in _URL_ATTR.finditer(html):
            assert not m.group(1).lower().startswith(_REMOTE_SCHEMES)

    def test_print_rules_present(self, tmp_path):
        runs = _runs(tmp_path)
        html = render_report_html(runs, resolve_design("default"))
        assert "@media print" in html
        assert "page-break-inside" in html
        assert "@page" in html

    def test_session_name_is_escaped(self, tmp_path):
        runs = _runs(tmp_path, session="A&B<script>")
        html = render_report_html(runs, resolve_design("default"))
        assert "<script>" not in html.split("<style>")[1].split("</style>")[0]  # not injected as a tag
        assert "A&amp;B" in html or "A&#38;B" in html or "&amp;B" in html

    def test_defs_appears_exactly_once(self, tmp_path):
        runs = _runs(tmp_path)
        html = render_report_html(runs, resolve_design("two_armed_bandit"))
        assert html.count("<pattern id=\"hatch-0\"") == 1

    def test_failing_section_does_not_kill_the_report(self, tmp_path):
        runs = _runs(tmp_path)
        design = ReportDef(
            name="broken",
            label="Broken Design",
            sections=[
                SectionSpec(ref="generic.nonexistent_xyz"),
                SectionSpec(ref="generic.provenance"),
            ],
        )
        html = render_report_html(runs, design)
        assert "failed to render" in html
        assert "Session Provenance" in html

    def test_deterministic_apart_from_generated_at_line(self, tmp_path):
        runs = _runs(tmp_path)
        design = resolve_design("two_armed_bandit")
        html1 = render_report_html(runs, design)
        html2 = render_report_html(runs, design)
        strip = lambda h: re.sub(r"Generated [\d-]+ [\d:]+ UTC", "Generated X", h)
        assert strip(html1) == strip(html2)

    def test_output_is_well_formed_enough_to_extract_svgs(self, tmp_path):
        import xml.etree.ElementTree as ET
        runs = _runs(tmp_path)
        html = render_report_html(runs, resolve_design("default"))
        svg_blocks = re.findall(r"<svg.*?</svg>", html, re.DOTALL)
        assert len(svg_blocks) > 0
        for block in svg_blocks:
            ET.fromstring(block)


class TestBuildSessionReport:
    def test_writes_file_containing_session_name(self, tmp_path):
        rows = bandit_run(n_trials=2, session="TestSession01")
        csv_path = write_session(tmp_path, rows, session="TestSession01")
        out = build_session_report(csv_path, out_path=tmp_path / "out.html")
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "TestSession01" in content
        assert len(content) > 5000

    def test_explicit_design_name_selects_that_design(self, tmp_path):
        """Covers report/__init__.py's _resolve_design_for, which reads
        DEFAULT_REPORTS_DIR directly by filename stem -- a stat/read this
        test would not otherwise exercise at all. Without this test, a
        --design free_feeding call could silently fall through to the
        auto-resolved design (wrong report, exit code 0) with nothing to
        catch it.

        The session here is a bandit run (which auto-resolves to the
        two_armed_bandit design), so finding the free_feeding-only
        "Meal-Bout Analysis" section in the output proves --design
        actually overrode the auto-resolution rather than being ignored.
        """
        rows = bandit_run(n_trials=2, session="ForcedDesignTest")
        csv_path = write_session(tmp_path, rows, session="ForcedDesignTest")
        out = build_session_report(
            csv_path, out_path=tmp_path / "ff.html", design="free_feeding",
        )
        content = out.read_text(encoding="utf-8")
        assert "Meal-Bout Analysis" in content
        assert "Two-Armed Bandit" not in content
