"""Real-data smoke test for the report tool.

Skipped cleanly on any machine without the actual field-recorded
EXP-Test-02.csv on its external drive (i.e. everywhere except this base
station). Where it does run, it is ground truth: these exact counts were
independently verified against the raw CSV during development (see the
session notes) — 321 rows split into 2 runs, 5 `Loaded` / 3 `Pellet Taken`
across both runs, 11 `Dome Opened` rows collapsing to 7 true openings
after milestone/edge dedup, 19 `script_stalled` events, and a first-trial
latency of ~1441s in run 1.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REAL_FILE = Path("/media/base-station-00/STORAGE/sfm_logs/EXP-Test-02.csv")

pytestmark = pytest.mark.skipif(not REAL_FILE.exists(), reason="real EXP-Test-02.csv not present on this machine")


def test_real_session_loads_and_splits_correctly():
    from base_station.log_manager import LogManager
    from base_station.report.loader import load_rows, load_heartbeats
    from base_station.report.session import split_runs

    rows, schema, warnings = load_rows(REAL_FILE)
    assert len(rows) == 321
    assert warnings == []

    hb = load_heartbeats(LogManager.heartbeat_path_for(REAL_FILE))
    runs = split_runs(rows, hb, REAL_FILE)
    assert len(runs) == 2
    assert all(r.experiment == "two_armed_bandit" for r in runs)

    total_taken = sum(len(r.by_event("Pellet Taken")) for r in runs)
    total_loaded = sum(len(r.by_event("Loaded")) for r in runs)
    assert total_taken == 3
    assert total_loaded == 5


def test_real_session_dome_dedup_matches_known_split():
    from base_station.log_manager import LogManager
    from base_station.report.loader import load_rows, load_heartbeats
    from base_station.report.session import split_runs

    rows, _, _ = load_rows(REAL_FILE)
    hb = load_heartbeats(LogManager.heartbeat_path_for(REAL_FILE))
    runs = split_runs(rows, hb, REAL_FILE)

    milestone = sum(1 for r in runs for row in r.can_events()
                    if row.event_name == "Dome Opened" and row.can_event and row.can_event.name == "DomeOpened")
    edge = sum(1 for r in runs for row in r.can_events()
              if row.event_name == "Dome Opened" and row.can_event and row.can_event.name == "InputChanged")
    assert milestone == 7
    assert edge == 4


def test_real_session_apparatus_health_and_first_trial_latency():
    from base_station.log_manager import LogManager
    from base_station.report.loader import load_rows, load_heartbeats
    from base_station.report.session import split_runs
    from base_station.report.metrics import compute_run_metrics

    rows, _, _ = load_rows(REAL_FILE)
    hb = load_heartbeats(LogManager.heartbeat_path_for(REAL_FILE))
    runs = split_runs(rows, hb, REAL_FILE)

    total_stalled = 0
    first_trial_t = None
    for run in runs:
        m = compute_run_metrics(run)
        total_stalled += len(m.health.get("script_stalled", []))
        if first_trial_t is None:
            trials = run.exp("trial")
            if trials:
                first_trial_t = trials[0].t

    assert total_stalled == 19
    assert first_trial_t is not None
    assert abs(first_trial_t - 1441.081) < 0.5


def test_real_session_generates_a_complete_report(tmp_path):
    from base_station.report import build_session_report

    out = build_session_report(REAL_FILE, out_path=tmp_path / "real_report.html")
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert len(content) > 10_000
    assert "EXP-Test-02" in content
    assert "<script" not in content

    import xml.etree.ElementTree as ET
    import re
    svgs = re.findall(r"<svg.*?</svg>", content, re.DOTALL)
    assert len(svgs) > 5
    for s in svgs:
        ET.fromstring(s)
