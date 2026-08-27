"""sfm_analysis — cross-platform analysis and reporting for SFM feeder logs.

Reads the session CSVs written by the SFM base station (``packages/dev_gui`` in
the parent repo, https://github.com/Neurotech-Hub/SFM) and turns them into
printable HTML behavior reports, with no dependency on the Raspberry Pi
base-station GUI or any hardware library. Install with ``pip install
sfm-analysis`` on Windows, macOS, or Linux and run ``sfm-report`` — see
README.md for the full CLI and the Python analysis API.

This package is stdlib-only by design: no jinja2, no pandas, no matplotlib.
Reports are self-contained HTML with inline SVG, printable via Ctrl+P.
"""

from __future__ import annotations

__version__ = "0.1.2"

__all__ = ["__version__"]
