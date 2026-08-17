"""Tests for run_report.py (the CLI)."""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from report_fixtures import bandit_run, legacy9_file, write_session  # noqa: E402

_HERE = Path(__file__).resolve().parent.parent
_SCRIPT = _HERE / "run_report.py"
_PYTHON = sys.executable


def _run(args, cwd=None, input_text=None):
    return subprocess.run(
        [_PYTHON, str(_SCRIPT)] + args,
        cwd=cwd or _HERE, capture_output=True, text=True, input=input_text, timeout=30,
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
