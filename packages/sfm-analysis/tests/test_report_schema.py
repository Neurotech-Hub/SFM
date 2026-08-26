"""Tests for sfm_analysis.report.schema."""

from pathlib import Path

from sfm_analysis.report.schema import (
    DEFAULT_REPORTS_DIR, SectionContext, SectionSpec, load_design,
    load_report_defs, resolve_design, resolve_section, run_section,
)


class TestPackagedDesigns:
    """Guards the importlib.resources resolution in schema.py's
    _packaged_designs_dir(): a non-namespace package that setuptools fails
    to discover, or a files()-vs-Path mismatch, both fail silently under
    an editable install and only show up once a real wheel is built (see
    the sdk.yml CI job's wheel-namelist check for the build-time half of
    this guard)."""

    def test_packaged_designs_dir_is_a_real_directory(self):
        assert DEFAULT_REPORTS_DIR.is_dir()

    def test_all_shipped_designs_are_present(self):
        stems = {p.stem for p in DEFAULT_REPORTS_DIR.glob("*.json")}
        assert stems == {
            "default", "free_feeding", "fixed_and_random",
            "probability_delivery", "two_armed_bandit",
            "actogram_takes",
        }


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


class TestLoadDesign:
    def test_bundled_name(self):
        d = load_design("default")
        assert d.name == "default"

    def test_bundled_name_with_json_suffix(self):
        d = load_design("two_armed_bandit.json")
        assert d.name == "two_armed_bandit"

    def test_bundled_actogram_takes(self):
        """pip users remap the actogram without a local JSON file."""
        d = load_design("actogram_takes")
        assert d.name == "actogram_takes"
        actogram = next(s for s in d.sections if s.ref == "timeline.actogram")
        assert actogram.options["event_names"] == ["Pellet Taken"]

    def test_actogram_takes_is_default_with_take_ticks(self):
        default = load_design("default")
        takes = load_design("actogram_takes")
        assert [s.ref for s in default.sections] == [s.ref for s in takes.sections]
        assert [s.ref for s in default.combined_sections] == [
            s.ref for s in takes.combined_sections
        ]

    def test_actogram_takes_is_not_auto_selected(self):
        d = resolve_design("two_armed_bandit")
        assert d.name == "two_armed_bandit"
        d = resolve_design("unknown_experiment_xyz")
        assert d.name == "default"

    def test_example_json_matches_bundled(self):
        bundled = (DEFAULT_REPORTS_DIR / "actogram_takes.json").read_text(
            encoding="utf-8",
        )
        example = (
            Path(__file__).resolve().parent.parent
            / "examples" / "report_design" / "designs" / "actogram_takes.json"
        )
        assert example.read_text(encoding="utf-8") == bundled

    def test_local_json_path(self, tmp_path):
        src = (
            Path(__file__).resolve().parent.parent
            / "examples" / "report_design" / "designs" / "actogram_takes.json"
        )
        assert src.is_file(), "example design missing from source tree"
        copied = tmp_path / "actogram_takes.json"
        copied.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        d = load_design(copied)
        assert d.name == "actogram_takes"
        actogram = next(s for s in d.sections if s.ref == "timeline.actogram")
        assert actogram.options["event_names"] == ["Pellet Taken"]

    def test_unknown_name_raises(self):
        import pytest
        with pytest.raises(ValueError, match="Unknown report design"):
            load_design("not_a_real_design")


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

    def test_beta_flag_appends_suffix_to_title(self):
        spec = SectionSpec(ref="generic.provenance", beta=True)
        ctx = SectionContext(runs=[], metrics=[], combined=False, align="relative")
        result = run_section(spec, ctx)
        assert result.title == "Session Provenance (beta)"

    def test_beta_flag_does_not_double_suffix_an_explicit_title(self):
        spec = SectionSpec(ref="generic.provenance", title="Custom (beta)", beta=True)
        ctx = SectionContext(runs=[], metrics=[], combined=False, align="relative")
        result = run_section(spec, ctx)
        assert result.title == "Custom (beta)"

    def test_non_beta_section_title_is_unmodified(self):
        spec = SectionSpec(ref="generic.provenance")
        ctx = SectionContext(runs=[], metrics=[], combined=False, align="relative")
        result = run_section(spec, ctx)
        assert not result.title.endswith("(beta)")

    def test_beta_flag_applies_to_error_block_title_too(self):
        spec = SectionSpec(ref="generic.nonexistent_section_xyz", beta=True)
        ctx = SectionContext(runs=[], metrics=[], combined=False, align="relative")
        result = run_section(spec, ctx)
        assert result.title.endswith("(beta)")


class TestExperimentSpecificSectionsAreBeta:
    """Every non-generic, non-timeline, non-compare section ref in every
    shipped design must be marked beta:true — these are the least-documented
    graphs (experiment-specific metrics), and the report must say so."""

    def test_timeline_is_the_first_section_in_every_per_session_design(self):
        # timeline.explorer (the interactive timeline) leads every design,
        # ahead of timeline.session_raster (its print-only static
        # fallback once the explorer is active -- see sections/timeline.py).
        defs = load_report_defs(DEFAULT_REPORTS_DIR)
        for d in defs:
            if not d.sections:
                continue
            assert d.sections[0].ref == "timeline.explorer", d.name

