"""report — turns SFM session CSV logs into printable HTML behavior reports.

Public entry points (used by run_report.py, sfm_analysis.analysis, and,
later, a GUI hook):

    load_runs(csv_path, ...)              -> List[RunData]
    build_session_report(csv_path, ...)   -> Path
    build_combined_report(csv_paths, ...) -> Path
    render_report_html(...)               -> str  (re-exported from render.py)

The package is stdlib-only (no jinja2/pandas/matplotlib) and produces a
single self-contained HTML file, printable via Ctrl+P. See
sfm_analysis/report/schema.py for how report "designs" (JSON) compose
"sections" (Python) — the same split the SFM base station's experiment
system uses for templates/parameters.

Every report is exactly one HTML file, never a pair. The interactive
timeline (pan/zoom/brush-to-zoom over a canvas) is one such section —
sections/timeline.py's timeline.explorer, backed by explorer.py/
explorer_render.py — embedded directly rather than written as a sibling
document; pass build_session_report(..., include_explorer=False) for
the leaner, guaranteed-script-free printable-only document.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from ..logs import heartbeat_path_for
from .loader import load_heartbeats, load_rows
from .render import render_report_html
from .schema import load_design, resolve_design
from .session import RunData, split_runs

__all__ = [
    "load_runs",
    "build_session_report",
    "build_combined_report",
    "render_report_html",
]


def load_runs(csv_path: Path, run_id: Optional[int] = None) -> List[RunData]:
    """Load one session CSV into its RunData objects (see session.split_runs).

    Shared by build_session_report/build_combined_report below and by
    sfm_analysis.analysis.load_session -- the one place a CSV path becomes
    typed, run-split data.
    """
    csv_path = Path(csv_path)
    rows, schema, warnings = load_rows(csv_path)
    if not rows:
        raise ValueError(
            f"No usable rows in {csv_path.name} (schema={schema.value}). " + " ".join(warnings)
        )
    hb_path = heartbeat_path_for(csv_path)
    heartbeats = load_heartbeats(hb_path) if hb_path.exists() else []
    runs = split_runs(rows, heartbeats, csv_path)
    if run_id is not None:
        runs = [r for r in runs if r.run_id == run_id]
    if not runs:
        raise ValueError(f"No runs found in {csv_path.name}" + (f" for run_id={run_id}" if run_id else ""))
    return runs


def _resolve_design_for(runs: List[RunData], design: Optional[str]):
    if design:
        return load_design(design)
    experiments = {r.experiment for r in runs}
    experiment = next(iter(experiments)) if len(experiments) == 1 else "unknown"
    return resolve_design(experiment)


def build_session_report(
    csv_path: Path,
    out_path: Optional[Path] = None,
    *,
    run_id: Optional[int] = None,
    design: Optional[str] = None,
    align: str = "relative",
    title: Optional[str] = None,
    include_explorer: bool = True,
) -> Path:
    """Render one session's CSV to a standalone HTML report and write it to disk.

    `include_explorer=False` drops the embedded interactive timeline
    (see render_report_html) even if the design lists it, for a caller
    that wants the guaranteed-script-free printable-only document.
    """
    runs = load_runs(csv_path, run_id=run_id)
    report_def = _resolve_design_for(runs, design)
    html = render_report_html(runs, report_def, combined=False, align=align, title=title,
                               include_explorer=include_explorer)

    if out_path is None:
        session = runs[0].session
        suffix = f"_run{runs[0].run_id}" if len(runs) == 1 else ""
        out_path = Path(csv_path).parent / "reports" / f"{session}{suffix}_report.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def build_combined_report(
    csv_paths: Iterable[Path],
    out_path: Optional[Path] = None,
    *,
    design: Optional[str] = None,
    align: str = "relative",
    title: Optional[str] = None,
) -> Path:
    """Render a comparative report over several sessions' CSVs and write it to disk."""
    csv_paths = [Path(p) for p in csv_paths]
    if not csv_paths:
        raise ValueError("build_combined_report requires at least one CSV path")

    all_runs: List[RunData] = []
    for p in csv_paths:
        all_runs.extend(load_runs(p))

    report_def = _resolve_design_for(all_runs, design)
    html = render_report_html(all_runs, report_def, combined=True, align=align, title=title)

    if out_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = csv_paths[0].parent / "reports" / f"combined_{stamp}.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
