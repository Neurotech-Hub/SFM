"""Tests for sfm_analysis.report.naming."""


from sfm_analysis.report.naming import (
    NamingSettings, group_by, parse_session_name,
)


class TestParseSessionName:
    def test_cohort_subject_day_underscore(self):
        ident = parse_session_name("cohortA_M014_d3")
        assert ident.cohort == "cohortA"
        assert ident.subject == "M014"
        assert ident.day == 3
        assert ident.parsed is True

    def test_cohort_subject_day_hyphen_with_full_day_word(self):
        ident = parse_session_name("cohortA-M014-day3")
        assert ident.cohort == "cohortA"
        assert ident.subject == "M014"
        assert ident.day == 3

    def test_subject_day_only(self):
        ident = parse_session_name("M014_d3")
        assert ident.subject == "M014"
        assert ident.day == 3
        assert ident.cohort is None

    def test_subject_embedded_in_free_text(self):
        ident = parse_session_name("morning_run_M014_extra")
        assert ident.subject == "M014"

    def test_auto_named_sink_yields_date_only(self):
        ident = parse_session_name("session_20260812_150810")
        assert ident.date == "20260812"
        assert ident.subject is None

    def test_daily_session_yields_date(self):
        ident = parse_session_name("session_20260824")
        assert ident.date == "20260824"
        assert ident.subject is None
        assert ident.parsed is True

    def test_real_test_session_names_are_unparsed(self):
        for name in ("Exp-Test-01", "EXP-Test-02"):
            ident = parse_session_name(name)
            assert ident.subject is None
            assert ident.cohort is None
            assert ident.day is None
            assert ident.parsed is False
            assert ident.session == name
            assert ident.display_label == name

    def test_custom_pattern_overrides_defaults(self):
        ident = parse_session_name("XYZ123", patterns=[r"^(?P<subject>XYZ\d+)$"])
        assert ident.subject == "XYZ123"


class TestNamingSettings:
    def test_load_missing_file_returns_defaults(self, tmp_path):
        settings = NamingSettings.load(tmp_path / "nope.json")
        assert settings.patterns  # non-empty, from DEFAULT_PATTERNS

    def test_save_then_load_round_trips(self, tmp_path):
        path = tmp_path / "report_settings.json"
        settings = NamingSettings(patterns=[r"^(?P<subject>Z\d+)$"])
        settings.save(path)
        loaded = NamingSettings.load(path)
        assert loaded.patterns == [r"^(?P<subject>Z\d+)$"]

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "report_settings.json"
        path.write_text("{not valid json", encoding="utf-8")
        settings = NamingSettings.load(path)
        assert settings.patterns  # fell back, didn't raise


class TestGroupBy:
    def test_unparsed_sessions_grouped_under_empty_key(self):
        idents = [parse_session_name("Exp-Test-01"), parse_session_name("cohortA_M014_d3")]
        groups = group_by(idents, "subject")
        assert "" in groups
        assert "M014" in groups
        assert len(groups[""]) == 1
