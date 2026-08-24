"""report — turns VFM session CSV logs into printable HTML behavior reports.

Public entry points (used by run_report.py, sfm_analysis.analysis, and,
later, a GUI hook):

    load_runs(csv_path, ...)              -> List[RunData]
    build_session_report(csv_path, ...)   -> Path
    build_session_explorer(csv_path, ...) -> Path
    build_combined_report(csv_paths, ...) -> Path
    render_report_html(...)               -> str  (re-exported from render.py)

The package is stdlib-only (no jinja2/pandas/matplotlib) and produces a
single self-contained HTML file with inline SVG charts, printable via
Ctrl+P. See sfm_analysis/report/schema.py for how report "designs"
(JSON) compose "sections" (Python) — the same split the VFM base
station's experiment system uses for templates/parameters.

build_session_explorer is the one exception to "no JavaScript": it
writes a sibling interactive HTML file (pan/zoom/brush-to-zoom over a
canvas) for exploring a single run's timeline — see explorer.py and
explorer_render.py. It is never produced automatically by
build_session_report; a caller that wants both asks for both and, if it
wants them cross-linked, passes the explorer's path as
build_session_report's explorer_href.
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
    "load_runs",
    "build_session_report",
    "build_session_explorer",
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
    explorer_href: Optional[str] = None,
) -> Path:
    """Render one session's CSV to a standalone HTML report and write it to disk.

    `explorer_href`, if given, is a relative link embedded in the report
    to a sibling interactive explorer file -- this function does not
    build that file itself; see build_session_explorer.
    """
    runs = load_runs(csv_path, run_id=run_id)
    report_def = _resolve_design_for(runs, design)
    html = render_report_html(runs, report_def, combined=False, align=align, title=title,
                               explorer_href=explorer_href)

    if out_path is None:
        session = runs[0].session
        suffix = f"_run{runs[0].run_id}" if len(runs) == 1 else ""
        out_path = Path(csv_path).parent / "reports" / f"{session}{suffix}_report.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def build_session_explorer(
    csv_path: Path,
    out_path: Optional[Path] = None,
    *,
    run_id: Optional[int] = None,
    report_href: str = "",
) -> Path:
    """
    Render one run's interactive timeline explorer and write it to disk —
    a sibling artifact to build_session_report's printed output, not a
    replacement for it (see this module's docstring).

    An explorer represents exactly one run's timeline. If csv_path holds
    more than one run and run_id isn't given, the run with the most rows
    is used, so a session's aborted restart doesn't win over the real
    session that followed it -- the same heuristic
    sfm_analysis.analysis.Session.run() uses (duplicated here rather than
    imported from there, since `analysis` depends on this package, not
    the other way around).

    `report_href`, if given, is a relative link back to the sibling
    printed report, shown in the explorer's own header.
    """
    from .explorer import build_explorer_payload
    from .explorer_render import render_explorer_html
    from .metrics import compute_run_metrics

    runs = load_runs(csv_path, run_id=run_id)
    run = runs[0] if len(runs) == 1 else max(runs, key=lambda r: len(r.rows))
    m = compute_run_metrics(run)
    payload = build_explorer_payload(run, m)
    html = render_explorer_html(payload, report_href=report_href)

    if out_path is None:
        out_path = Path(csv_path).parent / "reports" / f"{run.session}_run{run.run_id}_explorer.html"
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
