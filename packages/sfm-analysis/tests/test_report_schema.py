"""Tests for sfm_analysis.report.schema."""


from sfm_analysis.report.schema import (
    DEFAULT_REPORTS_DIR, SectionContext, SectionSpec, load_report_defs,
    resolve_design, resolve_section, run_section,
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

    def test_all_five_shipped_designs_are_present(self):
        stems = {p.stem for p in DEFAULT_REPORTS_DIR.glob("*.json")}
        assert stems == {
            "default", "free_feeding", "fixed_and_random",
            "probability_delivery", "two_armed_bandit",
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

    def test_all_experiment_specific_refs_are_marked_beta(self):
        defs = load_report_defs(DEFAULT_REPORTS_DIR)
        general_prefixes = ("generic.", "timeline.", "compare.")
        offenders = []
        for d in defs:
            for spec in d.sections + d.combined_sections:
                if spec.ref.startswith(general_prefixes):
                    continue
                if not spec.beta:
                    offenders.append((d.name, spec.ref))
        assert offenders == []

    def test_timeline_is_the_first_section_in_every_per_session_design(self):
        defs = load_report_defs(DEFAULT_REPORTS_DIR)
        for d in defs:
            if not d.sections:
                continue
            assert d.sections[0].ref == "timeline.session_raster", d.name

