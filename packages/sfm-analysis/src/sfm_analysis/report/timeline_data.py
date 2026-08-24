"""timeline_data.py — the session-timeline data model: lanes, spans, and
marks, computed once and shared by every renderer that draws a session
timeline. Two renderers consume this today: sections/timeline.py's
static SVG panels, and explorer.py's interactive canvas payload. Neither
renderer computes this data itself, so a lane definition or a span's
meaning can never drift between the printed report and the interactive
explorer -- see ``panel_marks_spans`` below, which both call unmodified
(the explorer just passes the whole run's [0, duration] range instead of
one window).

Nothing here produces HTML or JSON; that is the two renderers' job.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .charts import Mark, Span

# Glyph + key + legend label per discrete node-level event, drawn on the
# "events" lane. Single source of truth for both renderers and the static
# report's legend, so they can never drift apart.
EVENT_GLYPH = {
    "Loaded": ("triangle", 0, "Loaded (pellet)"),
    "Pellet Taken": ("circle", 5, "Pellet Taken"),
    "NoFeedPresented": ("circle", 3, "No-Feed Presented"),
    "FeedSkipped": ("diamond", 7, "Feed Skipped"),
}

# Glyph + key + legend label per session-level mark, drawn on lane 0.
SESSION_MARK_STYLES = {
    "trial": ("tick", 3, "trial start"),  # key 3 = yellow
    "script_stalled": ("cross", 7, "script stalled"),
    "session_end": ("diamond", 5, "session end"),
}

# Span key + legend label per span type drawn (presence/dome/cycle bars, and
# the hatched fault band inserted inline in panel_marks_spans).
SPAN_LEGEND = [("presence", 0), ("dome open", 1), ("dispense cycle", 2), ("fault", 7)]

# A thin blank lane is inserted between each node's 4-lane group (see
# build_lanes) purely for visual whitespace — otherwise n1's last lane and
# n2's first lane sit flush against each other with nothing to tell them
# apart at a glance.
LANES_PER_NODE = 4


def node_lanes(node: int) -> List[str]:
    return [f"n{node} presence", f"n{node} dome", f"n{node} cycle", f"n{node} events"]


def build_lanes(nodes: List[int]) -> List[str]:
    lanes = ["session"]
    for i, node in enumerate(nodes):
        if i > 0:
            lanes.append("")  # spacer — visually separates this node's block from the last
        lanes.extend(node_lanes(node))
    return lanes


def node_lane_base(nodes: List[int], node: int) -> int:
    idx = nodes.index(node)
    return 1 + idx * (LANES_PER_NODE + 1)


def clip(t0: float, t1: float, w0: float, w1: float) -> Optional[Tuple[float, float]]:
    if t1 < w0 or t0 > w1:
        return None
    return max(t0, w0), min(t1, w1)


def panel_marks_spans(run, m, nodes: List[int], w0: float, w1: float) -> Tuple[List[Span], List[Mark]]:
    """
    Every span and mark visible in the window ``[w0, w1]`` (run-relative
    seconds). Pass ``w0=0, w1=run.duration_s`` for the whole run — this is
    what both the static report's overview panel and the interactive
    explorer's full payload do; only the report's stacked detail panels
    call this with a narrower window.
    """
    spans: List[Span] = []
    marks: List[Mark] = []

    for row in run.exp("trial"):
        if w0 <= row.t <= w1:
            glyph, key, _ = SESSION_MARK_STYLES["trial"]
            marks.append(Mark(lane=0, t=row.t, glyph=glyph, key=key, title=f"trial {row.fields.get('trial', '')}"))
    for row in run.exp("script_stalled"):
        if w0 <= row.t <= w1:
            glyph, key, _ = SESSION_MARK_STYLES["script_stalled"]
            marks.append(Mark(lane=0, t=row.t, glyph=glyph, key=key, title="script_stalled"))
    for row in run.exp("session_end"):
        if w0 <= row.t <= w1:
            glyph, key, _ = SESSION_MARK_STYLES["session_end"]
            marks.append(Mark(lane=0, t=row.t, glyph=glyph, key=key, title="session_end"))

    presence_bouts, _ = m.presence
    dome_bouts, _ = m.dome

    for node in nodes:
        base = node_lane_base(nodes, node)

        for b in presence_bouts:
            if b.node != node:
                continue
            clipped = clip(b.t0, b.t1 if b.t1 is not None else w1, w0, w1)
            if clipped:
                spans.append(Span(lane=base + 0, t0=clipped[0], t1=clipped[1], key=0,
                                   open_ended=b.censored, title="presence"))

        for b in dome_bouts:
            if b.node != node:
                continue
            clipped = clip(b.t0, b.t1 if b.t1 is not None else w1, w0, w1)
            if clipped:
                spans.append(Span(lane=base + 1, t0=clipped[0], t1=clipped[1], key=1,
                                   hatch=True, open_ended=b.censored, title="dome open"))

        for c in m.cycles:
            if c.node != node:
                continue
            c_t0 = c.cmd_t if c.cmd_t is not None else (c.ready_t if c.ready_t is not None else c.end_t)
            clipped = clip(c_t0, c.end_t, w0, w1)
            if clipped:
                spans.append(Span(lane=base + 2, t0=clipped[0], t1=clipped[1], key=2,
                                   hatch=not c.fed, open_ended=c.censored,
                                   title=f"cycle {c.index} ({'fed' if c.fed else 'no-feed'})"))
            for label, t in (("Loaded" if c.fed else "NoFeedPresented", c.ready_t),
                              ("Pellet Taken", c.taken_t)):
                if t is not None and w0 <= t <= w1:
                    glyph, key, _ = EVENT_GLYPH[label]
                    marks.append(Mark(lane=base + 3, t=t, glyph=glyph, key=key, title=label))

        for row in run.by_node(node):
            if row.frame_type == "EVENT" and row.event_name == "FeedSkipped" and w0 <= row.t <= w1:
                glyph, key, _ = EVENT_GLYPH["FeedSkipped"]
                marks.append(Mark(lane=base + 3, t=row.t, glyph=glyph, key=key, title="FeedSkipped"))
            if row.frame_type == "EVENT" and row.event_name.startswith("Fault:") and w0 <= row.t <= w1:
                # Filled (not hatched) — a solid red band reads unambiguously
                # against the hatched orange dome-open band on the lane above.
                spans.append(Span(lane=base + 2, t0=row.t, t1=min(w1, row.t + max(2.0, (w1 - w0) * 0.01)),
                                   key=7, hatch=False, title=row.event_name))

    return spans, marks
