"""Tests for run_report.py -- the thin wrapper around sfm_analysis.cli.report.

All of the CLI's actual argument-parsing and report-generation behavior is
tested against sfm_analysis.cli.report directly in the SDK's own test
suite (packages/sfm-analysis/tests/test_cli_report.py). What's left to
verify here is only the shim itself: that it still runs as a script, and
that it still passes the base station's own (writable-probing) log-dir
resolver through rather than falling back to the SDK's.
"""

import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_SCRIPT = _HERE / "run_report.py"


def _run(args):
    return subprocess.run(
        [sys.executable, str(_SCRIPT)] + args,
        cwd=_HERE, capture_output=True, text=True, timeout=30,
    )


def test_shim_lists_designs():
    """No --log-dir given, so this also exercises the shim's import chain
    (base_station.storage -> sfm_analysis.cli.report) end to end."""
    result = _run(["--list-designs"])
    assert result.returncode == 0
    assert "default" in result.stdout
    assert "two_armed_bandit" in result.stdout


def test_shim_lists_sessions_with_explicit_log_dir(tmp_path):
    """--log-dir given explicitly, so this proves --log-dir still
    overrides the base station's injected resolver through the shim,
    exactly as it did before run_report.py moved its implementation
    into the SDK."""
    result = _run(["--list", "--log-dir", str(tmp_path)])
    assert result.returncode == 0
    assert str(tmp_path) in result.stdout
    assert "No sessions found" in result.stdout


def test_shim_uses_base_station_resolver_not_sdk_resolver(monkeypatch, tmp_path):
    """Force $SFM_LOG_DIR (the SDK resolver's override) at a directory the
    base station resolver would never pick, and confirm --list (with no
    --log-dir) ignores it -- proving the shim really does inject
    base_station.storage.default_log_dir rather than silently falling
    back to sfm_analysis.storage.default_log_dir."""
    sdk_only_dir = tmp_path / "sdk_would_pick_this"
    sdk_only_dir.mkdir()
    env = dict(os.environ, SFM_LOG_DIR=str(sdk_only_dir))
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--list"],
        cwd=_HERE, capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0
    assert str(sdk_only_dir) not in result.stdout
