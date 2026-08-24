"""report — turns VFM session CSV logs into printable HTML behavior reports.

Public entry points (used by both run_report.py and, later, a GUI hook):

    build_session_report(csv_path, ...)   -> Path
    build_combined_report(csv_paths, ...) -> Path
    render_report_html(...)               -> str  (re-exported from render.py)

The package is stdlib-only (no jinja2/pandas/matplotlib) and produces a
single self-contained HTML file with inline SVG charts, printable via
Ctrl+P. See sfm_analysis/report/schema.py for how report "designs"
(JSON) compose "sections" (Python) — the same split the VFM base
station's experiment system uses for templates/parameters.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from ..logs import heartbeat_path_for
from .loader import load_heartbeats, load_rows
from .render import render_report_html
from .schema import load_report_def, resolve_design
from .session import RunData, split_runs

__all__ = [
    "build_session_report",
    "build_combined_report",
    "render_report_html",
]


def _load_runs(csv_path: Path, run_id: Optional[int] = None) -> List[RunData]:
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
        # An explicit --design name is looked up directly by filename stem.
        from .schema import DEFAULT_REPORTS_DIR
        path = DEFAULT_REPORTS_DIR / f"{design}.json"
        if path.exists():
            return load_report_def(path)
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
) -> Path:
    """Render one session's CSV to a standalone HTML report and write it to disk."""
    runs = _load_runs(csv_path, run_id=run_id)
    report_def = _resolve_design_for(runs, design)
    html = render_report_html(runs, report_def, combined=False, align=align, title=title)

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
        all_runs.extend(_load_runs(p))

    report_def = _resolve_design_for(all_runs, design)
    html = render_report_html(all_runs, report_def, combined=True, align=align, title=title)

    if out_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = csv_paths[0].parent / "reports" / f"combined_{stamp}.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
