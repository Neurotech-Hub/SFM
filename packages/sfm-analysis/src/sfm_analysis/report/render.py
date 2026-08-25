"""render.py — assembles a complete, self-contained report.html.

Pulls together style.py (CSS + hatch defs), the per-run RunMetrics
(metrics.py), and the section list a ReportDef declares (schema.py) into
one HTML string. No file I/O happens here — see report/__init__.py for
the CLI-facing functions that read CSVs and write the result to disk.

Self-containment is the hard invariant this module must uphold: no
reference to a remote origin (http(s)://, //, javascript:), no inline
on*= event handler, nothing that requires network access to render or
print -- a *relative* link to a sibling explorer HTML file
(explorer_href) is one exception, since a same-directory relative link
needs no network either. A section may also contribute inline CSS/JS
via SectionResult.extra_css/extra_js (see schema.SectionResult) -- no
section uses this yet, so a rendered report still carries zero inline
<script> elements today; once one does, this stays capped at exactly
one <script>, never a <script src=>. test_report_render.py asserts all
of this on every build.
"""

from __future__ import annotations

import html as _html
from datetime import datetime, timezone
from typing import List, Optional

from . import charts, style
from .metrics import RunMetrics, compute_run_metrics
from .schema import ReportDef, SectionContext, run_section
from .session import RunData


def _kpi_band(runs: List[RunData], metrics: List[RunMetrics]) -> str:
    total_presented = sum(sum(a.presented_total for a in m.pellets.values()) for m in metrics)
    total_taken = sum(sum(a.taken_total for a in m.pellets.values()) for m in metrics)
    total_duration = sum(r.duration_s for r in runs)
    total_stalled = sum(len(m.health.get("script_stalled", [])) for m in metrics)
    total_faults = sum(len(m.faults) for m in metrics)

    take_rate = f"{(total_taken / total_presented * 100):.0f}%" if total_presented else "—"

    first_trial_t: Optional[float] = None
    for run in runs:
        trials = run.exp("trial")
        if trials:
            first_trial_t = trials[0].t
            break

    tiles = [
        charts.kpi("Runs", str(len(runs))),
        charts.kpi("Duration", _fmt_duration(total_duration)),
        charts.kpi("Pellets presented → taken", f"{total_presented} → {total_taken}", sub=f"take rate {take_rate}"),
    ]
    if first_trial_t is not None:
        tiles.append(charts.kpi("Time to first trial", _fmt_duration(first_trial_t)))
    if total_stalled:
        tiles.append(charts.kpi("script_stalled events", str(total_stalled), severity="warning"))
    if total_faults:
        tiles.append(charts.kpi("Fault intervals", str(total_faults), severity="serious"))

    return f'<div class="kpi-band">{"".join(tiles)}</div>'


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def render_report_html(
    runs: List[RunData],
    design: ReportDef,
    *,
    combined: bool = False,
    align: str = "relative",
    title: Optional[str] = None,
    explorer_href: Optional[str] = None,
) -> str:
    """
    Render a complete HTML document for `runs` using `design`.

    `runs` is one session's runs for a per-session report, or the merged
    runs of several sessions for a combined report (`combined=True`,
    which selects `design.combined_sections` instead of `design.sections`).

    `explorer_href`, if given, is a relative link to a sibling
    interactive explorer HTML file (see report/explorer.py) -- never an
    absolute or remote URL, so this document stays self-contained and
    works offline regardless of where the pair of files end up. This is
    the one link this document ever emits; see test_report_render.py for
    the scheme-based self-containment check that still allows it.

    (This parameter is slated for removal once the explorer is embedded
    directly as a report section instead of a sibling file -- see the
    Phase 5 plan. Left wired for now so this commit changes nothing
    observable.)
    """
    metrics = [compute_run_metrics(r) for r in runs]
    specs = design.combined_sections if combined else design.sections
    active_refs = frozenset(spec.ref for spec in specs if spec.enabled)

    doc_title = title or (
        f"Combined Report — {len(runs)} runs" if combined else
        (runs[0].session if runs else "Session Report")
    )

    section_html_parts = []
    css_chunks: List[str] = []
    js_chunks: List[str] = []
    seen_css, seen_js = set(), set()
    for spec in specs:
        if not spec.enabled:
            continue
        ctx = SectionContext(runs=runs, metrics=metrics, combined=combined, align=align,
                              opts=spec.options, design=design, active_refs=active_refs)
        result = run_section(spec, ctx)
        if result is None or result.empty:
            continue
        break_style = ' style="page-break-before: always"' if result.page_break_before else ""
        section_html_parts.append(
            f'<div class="section"{break_style}><h2>{_html.escape(result.title)}</h2>{result.html}</div>'
        )
        # Deduplicated (preserving first-seen order): several instances of the
        # same section type would otherwise repeat identical CSS/JS verbatim.
        if result.extra_css and result.extra_css not in seen_css:
            seen_css.add(result.extra_css)
            css_chunks.append(result.extra_css)
        if result.extra_js and result.extra_js not in seen_js:
            seen_js.add(result.extra_js)
            js_chunks.append(result.extra_js)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    session_list = ", ".join(sorted({r.session for r in runs})) or "—"
    explorer_link = (
        f' · <a href="{_html.escape(explorer_href)}">interactive timeline</a>'
        if explorer_href else ""
    )

    script_block = f"<script>{''.join(js_chunks)}</script>" if js_chunks else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_html.escape(doc_title)}</title>
<style>{style.PAGE_CSS}{"".join(css_chunks)}</style>
</head>
<body>
{charts.defs()}
<div class="report">
  <h1>{_html.escape(doc_title)}</h1>
  <p class="note">Sessions: {_html.escape(session_list)} · Design: {_html.escape(design.label)}{explorer_link}</p>
  {_kpi_band(runs, metrics)}
  {"".join(section_html_parts)}
  <footer>Generated {generated_at} by run_report.py &mdash; VFM behavior report tool.
  Print with Ctrl+P / Cmd+P.</footer>
</div>
{script_block}
</body>
</html>
"""
