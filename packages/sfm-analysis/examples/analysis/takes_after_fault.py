#!/usr/bin/env python3
"""takes_after_fault.py — peri-event windows: how many pellets were taken
in the N seconds after each fault started?

The demo session has exactly one fault interval (a PelletLost fault on
node 1, ~11 seconds of downtime), so this recipe's output is a fixed,
known number — a good way to sanity-check that your own analysis script
using the same pattern against real data is doing what you think it is.

    python examples/analysis/takes_after_fault.py
"""

from __future__ import annotations

from sfm_analysis.analysis import intervals as iv
from sfm_analysis.analysis import load_session
from sfm_analysis.report.demo import DEMO_SESSION_PATH

WINDOW_S = 30.0  # look this many seconds after each fault starts


def main() -> None:
    s = load_session(str(DEMO_SESSION_PATH))
    run = s.run()
    m = s.metrics_for()

    if not m.faults:
        print(f"{run.run_label}: no fault intervals in this run")
        return

    fault_starts = [f.t0 for f in m.faults]
    windows = iv.around(fault_starts, (0.0, WINDOW_S))
    take_times = [row.t for row in run.by_event("Pellet Taken")]

    n = iv.count_in(take_times, windows)
    print(f"{run.run_label}: {len(m.faults)} fault interval(s)")
    for f in m.faults:
        dur = f"{f.dur:.1f}s" if f.dur is not None else "still open at run end"
        print(f"    node {f.node}: {f.code.name} at t={f.t0:.1f}s, duration {dur}")
    print(f"{n} pellet take(s) within {WINDOW_S:.0f}s after a fault started")


if __name__ == "__main__":
    main()
