"""Tests for sfm_analysis.cli.report (installed as the sfm-report console
script). Invoked via `-m` rather than a script path so this works
identically regardless of install mode (editable, wheel, git checkout) and
on every OS."""

import subprocess
import sys

from report_fixtures import bandit_run, legacy9_file, write_session

_PYTHON = sys.executable


def _run(args, cwd=None, input_text=None):
    return subprocess.run(
        [_PYTHON, "-m", "sfm_analysis.cli.report"] + args,
        cwd=cwd, capture_output=True, text=True, input=input_text, timeout=30,
    )


class TestListCommands:
    def test_list_sessions(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=1, session="X"), session="X")
        result = _run(["--list", "--log-dir", str(tmp_path)])
        assert result.returncode == 0
        assert "X" in result.stdout

    def test_list_empty_dir(self, tmp_path):
        result = _run(["--list", "--log-dir", str(tmp_path)])
        assert result.returncode == 0
        assert "No sessions found" in result.stdout

    def test_list_designs(self):
        result = _run(["--list-designs"])
        assert result.returncode == 0
        assert "default" in result.stdout

    def test_check_names(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=1, session="cohortA_M014_d3"), session="cohortA_M014_d3")
        result = _run(["--check-names", "--log-dir", str(tmp_path)])
        assert result.returncode == 0
        assert "M014" in result.stdout


