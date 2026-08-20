"""sections/compare.py — cross-session sections for combined reports.

Covers the four comparison axes a cohort report needs: side-by-side
(``cohort_table``, the always-available fallback), across days
(``learning_curve``), across subjects (``subject_spread``), and across
nodes/spatial (``node_preference``), plus ``quality_matrix`` (which run
had a bad day) and ``cumulative_overlay`` (a shared-timeline view using
whatever ``ctx.align`` mode was requested — see align.py).

These sections only run when ``ctx.combined`` is True; each ``run``/``m``
pair is one row here, not one full sub-report the way generic.* sections
treat multiple runs of a single session.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .. import charts
from ..align import compute_offsets
from ..charts import Frame, Series, escape_text
from ..naming import parse_session_name
from ..schema import SectionContext, SectionResult
from ..stats import median, safe_ratio, wilson_ci


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _fmt_pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def cohort_table_section(ctx: SectionContext) -> Optional[SectionResult]:
    if not ctx.runs:
        return SectionResult(section_id="compare.cohort_table", title="Cohort Summary", html="", empty=True)

    rows = []
    for run, m in zip(ctx.runs, ctx.metrics):
        ident = parse_session_name(run.session)
        presented = sum(a.presented_total for a in m.pellets.values())
        taken = sum(a.taken_total for a in m.pellets.values())
        cumulative = [tp.taken for tp in m.throughput]
        latencies = [c.retrieval_latency for c in m.cycles if c.retrieval_latency is not None]
        bouts, _ = m.presence
        occ = sum((b.dur or 0) for b in bouts)
        rows.append(f"""
        <tr>
          <td>{escape_text(run.run_label)}</td>
          <td>{escape_text(ident.subject or '—')}</td>
          <td>{escape_text(ident.day) if ident.day is not None else '—'}</td>
          <td>{escape_text(run.experiment)}</td>
          <td>{_fmt_duration(run.duration_s)}</td>
          <td>{presented} &rarr; {taken}</td>
          <td>{_fmt_pct(safe_ratio(taken, presented))}</td>
          <td>{_fmt_duration(median(latencies))}</td>
          <td>{_fmt_pct(safe_ratio(occ, run.duration_s))}</td>
          <td>{len(m.faults)}</td>
          <td>{len(m.health.get('script_stalled', []))}</td>
          <td>{charts.sparkline(cumulative)}</td>
        </tr>
        """)

    html = f"""
    <table>
      <tr><th>Run</th><th>Subject</th><th>Day</th><th>Experiment</th><th>Duration</th>
          <th>Presented &rarr; Taken</th><th>Take rate</th><th>Median retrieval</th>
          <th>Occupancy</th><th>Faults</th><th>Stalls</th><th>Cumulative</th></tr>
      {"".join(rows)}
    </table>
    """
    return SectionResult(section_id="compare.cohort_table", title="Cohort Summary", html=html)


def learning_curve_section(ctx: SectionContext) -> Optional[SectionResult]:
    metric_name = ctx.opts.get("metric", "take_rate")
    by_subject: Dict[str, List] = {}
    for run, m in zip(ctx.runs, ctx.metrics):
        ident = parse_session_name(run.session)
        key = ident.subject or run.session
        by_subject.setdefault(key, []).append((run, m, ident))

    parts = []
    any_data = False
    frame = Frame()
    lines_html = []
    all_x: List[float] = []

    for si, (subject, entries) in enumerate(sorted(by_subject.items())):
        entries.sort(key=lambda e: (e[2].day if e[2].day is not None else 0, e[0].t0_ms))
        pts = []
        for xi, (run, m, ident) in enumerate(entries):
            x_val = ident.day if ident.day is not None else xi
            y_val = _metric_value(metric_name, run, m)
            if y_val is not None:
                pts.append((x_val, y_val))
                all_x.append(x_val)
        if len(pts) >= 2:
            any_data = True
            lines_html.append((subject, si, pts))

    if not any_data:
        return SectionResult(section_id="compare.learning_curve", title="Learning Across Sessions", html="", empty=True)

    x0, x1 = min(all_x), max(all_x)
    x = charts.linear(x0, x1, frame.px0, frame.px1)
    y = charts.linear(0, 1, frame.py1, frame.py0)
    axes = charts.axes(frame, x, y, x_ticks=[(v, str(int(v))) for v in sorted(set(all_x))],
                        y_ticks=[0, 0.25, 0.5, 0.75, 1.0], x_label="day", y_label=metric_name.replace("_", " "))
    body = [axes]
    for subject, key, pts in lines_html:
        body.append(charts.line(frame, x, y, pts, key=key, markers=True))
    chart = charts.svg(frame, "".join(body), title="Learning curve across sessions")
    legend = charts.legend([(subject, key) for subject, key, _ in lines_html])

    html = f'<figure>{chart}{legend}<figcaption>Metric: {escape_text(metric_name)}. ' \
          f'X-axis is parsed session day where available, else session order.</figcaption></figure>'
    return SectionResult(section_id="compare.learning_curve", title="Learning Across Sessions", html=html)


def _metric_value(name: str, run, m) -> Optional[float]:
    if name == "take_rate":
        presented = sum(a.presented_total for a in m.pellets.values())
        taken = sum(a.taken_total for a in m.pellets.values())
        return safe_ratio(taken, presented)
    if name == "median_retrieval_latency":
        vals = [c.retrieval_latency for c in m.cycles if c.retrieval_latency is not None]
        return median(vals)
    if name == "p_choose_rich":
        try:
            from ..analyses.bandit import build_trials, block_curve
        except ImportError:
            return None
        trials = build_trials(run)
        points = block_curve(trials)
        if not points:
            return None
        return sum(p.p for p in points) / len(points)
    return None


def subject_spread_section(ctx: SectionContext) -> Optional[SectionResult]:
    metric_name = ctx.opts.get("metric", "take_rate")
    by_subject: Dict[str, List[float]] = {}
    for run, m in zip(ctx.runs, ctx.metrics):
        ident = parse_session_name(run.session)
        key = ident.subject or run.session
        val = _metric_value(metric_name, run, m)
        if val is not None:
            by_subject.setdefault(key, []).append(val)

    if len(by_subject) < 2:
        return SectionResult(section_id="compare.subject_spread", title="Subject Spread", html="", empty=True)

    frame = Frame()
    series = [Series(subj, vals, key=i) for i, (subj, vals) in enumerate(sorted(by_subject.items()))]
    chart = charts.svg(frame, charts.dot_strip(frame, series), title="Per-subject spread")
    html = f'<figure>{chart}<figcaption>Metric: {escape_text(metric_name)}, one row per subject, ' \
          f'median marked.</figcaption></figure>'
    return SectionResult(section_id="compare.subject_spread", title="Subject Spread", html=html)


def node_preference_section(ctx: SectionContext) -> Optional[SectionResult]:
    occupancy: Dict[str, Dict[int, float]] = {}
    all_nodes = set()
    for run, m in zip(ctx.runs, ctx.metrics):
        bouts, _ = m.presence
        by_node: Dict[int, float] = {}
        for b in bouts:
            by_node[b.node] = by_node.get(b.node, 0.0) + (b.dur or 0.0)
        total = sum(by_node.values())
        if total <= 0:
            continue
        occupancy[run.run_label] = {n: v / total for n, v in by_node.items()}
        all_nodes.update(by_node.keys())

    if len(occupancy) < 2 or not all_nodes:
        return SectionResult(section_id="compare.node_preference", title="Node Preference", html="", empty=True)

    nodes = sorted(all_nodes)
    runs_list = sorted(occupancy.keys())
    matrix = [[occupancy[r].get(n) for n in nodes] for r in runs_list]

    frame = Frame(h=max(140, 30 + len(runs_list) * 22))
    chart = charts.svg(frame, charts.heatmap(frame, matrix, runs_list, [f"node {n}" for n in nodes]),
                        title="Occupancy share by node")
    html = f'<figure>{chart}<figcaption>Fraction of presence time spent at each node, per run. ' \
          f'A persistent bias toward one node across many runs suggests a spatial preference rather ' \
          f'than a trial-by-trial choice effect.</figcaption></figure>'
    return SectionResult(section_id="compare.node_preference", title="Node Preference", html=html)


def quality_matrix_section(ctx: SectionContext) -> Optional[SectionResult]:
    if len(ctx.runs) < 2:
        return SectionResult(section_id="compare.quality_matrix", title="Data Quality Matrix", html="", empty=True)

    metrics_cols = ["script_stalled", "faults", "orphan closes", "post-session rows"]
    runs_list = []
    matrix = []
    max_val = 1
    for run, m in zip(ctx.runs, ctx.metrics):
        _, presence_issues = m.presence
        row_vals = [
            len(m.health.get("script_stalled", [])),
            len(m.faults),
            presence_issues.orphan_closes,
            sum(1 for r in run.rows if r.post_session),
        ]
        max_val = max(max_val, max(row_vals))
        runs_list.append(run.run_label)
        matrix.append(row_vals)

    frame = Frame(h=max(140, 30 + len(runs_list) * 22))
    chart = charts.svg(frame, charts.heatmap(frame, matrix, runs_list, metrics_cols,
                                              vmin=0, vmax=max_val, fmt="{:.0f}"),
                        title="Data quality issue counts by run")
    html = f'<figure>{chart}<figcaption>Raw counts (not normalized) — use this to spot which run to ' \
          f'exclude or re-check before pooling results.</figcaption></figure>'
    return SectionResult(section_id="compare.quality_matrix", title="Data Quality Matrix", html=html)


def cumulative_overlay_section(ctx: SectionContext) -> Optional[SectionResult]:
    if len(ctx.runs) < 2:
        return SectionResult(section_id="compare.cumulative_overlay", title="Cumulative Overlay", html="", empty=True)

    offsets = compute_offsets(ctx.runs, ctx.align)
    series_pts: List[tuple] = []
    all_t: List[float] = []
    for i, (run, m) in enumerate(zip(ctx.runs, ctx.metrics)):
        off = offsets.get(id(run), 0.0)
        pts = [(tp.t + off, tp.taken) for tp in m.throughput]
        if pts:
            series_pts.append((run.run_label, i, pts))
            all_t.extend(t for t, _ in pts)

    if not series_pts or not all_t:
        return SectionResult(section_id="compare.cumulative_overlay", title="Cumulative Overlay", html="", empty=True)

    t0, t1 = min(all_t), max(0.0, max(all_t))
    max_y = max((max((v for _, v in pts), default=0) for _, _, pts in series_pts), default=1) or 1

    frame = Frame()
    x = charts.linear(t0, t1, frame.px0, frame.px1)
    y = charts.linear(0, max_y, frame.py1, frame.py0)
    x_ticks = [(v, f"{v:.0f}s") for v in charts.nice_ticks(t0, t1, 8)]
    axes = charts.axes(frame, x, y, x_ticks=x_ticks, y_ticks=charts.nice_ticks(0, max_y, 5),
                        x_label=f"time (aligned: {ctx.align})", y_label="cumulative pellets taken")
    body = [axes]
    for label, key, pts in series_pts:
        body.append(charts.line(frame, x, y, pts, key=key, step=True))
    chart = charts.svg(frame, "".join(body), title="Cumulative pellets, aligned overlay")
    legend = charts.legend([(label, key) for label, key, _ in series_pts])

    html = f'<figure>{chart}{legend}<figcaption>Alignment: <b>{escape_text(ctx.align)}</b>. ' \
          f'"relative" starts every run at its own session start; "event:&lt;name&gt;" starts every ' \
          f'run at the first row named &lt;name&gt;, which is what makes a run with a long pre-trial ' \
          f'stall comparable to a clean one.</figcaption></figure>'
    return SectionResult(section_id="compare.cumulative_overlay", title="Cumulative Overlay", html=html)


SECTIONS = {
    "cohort_table": cohort_table_section,
    "learning_curve": learning_curve_section,
    "subject_spread": subject_spread_section,
    "node_preference": node_preference_section,
    "quality_matrix": quality_matrix_section,
    "cumulative_overlay": cumulative_overlay_section,
}
