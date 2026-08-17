"""Tests for base_station.report.schema."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from base_station.report.schema import (  # noqa: E402
    DEFAULT_REPORTS_DIR, SectionContext, SectionSpec, load_report_defs,
    resolve_design, resolve_section, run_section,
)


class TestLoadReportDefs:
    def test_every_design_in_reports_dir_loads(self):
        defs = load_report_defs(DEFAULT_REPORTS_DIR)
        assert len(defs) >= 1
        names = {d.name for d in defs}
        assert "default" in names

    def test_every_section_ref_resolves(self):
        """Catches a typo'd section name at test time, not in the lab."""
        defs = load_report_defs(DEFAULT_REPORTS_DIR)
        for d in defs:
            for spec in d.sections + d.combined_sections:
                resolve_section(spec.ref)  # raises on typo/missing


class TestResolveDesign:
    def test_matches_by_name(self):
        d = resolve_design("default")
        assert d.name == "default"

    def test_unknown_experiment_falls_back_to_default(self):
        d = resolve_design("totally_made_up_experiment_xyz")
        assert d.name == "default"

    def test_missing_reports_dir_does_not_raise(self, tmp_path):
        d = resolve_design("anything", directory=tmp_path / "nope")
        assert d.name == "default"


class TestRunSection:
    def test_failing_section_renders_error_block_not_exception(self):
        spec = SectionSpec(ref="generic.nonexistent_section_xyz")
        ctx = SectionContext(runs=[], metrics=[], combined=False, align="relative")
        result = run_section(spec, ctx)
        assert result is not None
        assert "failed to render" in result.html
        assert "<pre>" in result.html

    def test_working_section_returns_result(self):
        spec = SectionSpec(ref="generic.provenance")
        ctx = SectionContext(runs=[], metrics=[], combined=False, align="relative")
        result = run_section(spec, ctx)
        assert result is not None
        assert result.section_id == "generic.provenance"