class TestDemo:
    def test_demo_renders_without_a_log_dir_or_real_data(self, tmp_path):
        out = tmp_path / "demo.html"
        result = _run(["--demo", "--out", str(out)])
        assert result.returncode == 0, result.stderr
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "Bandit_test_01" in content
        assert len(content) > 5000

    def test_demo_with_explicit_target_errors(self):
        result = _run(["--demo", "SomeSession"])
        assert result.returncode == 2
        assert "--demo" in result.stderr

    def test_demo_with_all_errors(self):
        result = _run(["--demo", "--all"])
        assert result.returncode == 2
        assert "--demo" in result.stderr

    def test_demo_default_out_writes_to_cwd_not_the_installed_package(self, tmp_path):
        """DEMO_SESSION_PATH lives inside the installed sfm_analysis
        package itself; without this default-out override, --demo with
        no --out would try to write next to it -- typically unwritable
        on a real (non-editable) pip install."""
        result = _run(["--demo"], cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        written = list(tmp_path.glob("*_report.html"))
        assert len(written) == 1
        assert written[0].parent == tmp_path


class TestExplorer:
    """The interactive timeline is embedded directly in the one report
    file by default (sections/timeline.py's timeline.explorer) -- there
    is no longer a second, sibling explorer file to write or cross-link."""

    def test_default_run_writes_one_file_with_the_embedded_widget(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=3, session="ExplTest"), session="ExplTest")
        report_out = tmp_path / "out.html"
        result = _run(["ExplTest", "--log-dir", str(tmp_path), "--out", str(report_out)])
        assert result.returncode == 0, result.stderr

        assert report_out.exists()
        assert not (tmp_path / "out_explorer.html").exists()
        content = report_out.read_text(encoding="utf-8")
        assert content.lower().count("<script") == 1
        assert 'class="sfm-explorer"' in content

    def test_no_explorer_flag_yields_a_script_free_file(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=2, session="ExplDefault"), session="ExplDefault")
        result = _run(["ExplDefault", "--log-dir", str(tmp_path), "--no-explorer"], cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        reports_dir = tmp_path / "reports"
        report_file = reports_dir / "ExplDefault_run1_report.html"
        assert report_file.exists()
        content = report_file.read_text(encoding="utf-8")
        assert "<script" not in content.lower()
        assert 'class="sfm-explorer"' not in content

    def test_multiple_targets_and_out_dir_each_get_one_file(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
        write_session(tmp_path, bandit_run(n_trials=1, session="B"), session="B")
        out_dir = tmp_path / "out"
        result = _run(["A", "B", "--log-dir", str(tmp_path), "--out", str(out_dir)])
        assert result.returncode == 0, result.stderr
        assert (out_dir / "A_report.html").exists()
        assert (out_dir / "B_report.html").exists()
        assert not (out_dir / "A_explorer.html").exists()
        assert 'class="sfm-explorer"' in (out_dir / "A_report.html").read_text(encoding="utf-8")

    def test_demo_no_explorer(self, tmp_path):
        result = _run(["--demo", "--no-explorer"], cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        written = list(tmp_path.glob("*_report.html"))
        assert len(written) == 1
        assert "<script" not in written[0].read_text(encoding="utf-8").lower()


class TestGenerateReport:
    def test_single_session_writes_file_over_5kb(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=3, session="Sess01"), session="Sess01")
        out = tmp_path / "out.html"
        result = _run(["Sess01", "--log-dir", str(tmp_path), "--out", str(out)])
        assert result.returncode == 0, result.stderr
        assert out.exists()
        assert out.stat().st_size > 5000
        assert "Sess01" in out.read_text(encoding="utf-8")

    def test_combine_two_sessions_mentions_both(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=2, session="A", t0_ms=1_700_000_000_000), session="A")
        write_session(tmp_path, bandit_run(n_trials=2, session="B", t0_ms=1_700_100_000_000), session="B")
        out = tmp_path / "combined.html"
        result = _run(["A", "B", "--combine", "--log-dir", str(tmp_path), "--out", str(out)])
        assert result.returncode == 0, result.stderr
        content = out.read_text(encoding="utf-8")
        assert "A" in content and "B" in content

    def test_no_match_exits_1(self, tmp_path):
        result = _run(["NoSuchSession", "--log-dir", str(tmp_path)])
        assert result.returncode == 1

    def test_all_combines_every_session_in_log_dir(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=2, session="A", t0_ms=1_700_000_000_000), session="A")
        write_session(tmp_path, bandit_run(n_trials=2, session="B", t0_ms=1_700_100_000_000), session="B")
        out = tmp_path / "all.html"
        result = _run(["--all", "--combine", "--log-dir", str(tmp_path), "--out", str(out)])
        assert result.returncode == 0, result.stderr
        content = out.read_text(encoding="utf-8")
        assert "A" in content and "B" in content

    def test_all_with_explicit_target_errors(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
        result = _run(["--all", "A", "--log-dir", str(tmp_path)])
        assert result.returncode == 2

    def test_since_filters_out_everything_in_the_future(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
        result = _run(["--all", "--since", "2099-01-01", "--log-dir", str(tmp_path)])
        assert result.returncode == 1

    def test_bad_align_value_errors(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
        result = _run(["A", "--align", "bogus", "--log-dir", str(tmp_path)])
        assert result.returncode == 2
        assert "--align" in result.stderr

    def test_align_event_syntax_accepted(self, tmp_path):
        write_session(tmp_path, bandit_run(n_trials=1, session="A"), session="A")
        out = tmp_path / "out.html"
        result = _run(["A", "--align", "event:trial", "--log-dir", str(tmp_path), "--out", str(out)])
        assert result.returncode == 0, result.stderr

    def test_legacy_schema_only_exits_2(self, tmp_path):
        legacy9_file(tmp_path, name="legacy")
        result = _run(["legacy", "--log-dir", str(tmp_path)])
        assert result.returncode == 2
        assert "unsupported schema" in result.stderr

    def test_legacy_mixed_with_good_file_still_succeeds(self, tmp_path):
        legacy9_file(tmp_path, name="legacy")
        write_session(tmp_path, bandit_run(n_trials=1, session="Good"), session="Good")
        out_dir = tmp_path / "out"
        result = _run(["legacy", "Good", "--log-dir", str(tmp_path), "--out", str(out_dir)])
        assert result.returncode == 0, result.stderr
        assert "unsupported schema" in result.stderr
        assert any(out_dir.glob("*.html"))
