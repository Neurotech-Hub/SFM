#!/usr/bin/env python3
"""actogram_by_event.py — map which CAN events count as actogram ticks.

After ``pip install sfm-analysis`` (no source tree)::

    python -m sfm_analysis.examples.actogram_by_event
    python -m sfm_analysis.examples.actogram_by_event /path/to/MySession.csv

From a git checkout this file is the same recipe::

    python examples/analysis/actogram_by_event.py
"""

from sfm_analysis.examples.actogram_by_event import main

if __name__ == "__main__":
    main()
