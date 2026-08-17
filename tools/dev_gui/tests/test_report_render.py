"""Tests for base_station.report.render and the report/__init__.py public API."""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from report_fixtures import bandit_run, write_session  # noqa: E402

from base_station.report import build_session_report  # noqa: E402
from base_station.report.loader import load_rows  # noqa: E402
from base_station.report.render import render_report_html  # noqa: E402
from base_station.report.schema import (  # noqa: E402
    ReportDef, SectionSpec, resolve_design,
)
from base_station.report.session import split_runs  # noqa: E402


def _runs(tmp_path, session="Weird&Session<Name>"):
    rows = bandit_run(n_trials=3, session=session)
    path = write_session(tmp_path, rows, session=session)
    loaded, _, _ = load_rows(path)
    return split_runs(loaded, [], path)


class TestRenderReportHtml:
    def test_self_contained_no_script_or_external_resources(self, tmp_path):
        runs = _runs(tmp_path)
        design = resolve_design("two_armed_bandit")
        html = render_report_html(runs, design)
        assert "<script" not in html
        assert "href=" not in html
        assert " src=" not in html

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
