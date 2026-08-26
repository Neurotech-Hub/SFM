"""sections/timeline.py — the session raster: the centerpiece of the report.

One lane group per node (presence / dome / dispense-cycle / discrete
events), plus a session-scope lane for trial boundaries and script
health. This is what actually shows an experimenter that a session
stalled for 24 minutes before the first trial, or that an animal ignored
one node for half the run — the summary sections elsewhere are all
derived from what's drawn here.

Long runs auto-paginate: a whole-run overview strip, then stacked
``window_s``-second detail panels — the print-native substitute for pan/
zoom in this static document. The interactive explorer (``explorer.py``,
a sibling HTML file with real pan/zoom) draws the identical lanes/spans/
marks from the same ``timeline_data`` module this file uses, so the two
views can never show different data for the same session.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .. import charts
from ..charts import Frame, Mark, escape_text
from ..explorer import build_explorer_payload
from ..explorer_render import EXPLORER_CSS, EXPLORER_JS, explorer_bootstrap_js, explorer_widget_html
from ..metrics import DEFAULT_ACTIVITY_EVENTS, activity_by_day
from ..timeline_data import (
    EVENT_GLYPH as _EVENT_GLYPH,
    SESSION_MARK_STYLES as _SESSION_MARK_STYLES,
    SPAN_LEGEND as _SPAN_LEGEND,
    build_lanes as _build_lanes,
    node_lane_base as _node_lane_base,
    panel_marks_spans as _panel_marks_spans,
)
from ..schema import SectionContext, SectionResult

# The ref this module's interactive section registers under (SECTIONS
# below) -- session_raster_section checks ctx.active_refs against this
# exact string to decide whether it should go print-only, so the two
# stay coupled by name rather than by import-time ordering.
EXPLORER_REF = "timeline.explorer"


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
    # None (not set in the design JSON) means "adaptive": window_s scales
    # up with run duration so panel count never exceeds ~12, rather than
    # a 24h run producing 144 stacked panels at a fixed 600s. An explicit
    # window_s in options overrides this per-run scaling entirely and
    # pins a single fixed panel width regardless of duration, same as
    # before this became adaptive. min_window_s only applies in the
    # adaptive case -- it's the short-run floor (a design that wants
    # coarser panels than 600s even for a short run, e.g. free_feeding's
    # slower cadence, sets this instead of a fixed window_s).
    window_s_opt = ctx.opts.get("window_s")
    min_window_s = float(ctx.opts.get("min_window_s", 600.0))
    figs = []

    for run, m in zip(ctx.runs, ctx.metrics):
        nodes = sorted(set(run.nodes) | {r.node_id for r in run.rows if r.node_id != 0})
        if not nodes:
            continue
        duration = max(run.duration_s, 1.0)
        window_s = float(window_s_opt) if window_s_opt is not None else max(min_window_s, duration / 12.0)
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        overview = _panel(run, m, nodes, 0.0, duration, tall=False)
        span_legend = charts.legend(_SPAN_LEGEND, prefix="Bands:")
        mark_legend = charts.legend_glyphs(
            [(label, glyph, key) for glyph, key, label in _SESSION_MARK_STYLES.values()]
            + [(label, glyph, key) for glyph, key, label in _EVENT_GLYPH.values()],
            prefix="Markers:",
        )
        # Legend sits once, above every panel for this run — it's shared
        # (same bands/markers throughout), so it belongs with the run as a
        # whole rather than repeated per panel or trailing after them.
        figs.append(f'{heading}{span_legend}{mark_legend}')
        figs.append(f'<figure><figcaption>Full-session overview '
                    f'({_fmt_duration(duration)}).</figcaption>{overview}</figure>')

        if duration > window_s * 1.25:
            n_windows = int(duration // window_s) + (1 if duration % window_s else 0)
            for i in range(n_windows):
                w0 = i * window_s
                w1 = min(duration, (i + 1) * window_s)
                panel = _panel(run, m, nodes, w0, w1, tall=True)
                figs.append(f'<figure><figcaption>Detail: '
                            f'{_fmt_duration(w0)}–{_fmt_duration(w1)}</figcaption>{panel}</figure>')

    if not figs:
        return SectionResult(section_id="timeline.session_raster", title="Session Timeline", html="", empty=True)

    # When the interactive explorer is also in this document, these panels
    # are redundant on screen (the explorer draws the same lanes/spans/
    # marks, plus real zoom) -- but the printed PDF can't run the JS that
    # draws the explorer, so the panels must still appear there. See
    # PAGE_CSS's .print-only and explorer_section below.
    html = "".join(figs)
    title = "Session Timeline"
    if EXPLORER_REF in ctx.active_refs:
        html = f'<div class="print-only">{html}</div>'
        title = "Session Timeline (printed panels)"

    return SectionResult(
        section_id="timeline.session_raster",
        title=title,
        html=html,
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


def _actogram_panel(
    days,
    *,
    event_label: str,
) -> str:
    lanes = [d.date.strftime("%b %d") for d in days]
    frame = Frame(h=max(140, 40 + len(lanes) * 16))
    x = charts.linear(0.0, 24.0, frame.px0, frame.px1)
    lane_gap = (frame.py1 - frame.py0) / len(lanes)
    lane_h = min(14, lane_gap * 0.8)

    body = []
    # No light/dark shading: the rig does not record the facility's light
    # schedule, so any shading here would be a fixed clock-time assumption
    # drawn as if it were measured data. Time of day is on the axis; a
    # reader who knows their own light cycle can apply it themselves.

    body.append(f'<line class="axis-line" x1="{frame.px0:.1f}" y1="{frame.py1:.1f}" '
               f'x2="{frame.px1:.1f}" y2="{frame.py1:.1f}"/>')
    for hour in (0, 6, 12, 18, 24):
        px = x(float(hour))
        body.append(f'<line class="gridline" x1="{px:.1f}" y1="{frame.py0:.1f}" x2="{px:.1f}" y2="{frame.py1:.1f}"/>')
        body.append(f'<text x="{px:.1f}" y="{frame.py1 + 14:.1f}" text-anchor="middle">{hour}:00</text>')

    marks = [
        Mark(
            lane=li, t=t, glyph="tick", key=0,
            title=f"{event_label} · {lanes[li]} {int(t):02d}:{int((t % 1) * 60):02d}",
        )
        for li, day in enumerate(days) for t in day.times
    ]
    body.append(charts.raster(frame, x, lanes, marks, lane_h=lane_h))

    return charts.svg(
        frame, "".join(body),
        title=f"actogram — {event_label}",
        desc=(
            f"Each tick is a {event_label} event from the session log. "
            "One row per calendar day; x-axis is rig-local time of day (0–24 h)."
        ),
    )


def _actogram_event_names(opts: dict) -> Tuple[str, ...]:
    """CAN EVENT display names that count as actogram ticks.

    Default is presence onsets (``DEFAULT_ACTIVITY_EVENTS``). Design JSON
    may pass ``event_names`` as a string or a list — several names are
    pooled into one series (union of ticks), not separate colours.
    """
    raw = opts.get("event_names", DEFAULT_ACTIVITY_EVENTS)
    if isinstance(raw, str):
        names: Sequence[str] = (raw,)
    else:
        names = tuple(str(n) for n in raw if str(n).strip())
    return tuple(names) if names else DEFAULT_ACTIVITY_EVENTS


def actogram_section(ctx: SectionContext) -> Optional[SectionResult]:
    """
    One row per calendar day, time-of-day on the x-axis — the figure that
    replaces dozens of session_raster detail panels for a multi-day run.
    See metrics.activity_by_day for what counts as "activity" and why
    grouping needs no UTC offset to be correct.

    Empty (nothing rendered) for any run with fewer than 2 distinct days
    of activity — a single-day run gets nothing an actogram would add
    over the session raster above it.

    ``event_names`` (design-JSON option) selects which CAN EVENT rows are
    plotted; omit it to keep the presence-onset default. Multiple names
    become one tick series. The section heading names those events so a
    printed page is unambiguous about what the ticks are.
    """
    event_names = _actogram_event_names(ctx.opts)
    event_label = ", ".join(event_names)
    title = f"Actogram — {event_label}"
    figs = []

    for run in ctx.runs:
        days = activity_by_day(run, event_names=event_names)
        if len(days) < 2:
            continue
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        panel = _actogram_panel(days, event_label=event_label)
        figs.append(
            f'{heading}<figure><figcaption>{len(days)} days.'
            f'</figcaption>{panel}</figure>'
        )

    if not figs:
        return SectionResult(section_id="timeline.actogram", title=title, html="", empty=True)

    return SectionResult(
        section_id="timeline.actogram",
        title=title,
        html="".join(figs),
    )


def explorer_section(ctx: SectionContext) -> Optional[SectionResult]:
    """
    The interactive timeline: one real pan/zoom/brush-zoom widget per run,
    embedded directly in the report rather than a sibling file (see
    explorer.py/explorer_render.py). Draws from exactly the same
    timeline_data.panel_marks_spans session_raster_section uses above, so
    the two can never disagree about what a span or mark means.

    session_raster_section (this module) checks EXPLORER_REF against
    ctx.active_refs to go print-only whenever this section is also
    active, so the printed PDF still gets full detail without this
    section's canvas -- which print can't run.
    """
    entries = []
    legends_done = False
    span_legend = mark_legend = ""
    body = []

    for i, (run, m) in enumerate(zip(ctx.runs, ctx.metrics)):
        nodes = sorted(set(run.nodes) | {r.node_id for r in run.rows if r.node_id != 0})
        if not nodes:
            continue
        payload = build_explorer_payload(run, m)
        dom_id = f"sfm-explorer-{i}"
        entries.append({"dom_id": dom_id, "payload": payload})

        if not legends_done:
            # Legends are the same regardless of run (fixed vocabularies in
            # timeline_data.py), so build them once rather than repeating
            # per widget.
            span_legend = charts.legend(_SPAN_LEGEND, prefix="Bands:")
            mark_legend = charts.legend_glyphs(
                [(label, glyph, key) for glyph, key, label in _SESSION_MARK_STYLES.values()]
                + [(label, glyph, key) for glyph, key, label in _EVENT_GLYPH.values()],
                prefix="Markers:",
            )
            legends_done = True

        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        body.append(f"{heading}{explorer_widget_html(payload, dom_id=dom_id)}")

    if not entries:
        return SectionResult(section_id=EXPLORER_REF, title="Interactive Timeline", html="", empty=True)

    # A no-JS viewer must not see a blank hole where the timeline should
    # be -- fall back to the print-only static panels being visible on
    # screen too, mirroring what happens automatically when actually
    # printing.
    noscript = '<noscript><style>.print-only{display:block!important}.sfm-explorer{display:none}</style></noscript>'

    return SectionResult(
        section_id=EXPLORER_REF,
        title="Interactive Timeline",
        html=f"{noscript}{span_legend}{mark_legend}{''.join(body)}",
        extra_css=EXPLORER_CSS,
        extra_js=EXPLORER_JS + explorer_bootstrap_js(entries),
        page_break_before=True,
    )


SECTIONS = {
    "session_raster": session_raster_section,
    "actogram": actogram_section,
    "explorer": explorer_section,
}
