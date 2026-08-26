#!/usr/bin/env python3
"""exp_events_by_name.py — inventory a session's experiment-engine rows.

Custom templates log their own EXP events (bandit_trial, alternation_advance,
probability_pick, …). Before writing an analysis against those names, list
what the file actually contains — this is the escape hatch in
ANALYSIS_GUIDE §3.

The demo session is two_armed_bandit, so the printed names are that
template's vocabulary plus the engine's session-lifecycle rows.

    python examples/analysis/exp_events_by_name.py
"""

from __future__ import annotations

from collections import Counter

from sfm_analysis.analysis import load_session
from sfm_analysis.report.demo import DEMO_SESSION_PATH


def main() -> None:
    s = load_session(str(DEMO_SESSION_PATH))
    run = s.run()
    rows = s.events_table(run_id=run.run_id, source="EXP")
    counts = Counter(row["event_name"] for row in rows)

    print(f"{run.run_label}: {len(rows)} EXP row(s), {len(counts)} distinct name(s)")
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:4d}  {name}")

    ends = [row for row in rows if row["event_name"] == "bandit_trial_end"]
    valid = sum(1 for row in ends if (row["fields"] or {}).get("valid") == 1)
    print(f"bandit_trial_end: {len(ends)} (valid={valid})")


if __name__ == "__main__":
    main()
