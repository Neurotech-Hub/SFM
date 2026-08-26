#!/usr/bin/env python3
"""Compare which CAN events count as actogram ticks.

The printed report defaults to presence onsets (``MousePresence Detected``).
The same helper the report uses — ``activity_by_day`` — accepts any GUI-log
event name. After ``pip install sfm-analysis`` this runs with no source
tree and no Pi::

    python -m sfm_analysis.examples.actogram_by_event
    python -m sfm_analysis.examples.actogram_by_event /path/to/MySession.csv

From a git checkout the same script is also
``examples/analysis/actogram_by_event.py``.
"""

from __future__ import annotations

import sys

from sfm_analysis.analysis import load_session
from sfm_analysis.report.demo import DEMO_SESSION_PATH
from sfm_analysis.report.metrics import activity_by_day

# Same strings as event_name on frame_type == "EVENT" rows in the GUI log.
PROXIES = (
    ("MousePresence Detected",),
    ("Pellet Taken",),
    ("Dome Opened", "Pellet Taken"),
)


def _summarize(run, names) -> None:
    days = activity_by_day(run, event_names=names)
    n_ticks = sum(len(d.times) for d in days)
    label = ", ".join(names)
    print(f"  {label}: {n_ticks} tick(s) across {len(days)} day(s)")
    for day in days:
        print(f"    {day.date.isoformat()}  n={len(day.times)}")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    target = args[0] if args else str(DEMO_SESSION_PATH)
    s = load_session(target)
    run = s.run()
    print(f"{run.run_label}: actogram event mapping")
    for names in PROXIES:
        _summarize(run, names)
    print(
        "To plot takes on the HTML actogram instead of presence "
        "(no file copy; ships with the package):\n"
        "  sfm-report MySession --design actogram_takes --open\n"
        "To plot a different event, copy that design next to your logs, "
        "edit event_names, and pass the path."
    )


if __name__ == "__main__":
    main()
