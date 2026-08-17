"""sections/generic.py — sections applicable to any session regardless of
which experiment template produced it.

Every function here has the signature ``SectionFn`` (schema.py): takes a
SectionContext (one or more runs plus their pre-computed RunMetrics) and
returns a SectionResult, or None if there's nothing to say (e.g. a
funnel section with zero cycles). Registered in SECTIONS at the bottom,
mirroring protocol.py's CAN_EVENT_DISPLAY_NAME table style.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import charts
from ..charts import Frame, Series, escape_text
from ..naming import parse_session_name
from ..schema import SectionContext, SectionResult
from ..stats import median, safe_ratio


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _fmt_pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _fmt_num(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def provenance_section(ctx: SectionContext) -> Optional[SectionResult]:
    rows_html = []
    for run in ctx.runs:
        ident = parse_session_name(run.session)
        subject_line = (
            f"subject {escape_text(ident.subject)}" if ident.subject else
            "no subject parsed from session name"
        )
        params_rows = "".join(
            f"<tr><td>{escape_text(k)}</td><td>{escape_text(v)}</td></tr>"
            for k, v in sorted(run.params.items())
        ) or "<tr><td colspan='2' class='note'>none logged</td></tr>"

        rows_html.append(f"""
        <div class="section">
          <h3>{escape_text(run.run_label)}</h3>
          <table>
            <tr><td>Experiment</td><td>{escape_text(run.experiment)}</td></tr>
            <tr><td>Nodes</td><td>{escape_text(run.nodes) or '—'}</td></tr>
            <tr><td>Seed</td><td>{escape_text(run.seed) if run.seed is not None else '—'}</td></tr>
            <tr><td>Identity</td><td>{subject_line}</td></tr>
            <tr><td>Duration</td><td>{_fmt_duration(run.duration_s)}</td></tr>
            <tr><td>End reason</td><td>{escape_text(run.end_reason) if run.end_reason else
                ('interrupted — no session_end' if not run.has_session_end else 'stopped normally')}</td></tr>
            <tr><td>Source file</td><td>{escape_text(str(run.source_path))}</td></tr>
          </table>
          <h3>Configured parameters</h3>
          <table><tbody>{params_rows}</tbody></table>
        </div>
        """)

    return SectionResult(
        section_id="generic.provenance",
        title="Session Provenance",
        html="".join(rows_html),
        summary={"runs": len(ctx.runs)},
    )


def data_quality_section(ctx: SectionContext) -> Optional[SectionResult]:
    notes: List[str] = []
    banners: List[str] = []

    for run, m in zip(ctx.runs, ctx.metrics):
        prefix = f"{run.run_label}: " if len(ctx.runs) > 1 else ""
        for note in run.notes:
            notes.append(prefix + note)

        _, presence_issues = m.presence
        if presence_issues.orphan_closes:
            notes.append(f"{prefix}{presence_issues.orphan_closes} presence bout(s) closed with no matching open (run began mid-bout).")
        if presence_issues.censored:
            notes.append(f"{prefix}{presence_issues.censored} presence bout(s) still open at run end.")

        _, dome_issues = m.dome
        if dome_issues.orphan_closes:
            notes.append(f"{prefix}{dome_issues.orphan_closes} dome bout(s) closed with no matching open.")

        for node, acct in m.pellets.items():
            loss_p = acct.bus_loss_presented
            loss_t = acct.bus_loss_taken
            if loss_p:
                notes.append(f"{prefix}node {node}: heartbeat counter shows {loss_p} more pellet(s) "
                             f"presented than CAN events recorded (possible bus loss).")
            if loss_t:
                notes.append(f"{prefix}node {node}: heartbeat counter shows {loss_t} more pellet(s) "
                             f"taken than CAN events recorded (possible bus loss).")

        post_session_rows = sum(1 for r in run.rows if r.post_session)
        if post_session_rows:
            notes.append(f"{prefix}{post_session_rows} row(s) logged after session_end (excluded from rate denominators).")

        if m.health.get("script_stalled"):
            n = len(m.health["script_stalled"])
            banners.append(f"{prefix}script_stalled fired {n} time(s) — the experiment script waited "
                           f"on an event that never arrived. Check apparatus health below.")

    banner_html = "".join(f'<div class="banner">{escape_text(b)}</div>' for b in banners)
    notes_html = "".join(f'<p class="note">{escape_text(n)}</p>' for n in notes) or '<p class="note">No data-quality issues detected.</p>'

    return SectionResult(
        section_id="generic.data_quality",
        title="Data Quality",
        html=banner_html + notes_html,
        summary={"note_count": len(notes)},
        notes=notes,
    )


def pellet_accounting_section(ctx: SectionContext) -> Optional[SectionResult]:
    any_data = any(m.pellets for m in ctx.metrics)
    if not any_data:
        return SectionResult(section_id="generic.pellet_accounting", title="Pellet Accounting", html="", empty=True)

    parts = []
    for run, m in zip(ctx.runs, ctx.metrics):
        rows = "".join(
            f"<tr><td>{node}</td><td>{a.presented}</td><td>{a.no_feed_presented}</td>"
            f"<td>{a.taken}</td><td>{a.feed_skipped}</td><td>{_fmt_pct(a.take_rate)}</td></tr>"
            for node, a in sorted(m.pellets.items())
        )
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        parts.append(f"""
        {heading}
        <table>
          <tr><th>Node</th><th>Presented</th><th>No-feed</th><th>Taken</th><th>Skipped</th><th>Take rate</th></tr>
          {rows}
        </table>
        """)

    return SectionResult(
        section_id="generic.pellet_accounting",
        title="Pellet Accounting",
        html="".join(parts),
    )


def retrieval_latency_section(ctx: SectionContext) -> Optional[SectionResult]:
    figs = []
    for run, m in zip(ctx.runs, ctx.metrics):
        by_node: Dict[int, List[float]] = {}
        dome_by_node: Dict[int, List[float]] = {}
        for c in m.cycles:
            if c.retrieval_latency is not None:
                by_node.setdefault(c.node, []).append(c.retrieval_latency)
            if c.dome_latency is not None:
                dome_by_node.setdefault(c.node, []).append(c.dome_latency)
        if not by_node and not dome_by_node:
            continue

        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        frame = Frame()
        series = [Series(f"node {n}", vals, key=i) for i, (n, vals) in enumerate(sorted(by_node.items()))]
        chart = charts.svg(frame, charts.distribution(frame, series, x_label="retrieval latency (s)"),
                            title="Retrieval latency distribution") if series else ""
        legend = charts.legend([(s.label, s.key) for s in series]) if len(series) > 1 else ""
        n_total = sum(len(v) for v in by_node.values())
        figs.append(f"""
        {heading}
        <figure>{chart}{legend}<figcaption>Time from pellet ready to Pellet Taken, per node (n={n_total}).
        {"Shown as individual points — too few samples for a reliable distribution shape." if n_total < 8 else ""}
        </figcaption></figure>
        """)

    if not figs:
        return SectionResult(section_id="generic.retrieval_latency", title="Retrieval Latency", html="", empty=True)

    return SectionResult(
        section_id="generic.retrieval_latency",
        title="Retrieval Latency",
        html="".join(figs),
    )


def presence_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    for run, m in zip(ctx.runs, ctx.metrics):
        bouts, issues = m.presence
        if not bouts:
            continue
        by_node: Dict[int, List] = {}
        for b in bouts:
            by_node.setdefault(b.node, []).append(b)

        rows = []
        for node, node_bouts in sorted(by_node.items()):
            durs = [b.dur for b in node_bouts if b.dur is not None]
            total = sum(durs) if durs else 0.0
            occ_frac = safe_ratio(total, run.duration_s)
            rows.append(
                f"<tr><td>{node}</td><td>{len(node_bouts)}</td>"
                f"<td>{_fmt_duration(median(durs))}</td><td>{_fmt_duration(total)}</td>"
                f"<td>{_fmt_pct(occ_frac)}</td></tr>"
            )

        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        issue_note = ""
        if issues.orphan_closes or issues.censored or issues.duplicate_opens:
            issue_note = (f'<p class="note">{issues.orphan_closes} orphan close(s), '
                          f'{issues.duplicate_opens} duplicate open(s), '
                          f'{issues.censored} still open at run end.</p>')
        parts.append(f"""
        {heading}
        <table>
          <tr><th>Node</th><th>Bouts</th><th>Median duration</th><th>Total occupancy</th><th>Occupancy %</th></tr>
          {"".join(rows)}
        </table>
        {issue_note}
        """)

    if not parts:
        return SectionResult(section_id="generic.presence", title="Presence & Occupancy", html="", empty=True)

    return SectionResult(section_id="generic.presence", title="Presence & Occupancy", html="".join(parts))


def interaction_funnel_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    for run, m in zip(ctx.runs, ctx.metrics):
        if not m.funnel:
            continue
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        frame = Frame(h=220)
        nodes = sorted(m.funnel.keys())
        stages = ["presented", "approached", "dome_opened", "taken"]
        cats = [f"node {n}" for n in nodes]
        series = [
            Series(stage.replace("_", " "), [getattr(m.funnel[n], stage) for n in nodes], key=i)
            for i, stage in enumerate(stages)
        ]
        chart = charts.svg(frame, charts.bars(frame, charts.linear(0, len(nodes), frame.px0, frame.px1),
                                               charts.linear(0, max((getattr(m.funnel[n], "presented") for n in nodes), default=1) or 1,
                                                             frame.py1, frame.py0),
                                               cats, series), title="Interaction funnel")
        legend = charts.legend([(s.label, s.key) for s in series])

        rows = "".join(
            f"<tr><td>{n}</td><td>{f.presented}</td><td>{f.approached}</td><td>{f.dome_opened}</td>"
            f"<td>{f.taken}</td><td>{f.approach_without_dome}</td><td>{f.dome_without_take}</td></tr>"
            for n in nodes for f in [m.funnel[n]]
        )
        parts.append(f"""
        {heading}
        <figure>{chart}{legend}</figure>
        <table>
          <tr><th>Node</th><th>Presented</th><th>Approached</th><th>Dome opened</th><th>Taken</th>
              <th>Approach w/o dome</th><th>Dome w/o take</th></tr>
          {rows}
        </table>
        """)

    if not parts:
        return SectionResult(section_id="generic.interaction_funnel", title="Interaction Funnel", html="", empty=True)

    return SectionResult(section_id="generic.interaction_funnel", title="Interaction Funnel", html="".join(parts))


def throughput_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    for run, m in zip(ctx.runs, ctx.metrics):
        if not m.throughput:
            continue
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        frame = Frame()
        max_v = max(max(p.presented, p.taken) for p in m.throughput) or 1
        x = charts.linear(0, run.duration_s, frame.px0, frame.px1)
        y = charts.linear(0, max_v, frame.py1, frame.py0)
        axes = charts.axes(frame, x, y,
                            x_ticks=charts.time_ticks(run.duration_s),
                            y_ticks=charts.nice_ticks(0, max_v, 5),
                            x_label="time", y_label="cumulative pellets")
        presented_line = charts.line(frame, x, y, [(p.t, p.presented) for p in m.throughput], key=0, step=True)
        taken_line = charts.line(frame, x, y, [(p.t, p.taken) for p in m.throughput], key=1, step=True)
        chart = charts.svg(frame, axes + presented_line + taken_line, title="Cumulative throughput")
        legend = charts.legend([("presented", 0), ("taken", 1)])
        parts.append(f"{heading}<figure>{chart}{legend}</figure>")

    if not parts:
        return SectionResult(section_id="generic.throughput", title="Throughput", html="", empty=True)

    return SectionResult(section_id="generic.throughput", title="Throughput", html="".join(parts))


def faults_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    any_faults = False
    for run, m in zip(ctx.runs, ctx.metrics):
        if not m.faults:
            continue
        any_faults = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        rows = "".join(
            f"<tr><td>{f.node}</td><td>{escape_text(f.code.name)}</td>"
            f"<td>{_fmt_duration(f.t0)}</td><td>{_fmt_duration(f.dur)}</td>"
            f"<td>{'unrecovered at run end' if f.censored else ('inferred' if f.inferred_close else 'recovered')}</td></tr>"
            for f in m.faults
        )
        parts.append(f"""
        {heading}
        <table>
          <tr><th>Node</th><th>Fault</th><th>Started at</th><th>Duration</th><th>Resolution</th></tr>
          {rows}
        </table>
        """)

    if not any_faults:
        return SectionResult(section_id="generic.faults", title="Faults & Downtime", html="<p>No faults recorded.</p>")

    return SectionResult(section_id="generic.faults", title="Faults & Downtime", html="".join(parts))


def apparatus_health_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    any_events = False
    for run, m in zip(ctx.runs, ctx.metrics):
        if not m.health:
            continue
        any_events = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        rows = "".join(
            f"<tr><td>{escape_text(name)}</td><td>{len(evs)}</td>"
            f"<td>{_fmt_duration(evs[0].t)}</td><td>{_fmt_duration(evs[-1].t)}</td></tr>"
            for name, evs in sorted(m.health.items())
        )
        parts.append(f"""
        {heading}
        <table>
          <tr><th>Event</th><th>Count</th><th>First at</th><th>Last at</th></tr>
          {rows}
        </table>
        """)

    if not any_events:
        return SectionResult(section_id="generic.apparatus_health", title="Apparatus Health",
                              html="<p>No vetoes, stalls, or script errors recorded.</p>")

    return SectionResult(section_id="generic.apparatus_health", title="Apparatus Health", html="".join(parts))


SECTIONS: Dict[str, Any] = {
    "provenance": provenance_section,
    "data_quality": data_quality_section,
    "pellet_accounting": pellet_accounting_section,
    "retrieval_latency": retrieval_latency_section,
    "presence": presence_section,
    "interaction_funnel": interaction_funnel_section,
    "throughput": throughput_section,
    "faults": faults_section,
    "apparatus_health": apparatus_health_section,
}
