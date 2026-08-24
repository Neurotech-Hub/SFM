#!/usr/bin/env python3
"""run_report.py — thin wrapper around the sfm-analysis report CLI.

The implementation moved to ``sfm_analysis.cli.report``; ``pip install
sfm-analysis`` also installs it as the ``sfm-report`` console script,
which is the preferred entry point everywhere except this base station.

This wrapper stays for two reasons: the documented base-station workflow
is ``python run_report.py`` (see ../../README.md and README.md), and the
base station must keep using its own writable-probing log-directory
resolver (base_station/storage.py) rather than the SDK's read-only one,
so that default log-dir resolution on the base station is unchanged.

Examples::

    python run_report.py --list
    python run_report.py EXP-Test-02 --open
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from base_station.storage import default_log_dir  # noqa: E402
from sfm_analysis.cli.report import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(default_log_dir=default_log_dir))
