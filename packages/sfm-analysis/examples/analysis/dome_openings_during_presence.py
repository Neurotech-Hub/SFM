#!/usr/bin/env python3
"""dome_openings_during_presence.py — "was the dome open while the mouse
was present?", the canonical interval-algebra question this package was
built to answer without double-counting.

A naive `run.by_event("Dome Opened")` count is wrong: that event_name is
shared by two different wire events (a milestone and a renamed edge — see
`sfm_analysis.report.loader`'s LogRow.can_event docstring), so it
double-counts every physical dome opening. `metrics.dome_bouts` already
does that dedup; this recipe starts from its output.

Run it directly — it needs no rig, no log directory, and no real data of
your own; it renders against the bundled demo session.

    python examples/analysis/dome_openings_during_presence.py
"""

from __future__ import annotations

from sfm_analysis.analysis import intervals as iv
from sfm_analysis.analysis import load_session
from sfm_analysis.report.demo import DEMO_SESSION_PATH


def main() -> None:
    s = load_session(str(DEMO_SESSION_PATH))
    run = s.run()
    m = s.metrics_for()

    presence_bouts, _ = m.presence
    dome_bouts, _ = m.dome

    # Bouts still open when the run ended (censored) have no t1 yet;
    # as_intervals refuses to guess, so we resolve them to run end here.
    presence_iv = iv.as_intervals(presence_bouts, run_end=run.duration_s)
    dome_iv = iv.as_intervals(dome_bouts, run_end=run.duration_s)

    overlap = iv.overlap(dome_iv, presence_iv)

    print(f"{run.run_label}: {len(dome_iv)} dome-open bouts, {len(presence_iv)} presence bouts")
    print(f"{len(overlap)} overlap window(s) where the dome was open while the mouse was present")
    for t0, t1 in overlap[:5]:
        print(f"    {t0:8.2f}s - {t1:8.2f}s  ({t1 - t0:.2f}s)")
    if len(overlap) > 5:
        print(f"    ... and {len(overlap) - 5} more")


if __name__ == "__main__":
    main()
