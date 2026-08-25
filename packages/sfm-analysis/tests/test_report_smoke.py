"""Real-data smoke test for the report pipeline.

Runs against a real field-recorded session bundled with the package
itself (``sfm_analysis.report.demo`` — the same file ``sfm-report --demo``
uses) so it executes on every machine and in CI, not just on the base
station that recorded it. These exact counts were independently verified
against the raw CSV during development:

  - 521 data rows split into 2 runs: run 1 is a 9-row abortive session
    (started and immediately stopped before any trial), run 2 is the real
    512-row, ~1291.8s two_armed_bandit session.
  - Run 2: 19 trials, 18 `Loaded` / 18 `Pellet Taken` / 19 `NoFeedPresented`.
  - Run 2: 38 `Dome Opened` rows collapsing to 34 true milestone openings
    (CanEvent.DomeOpened) + 4 InputChanged-edge duplicates after dedup —
    see loader.py's LogRow.can_event docstring for why these must not be
    counted together.
  - Run 2: one fault interval (`Fault: PelletLost` / `node_halted` at
    t=189.204 -> `node_recovered` at t=200.404).
  - Run 2's `session_end` fires at elapsed_s=668.442, but the run's last
    row (a trailing heartbeat) is at t=1291.8s -- so this file also
    exercises RunData.post_session flagging on real data.

Set SFM_SMOKE_CSV to point this test at a different, real session file
instead (e.g. a longer field recording kept outside the repo); the test
skips cleanly if that path doesn't exist.
"""

import os
from pathlib import Path

import pytest

from sfm_analysis.report.demo import DEMO_SESSION_PATH

REAL_FILE = Path(os.environ.get("SFM_SMOKE_CSV", str(DEMO_SESSION_PATH)))

pytestmark = [
    pytest.mark.realdata,
    pytest.mark.skipif(
        not REAL_FILE.exists(),
        reason=f"real session CSV not present (set SFM_SMOKE_CSV); looked for {REAL_FILE}",
    ),
]


def test_real_session_loads_and_splits_correctly():
    from sfm_analysis.logs import heartbeat_path_for
    from sfm_analysis.report.loader import load_rows, load_heartbeats
    from sfm_analysis.report.session import split_runs

    rows, schema, warnings = load_rows(REAL_FILE)
    assert len(rows) == 521
    assert warnings == []

    hb = load_heartbeats(heartbeat_path_for(REAL_FILE))
    runs = split_runs(rows, hb, REAL_FILE)
    assert len(runs) == 2
    assert all(r.experiment == "two_armed_bandit" for r in runs)

    run2 = next(r for r in runs if r.run_id == 2)
    total_taken = len(run2.by_event("Pellet Taken"))
    total_loaded = len(run2.by_event("Loaded"))
    assert total_taken == 18
    assert total_loaded == 18


def test_real_session_dome_dedup_matches_known_split():
    from sfm_analysis.logs import heartbeat_path_for
    from sfm_analysis.report.loader import load_rows, load_heartbeats
    from sfm_analysis.report.session import split_runs

    rows, _, _ = load_rows(REAL_FILE)
    hb = load_heartbeats(heartbeat_path_for(REAL_FILE))
    runs = split_runs(rows, hb, REAL_FILE)
    run2 = next(r for r in runs if r.run_id == 2)

    milestone = sum(1 for row in run2.can_events()
                    if row.event_name == "Dome Opened" and row.can_event and row.can_event.name == "DomeOpened")
    edge = sum(1 for row in run2.can_events()
              if row.event_name == "Dome Opened" and row.can_event and row.can_event.name == "InputChanged")
    assert milestone == 34
    assert edge == 4


def test_real_session_fault_interval_and_first_trial_latency():
    from sfm_analysis.logs import heartbeat_path_for
    from sfm_analysis.report.loader import load_rows, load_heartbeats
    from sfm_analysis.report.session import split_runs
    from sfm_analysis.report.metrics import compute_run_metrics

    rows, _, _ = load_rows(REAL_FILE)
    hb = load_heartbeats(heartbeat_path_for(REAL_FILE))
    runs = split_runs(rows, hb, REAL_FILE)
    run2 = next(r for r in runs if r.run_id == 2)

    m = compute_run_metrics(run2)
    assert len(m.faults) == 1
    fault = m.faults[0]
    assert abs(fault.t0 - 189.204) < 0.5
    assert abs(fault.t1 - 200.404) < 0.5
    assert fault.censored is False

    trials = run2.exp("trial")
    assert trials
    assert abs(trials[0].t - 20.017) < 0.5


def test_real_session_post_session_flagging():
    """session_end fires well before the run's last row (a trailing
    heartbeat), so any row after it must be flagged post_session."""
    from sfm_analysis.logs import heartbeat_path_for
    from sfm_analysis.report.loader import load_rows, load_heartbeats
    from sfm_analysis.report.session import split_runs

    rows, _, _ = load_rows(REAL_FILE)
    hb = load_heartbeats(heartbeat_path_for(REAL_FILE))
    runs = split_runs(rows, hb, REAL_FILE)
    run2 = next(r for r in runs if r.run_id == 2)

    assert run2.has_session_end
    post = [r for r in run2.rows if r.post_session]
    assert post
    assert all(r.t >= 668.4 for r in post)


def test_real_session_generates_a_complete_report(tmp_path):
    from sfm_analysis.report import build_session_report

    out = build_session_report(REAL_FILE, out_path=tmp_path / "real_report.html")
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert len(content) > 10_000
    assert "Bandit_test_01" in content
    # The default design embeds the interactive timeline.explorer section
    # by default (see sections/timeline.py) -- one inline <script>, never
    # a <script src=>. build_session_report(..., include_explorer=False)
    # is the guaranteed-script-free path; see test_report_render.py for
    # that invariant and test_report_explorer.py for the explorer's own
    # self-containment tests.
    assert content.lower().count("<script") == 1
    assert "<script src" not in content.lower()

    import xml.etree.ElementTree as ET
    import re
    svgs = re.findall(r"<svg.*?</svg>", content, re.DOTALL)
    assert len(svgs) > 5
    for s in svgs:
        ET.fromstring(s)
