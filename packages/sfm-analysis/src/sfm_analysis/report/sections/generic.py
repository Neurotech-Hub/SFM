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

from ...protocol import fault_user_message, reload_reason_message
from .. import charts
from ..charts import Frame, Series, TimeSeries, escape_text
from ..naming import parse_session_name
from ..schema import SectionContext, SectionResult
from ..stats import median, safe_ratio
from ..time_axis import TimeBase


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


def _downtime_by_node(faults) -> Dict[int, float]:
    totals: Dict[int, float] = {}
    for fault in faults:
        if fault.dur is None:
            continue
        totals[fault.node] = totals.get(fault.node, 0.0) + fault.dur
    return totals


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


def _run_heading(ctx: SectionContext, label: str) -> str:
    if len(ctx.runs) <= 1:
        return ""
    return f"<h3>{escape_text(label)}</h3>"


def _faults_block(run, faults, base) -> str:
    """The Faults & Downtime table for one run. Empty when the run has no faults."""
    if not faults:
        return ""
    ordered = sorted(faults, key=lambda f: (f.t0, f.node))
    rows = "".join(
        "<tr>"
        f"<td>{f.node}</td>"
        f"<td>{escape_text(f.code.name)}</td>"
        f"<td>{escape_text(fault_user_message(f.code))}</td>"
        f"<td>{charts.dual_time(base, f.t0)}</td>"
        f"<td>{'still open at run end' if f.censored else charts.dual_time(base, f.t1 or 0)}</td>"
        f"<td>{_fmt_duration(f.dur)}</td>"
        f"<td>{escape_text(f.cleared_by)}</td>"
        "</tr>"
        for f in ordered
    )
    downtime = _downtime_by_node(faults)
    summary_bits = []
    for node in sorted(downtime):
        total = downtime[node]
        share = (total / run.duration_s) if run.duration_s else None
        summary_bits.append(f"node {node}: {_fmt_duration(total)} ({_fmt_pct(share)})")
    summary = (
        f'<p class="note">Downtime: {escape_text("; ".join(summary_bits))}.</p>'
        if summary_bits else ""
    )
    return f"""
    <h3>Faults &amp; Downtime</h3>
    <table>
      <tr><th>Node</th><th>Fault</th><th>What it means</th><th>Started</th>
      <th>Cleared</th><th>Downtime</th><th>Cleared by</th></tr>
      {rows}
    </table>
    {summary}
    """


def _data_quality_events(ctx: SectionContext) -> tuple:
    """Pellet reloads, mechanism counts, then Faults & Downtime, then script events.

    Each block is omitted when it has nothing to show. Returns (html, has_clock).
    """
    parts: List[str] = []
    has_clock = False
    for run, m in zip(ctx.runs, ctx.metrics):
        heading = _run_heading(ctx, run.run_label)
        base = TimeBase.from_run(run)
        clock = base.wall(0) is not None

        if m.reloads:
            has_clock = has_clock or clock
            rows = "".join(
                "<tr>"
                f"<td>{event.node}</td>"
                f"<td>{charts.dual_time(base, event.t)}</td>"
                f"<td>{escape_text(reload_reason_message(event.reason))}</td>"
                f"<td>{escape_text((str(event.attempt) + ' of 3') if event.attempt else '—')}</td>"
                "</tr>"
                for event in m.reloads
            )
            parts.append(f"""
            {heading}
            <h3>Pellet reloads</h3>
            <table>
              <tr><th>Node</th><th>Time</th><th>Reason</th><th>Attempt</th></tr>
              {rows}
            </table>
            """)
            heading = ""

        active = [s for s in m.mechanism.values() if (
            s.retracts or s.holds or s.reload_count or s.dome_open_warnings or s.feed_skips
        )]
        if active:
            rows = "".join(
                "<tr>"
                f"<td>{s.node}</td><td>{s.retracts}</td><td>{s.holds}</td>"
                f"<td>{_fmt_duration(s.hold_total_s)}</td>"
                f"<td>{_fmt_duration(s.hold_longest_s)}</td>"
                f"<td>{s.dome_open_warnings}</td><td>{s.feed_skips}</td>"
                "</tr>"
                for s in sorted(active, key=lambda s: s.node)
            )
            parts.append(f"""
            {heading}
            <h3>Mechanism events per node</h3>
            <p class="note">Holds and retracts are the firmware keeping the pellet out of reach
            while the dome is open. They are expected behaviour, not faults.</p>
            <table>
              <tr><th>Node</th><th>Retracts</th><th>Dome holds</th><th>Total hold</th>
              <th>Longest hold</th><th>Dome-open warnings</th><th>Feed skips</th></tr>
              {rows}
            </table>
            """)
            heading = ""

        block = _faults_block(run, m.faults, base)
        if block:
            has_clock = has_clock or clock
            parts.append(heading + block)
            heading = ""

        if m.health:
            has_clock = has_clock or clock
            rows = []
            for name, evs in sorted(m.health.items()):
                ordered = sorted(evs, key=lambda r: r.t)
                rows.append(
                    "<tr>"
                    f"<td>{escape_text(name)}</td><td>{len(ordered)}</td>"
                    f"<td>{charts.dual_time(base, ordered[0].t)}</td>"
                    f"<td>{charts.dual_time(base, ordered[-1].t)}</td>"
                    "</tr>"
                )
            parts.append(f"""
            {heading}
            <h3>Script and control events</h3>
            <table>
              <tr><th>Event</th><th>Count</th><th>First</th><th>Last</th></tr>
              {''.join(rows)}
            </table>
            """)

    return "".join(parts), has_clock


