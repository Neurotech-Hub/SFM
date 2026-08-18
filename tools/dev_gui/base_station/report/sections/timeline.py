"""sections/timeline.py — the session raster: the centerpiece of the report.

One lane group per node (presence / dome / dispense-cycle / discrete
events), plus a session-scope lane for trial boundaries and script
health. This is what actually shows an experimenter that a session
stalled for 24 minutes before the first trial, or that an animal ignored
one node for half the run — the summary sections elsewhere are all
derived from what's drawn here.

Long runs auto-paginate: a whole-run overview strip, then stacked
``window_s``-second detail panels — the print-native substitute for pan/
zoom, since this document ships no JavaScript.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .. import charts
from ..charts import Frame, Mark, Span, escape_text
from ..metrics import Cycle
from ..schema import SectionContext, SectionResult

# Glyph + key + legend label per discrete node-level event, drawn on the
# "events" lane. Single source of truth for both drawing (_panel_marks_spans)
# and the legend (session_raster_section), so they can never drift apart.
_EVENT_GLYPH = {
    "Loaded": ("triangle", 0, "Loaded (pellet)"),
    "Pellet Taken": ("circle", 5, "Pellet Taken"),
    "NoFeedPresented": ("circle", 3, "No-Feed Presented"),
    "FeedSkipped": ("diamond", 7, "Feed Skipped"),
}

# Glyph + key + legend label per session-level mark, drawn on lane 0.
_SESSION_MARK_STYLES = {
    "trial": ("tick", 3, "trial start"),  # key 3 = yellow
    "script_stalled": ("cross", 7, "script stalled"),
    "session_end": ("diamond", 5, "session end"),
}

# Span key + legend label per span type drawn (presence/dome/cycle bars, and
# the hatched fault band inserted inline in _panel_marks_spans).
_SPAN_LEGEND = [("presence", 0), ("dome open", 1), ("dispense cycle", 2), ("fault", 7)]

# A thin blank lane is inserted between each node's 4-lane group (see
# _build_lanes) purely for visual whitespace — otherwise n1's last lane and
# n2's first lane sit flush against each other with nothing to tell them
# apart at a glance.
_LANES_PER_NODE = 4


def _node_lanes(node: int) -> List[str]:
    return [f"n{node} presence", f"n{node} dome", f"n{node} cycle", f"n{node} events"]


def _build_lanes(nodes: List[int]) -> List[str]:
    lanes = ["session"]
    for i, node in enumerate(nodes):
        if i > 0:
            lanes.append("")  # spacer — visually separates this node's block from the last
        lanes.extend(_node_lanes(node))
    return lanes


def _node_lane_base(nodes: List[int], node: int) -> int:
    idx = nodes.index(node)
    return 1 + idx * (_LANES_PER_NODE + 1)


def _clip(t0: float, t1: float, w0: float, w1: float) -> Optional[Tuple[float, float]]:
    if t1 < w0 or t0 > w1:
        return None
    return max(t0, w0), min(t1, w1)


def _panel_marks_spans(run, m, nodes: List[int], w0: float, w1: float) -> Tuple[List[Span], List[Mark]]:
    spans: List[Span] = []
    marks: List[Mark] = []

    for row in run.exp("trial"):
        if w0 <= row.t <= w1:
            glyph, key, _ = _SESSION_MARK_STYLES["trial"]
            marks.append(Mark(lane=0, t=row.t, glyph=glyph, key=key, title=f"trial {row.fields.get('trial', '')}"))
    for row in run.exp("script_stalled"):
        if w0 <= row.t <= w1:
            glyph, key, _ = _SESSION_MARK_STYLES["script_stalled"]
            marks.append(Mark(lane=0, t=row.t, glyph=glyph, key=key, title="script_stalled"))
    for row in run.exp("session_end"):
        if w0 <= row.t <= w1:
            glyph, key, _ = _SESSION_MARK_STYLES["session_end"]
            marks.append(Mark(lane=0, t=row.t, glyph=glyph, key=key, title="session_end"))

    presence_bouts, _ = m.presence
    dome_bouts, _ = m.dome

    for node in nodes:
        base = _node_lane_base(nodes, node)

        for b in presence_bouts:
            if b.node != node:
                continue
            clipped = _clip(b.t0, b.t1 if b.t1 is not None else w1, w0, w1)
            if clipped:
                spans.append(Span(lane=base + 0, t0=clipped[0], t1=clipped[1], key=0,
                                   open_ended=b.censored, title="presence"))

        for b in dome_bouts:
            if b.node != node:
                continue
            clipped = _clip(b.t0, b.t1 if b.t1 is not None else w1, w0, w1)
            if clipped:
                spans.append(Span(lane=base + 1, t0=clipped[0], t1=clipped[1], key=1,
                                   hatch=True, open_ended=b.censored, title="dome open"))

        for c in m.cycles:
            if c.node != node:
                continue
            c_t0 = c.cmd_t if c.cmd_t is not None else (c.ready_t if c.ready_t is not None else c.end_t)
            clipped = _clip(c_t0, c.end_t, w0, w1)
            if clipped:
                spans.append(Span(lane=base + 2, t0=clipped[0], t1=clipped[1], key=2,
                                   hatch=not c.fed, open_ended=c.censored,
                                   title=f"cycle {c.index} ({'fed' if c.fed else 'no-feed'})"))
            for label, t in (("Loaded" if c.fed else "NoFeedPresented", c.ready_t),
                              ("Pellet Taken", c.taken_t)):
                if t is not None and w0 <= t <= w1:
                    glyph, key, _ = _EVENT_GLYPH[label]
                    marks.append(Mark(lane=base + 3, t=t, glyph=glyph, key=key, title=label))

        for row in run.by_node(node):
            if row.frame_type == "EVENT" and row.event_name == "FeedSkipped" and w0 <= row.t <= w1:
                glyph, key, _ = _EVENT_GLYPH["FeedSkipped"]
                marks.append(Mark(lane=base + 3, t=row.t, glyph=glyph, key=key, title="FeedSkipped"))
            if row.frame_type == "EVENT" and row.event_name.startswith("Fault:") and w0 <= row.t <= w1:
                # Filled (not hatched) — a solid red band reads unambiguously
                # against the hatched orange dome-open band on the lane above.
                spans.append(Span(lane=base + 2, t0=row.t, t1=min(w1, row.t + max(2.0, (w1 - w0) * 0.01)),
                                   key=7, hatch=False, title=row.event_name))

    return spans, marks


def _panel(run, m, nodes: List[int], w0: float, w1: float, *, tall: bool) -> str:
    lanes = _build_lanes(nodes)
    frame = Frame(h=max(140, 30 + len(lanes) * (18 if tall else 12)))
    x = charts.linear(w0, w1, frame.px0, frame.px1)
    spans_data, marks_data = _panel_marks_spans(run, m, nodes, w0, w1)

    body = []
    body.append(f'<line class="axis-line" x1="{frame.px0:.1f}" y1="{frame.py1:.1f}" '
               f'x2="{frame.px1:.1f}" y2="{frame.py1:.1f}"/>')
    for t, label in charts.time_ticks(w1 - w0, target=8):
        px = x(w0 + t)
        body.append(f'<line class="gridline" x1="{px:.1f}" y1="{frame.py0:.1f}" x2="{px:.1f}" y2="{frame.py1:.1f}"/>')
        body.append(f'<text x="{px:.1f}" y="{frame.py1 + 14:.1f}" text-anchor="middle">{escape_text(label)}</text>')

    lane_h = 12 if tall else 8
    body.append(charts.spans(frame, x, lanes, spans_data, lane_h=lane_h))
    body.append(charts.raster(frame, x, lanes, marks_data, lane_h=lane_h))

    # A light divider through each spacer lane, so n1's block and n2's block
    # read as distinct groups instead of one continuous stack of lanes.
    lane_gap = (frame.py1 - frame.py0) / max(1, len(lanes))
    for node in nodes[1:]:
        spacer_idx = _node_lane_base(nodes, node) - 1
        y_mid = frame.py0 + spacer_idx * lane_gap + lane_gap / 2
        body.append(f'<line class="node-divider" x1="{frame.px0:.1f}" y1="{y_mid:.1f}" '
                    f'x2="{frame.px1:.1f}" y2="{y_mid:.1f}"/>')

    return charts.svg(frame, "".join(body), title=f"{run.run_label} timeline ({w0:.0f}s–{w1:.0f}s)")


def session_raster_section(ctx: SectionContext) -> Optional[SectionResult]:
    window_s = float(ctx.opts.get("window_s", 600))
    figs = []

    for run, m in zip(ctx.runs, ctx.metrics):
        nodes = sorted(set(run.nodes) | {r.node_id for r in run.rows if r.node_id != 0})
        if not nodes:
            continue
        duration = max(run.duration_s, 1.0)
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        overview = _panel(run, m, nodes, 0.0, duration, tall=False)
        span_legend = charts.legend(_SPAN_LEGEND, prefix="Bands:")
        mark_legend = charts.legend_glyphs(
            [(label, glyph, key) for glyph, key, label in _SESSION_MARK_STYLES.values()]
            + [(label, glyph, key) for glyph, key, label in _EVENT_GLYPH.values()],
            prefix="Markers:",
        )
        figs.append(f'{heading}<figure>{overview}<figcaption>Full-session overview '
                    f'({_fmt_duration(duration)}).</figcaption></figure>')

        if duration > window_s * 1.25:
            n_windows = int(duration // window_s) + (1 if duration % window_s else 0)
            for i in range(n_windows):
                w0 = i * window_s
                w1 = min(duration, (i + 1) * window_s)
                panel = _panel(run, m, nodes, w0, w1, tall=True)
                figs.append(f'<figure>{panel}<figcaption>Detail: '
                            f'{_fmt_duration(w0)}–{_fmt_duration(w1)}</figcaption></figure>')

        figs.append(span_legend)
        figs.append(mark_legend)

    if not figs:
        return SectionResult(section_id="timeline.session_raster", title="Session Timeline", html="", empty=True)

    return SectionResult(
        section_id="timeline.session_raster",
        title="Session Timeline",
        html="".join(figs),
        page_break_before=True,
    )


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


SECTIONS = {
    "session_raster": session_raster_section,
}
