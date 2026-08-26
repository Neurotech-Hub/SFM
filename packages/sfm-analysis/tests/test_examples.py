"""Runs every examples/analysis/*.py recipe as a subprocess and pins its
output against the bundled demo session's known ground truth.

These exist for two reasons: they're the copy-paste starting points the
README's cookbook links to, and running them here means a change to
metrics.py or the analysis API that breaks a documented recipe fails CI
instead of being discovered by the next person who copies one.
"""

import subprocess
import sys
from pathlib import Path

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "analysis"


def _run(name: str) -> subprocess.CompletedProcess:
    script = _EXAMPLES_DIR / name
    assert script.exists(), f"missing example script: {script}"
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=30,
    )


def test_dome_openings_during_presence():
    result = _run("dome_openings_during_presence.py")
    assert result.returncode == 0, result.stderr
    assert "38 dome-open bouts" in result.stdout
    assert "38 overlap window(s)" in result.stdout


def test_retrieval_latency_by_node():
    result = _run("retrieval_latency_by_node.py")
    assert result.returncode == 0, result.stderr
    assert "node 1:" in result.stdout
    assert "node 2:" in result.stdout
    assert "38 dispense cycles" in result.stdout


def test_takes_after_fault():
    result = _run("takes_after_fault.py")
    assert result.returncode == 0, result.stderr
    assert "1 fault interval(s)" in result.stdout
    assert "1 pellet take(s) within 30s after a fault started" in result.stdout


def test_exp_events_by_name():
    result = _run("exp_events_by_name.py")
    assert result.returncode == 0, result.stderr
    assert "141 EXP row(s), 20 distinct name(s)" in result.stdout
    assert "19  bandit_trial" in result.stdout
    assert "bandit_trial_end: 17 (valid=17)" in result.stdout


def test_actogram_by_event():
    result = _run("actogram_by_event.py")
    assert result.returncode == 0, result.stderr
    assert "actogram event mapping" in result.stdout
    assert "MousePresence Detected:" in result.stdout
    assert "Pellet Taken:" in result.stdout
    assert "Dome Opened, Pellet Taken:" in result.stdout
    assert "sfm-report MySession --design actogram_takes --open" in result.stdout


def test_actogram_by_event_as_installed_module():
    """The pip-install path: no source-tree examples/ directory required."""
    result = subprocess.run(
        [sys.executable, "-m", "sfm_analysis.examples.actogram_by_event"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "actogram event mapping" in result.stdout
    assert "sfm-report MySession --design actogram_takes --open" in result.stdout