def data_quality_section(ctx: SectionContext) -> Optional[SectionResult]:
    """Issue notes are only node reboots, missed event frames, and a missing session_end.

    Faults, pellet reloads, and mechanism counts are listed below those notes
    when they occurred. A run that passes the three checks adds no sentence.
    """
    notes: List[str] = []

    for run, m in zip(ctx.runs, ctx.metrics):
        prefix = f"{run.run_label}: " if len(ctx.runs) > 1 else ""
        if not run.has_session_end:
            notes.append(f"{prefix}No session_end — session may have been interrupted "
                         f"(power loss, crash, or the operator closed the GUI without Stop).")

        for node, acct in sorted(m.pellets.items()):
            if acct.missed_frames:
                notes.append(f"{prefix}node {node}: {acct.missed_frames} event frame(s) never arrived; "
                             f"the node counter caught the gap and the session totals above are "
                             f"corrected, but those pellets have no timestamped row.")
            if acct.counter_restarts:
                notes.append(f"{prefix}node {node}: rebooted {acct.counter_restarts} time(s) mid-run "
                             f"(pellet counter restarted). Counts continue across the reboot; "
                             f"anything dispensed while it was down is not in this run.")

    notes_html = "".join(f'<p class="note">{escape_text(n)}</p>' for n in notes)
    extra_html, has_clock = _data_quality_events(ctx)

    return SectionResult(
        section_id="generic.data_quality",
        title="Data Quality",
        html=notes_html + extra_html,
        summary={"note_count": len(notes)},
        notes=notes,
        uses_time_axis=has_clock,
    )


def pellet_accounting_section(ctx: SectionContext) -> Optional[SectionResult]:
    any_data = any(m.pellets for m in ctx.metrics)
    if not any_data:
        return SectionResult(section_id="generic.pellet_accounting", title="Pellet Accounting", html="", empty=True)

    parts = []
    for run, m in zip(ctx.runs, ctx.metrics):
        rows = "".join(
            f"<tr><td>{node}</td><td>{a.presented_total}</td><td>{a.no_feed_presented}</td>"
            f"<td>{a.taken_total}</td><td>{a.feed_skipped}</td><td>{_fmt_pct(a.take_rate)}</td>"
            f"<td>{a.missed_frames or ''}</td></tr>"
            for node, a in sorted(m.pellets.items())
        )
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        parts.append(f"""
        {heading}
        <table>
          <tr><th>Node</th><th>Presented</th><th>No-feed</th><th>Taken</th><th>Skipped</th>
              <th>Take rate</th><th>Recovered frames</th></tr>
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
        <figure><figcaption>Time from pellet ready to Pellet Taken, per node (n={n_total}).
        {"Shown as individual points — too few samples for a reliable distribution shape." if n_total < 8 else ""}
        </figcaption>{legend}{chart}</figure>
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
        <figure>{legend}{chart}</figure>
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
    has_clock = False
    for run, m in zip(ctx.runs, ctx.metrics):
        if not m.throughput:
            continue
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        base = TimeBase.from_run(run)
        has_clock = has_clock or base.wall(0) is not None
        chart = charts.time_series(
            base, 0.0, run.duration_s,
            [
                TimeSeries("presented", [(p.t, p.presented) for p in m.throughput], key=0, step=True),
                TimeSeries("taken", [(p.t, p.taken) for p in m.throughput], key=1, step=True),
            ],
            y_label="cumulative pellets",
            title="Cumulative throughput",
        )
        legend = charts.legend([("presented", 0), ("taken", 1)])
        parts.append(f"{heading}<figure>{legend}{chart}</figure>")

    if not parts:
        return SectionResult(section_id="generic.throughput", title="Throughput", html="", empty=True)

    return SectionResult(
        section_id="generic.throughput",
        title="Throughput",
        html="".join(parts),
        uses_time_axis=has_clock,
    )


def faults_section(ctx: SectionContext) -> Optional[SectionResult]:
    """Alias that renders nothing.

    The fault table lives inside Data Quality. A design that still names
    ``generic.faults`` skips this section instead of showing it twice or
    showing an error box.
    """
    del ctx
    return SectionResult(
        section_id="generic.faults",
        title="Faults & Downtime",
        html="",
        empty=True,
    )


def apparatus_health_section(ctx: SectionContext) -> Optional[SectionResult]:
    """Alias that renders nothing.

    Hand-written designs may still name ``generic.apparatus_health``. Leaving
    the entry in SECTIONS means those designs skip the section instead of
    showing an error box. Faults, pellet reloads, and mechanism counts live
    in Data Quality.
    """
    del ctx
    return SectionResult(
        section_id="generic.apparatus_health",
        title="Apparatus Health",
        html="",
        empty=True,
    )


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
