"""explorer.py — builds the JSON payload for the interactive session
explorer, a sibling ``<session>_explorer.html`` to the printed report.

The payload is built from exactly the same ``timeline_data.panel_marks_spans``
the static report's overview panel uses, called once with the whole run's
``[0, duration]`` range — so the printed report and the interactive
explorer can never disagree about what a span or mark means; there is
only one place that computes "what's a presence bout, what's a dispense
cycle" (see ``timeline_data.py``). This module only packs that data into
a compact, canvas-ready JSON structure; ``explorer_template.py`` renders
it into the actual HTML/JS document.

Only the palette is emitted from ``style.py`` — not dash patterns or a
per-key glyph table. The timeline view doesn't use either: spans use a
fixed dash for the "still open" (censored) border regardless of key, and
marks already carry their own explicit glyph string per mark (chosen by
event type, not derived from ``key``) — see ``timeline_data.EVENT_GLYPH``
/ ``SESSION_MARK_STYLES``. Emitting tables the renderer wouldn't use
would just be dead payload weight.
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import style
from .metrics import RunMetrics
from .session import RunData
from .timeline_data import build_lanes, panel_marks_spans


def build_explorer_payload(run: RunData, m: RunMetrics) -> Dict[str, Any]:
    """
    A JSON-serializable dict: everything the explorer's canvas renderer
    needs to draw this run's full timeline, pan, and brush-zoom, with no
    further queries back into Python once the page has loaded.

    Spans/marks are packed as flat arrays (not objects) to keep the
    payload small on a run with tens of thousands of them; ``strings``
    is a deduped pool for the (often-repeated) title text, referenced by
    index rather than repeating "presence" or "cycle 3 (fed)" per row.
    """
    nodes = sorted(set(run.nodes) | {r.node_id for r in run.rows if r.node_id != 0})
    duration = max(run.duration_s, 1.0)
    lanes = build_lanes(nodes)
    spans, marks = panel_marks_spans(run, m, nodes, 0.0, duration)

    strings: List[str] = []
    string_idx: Dict[str, int] = {}

    def intern(s: str) -> int:
        idx = string_idx.get(s)
        if idx is None:
            idx = len(strings)
            string_idx[s] = idx
            strings.append(s)
        return idx

    spans_packed = [
        [sp.lane, round(sp.t0, 3), round(sp.t1, 3), sp.key,
         int(sp.hatch), int(sp.open_ended), intern(sp.title)]
        for sp in spans
    ]
    marks_packed = [
        [mk.lane, round(mk.t, 3), mk.glyph, mk.key, intern(mk.title)]
        for mk in marks
    ]

    # t0_iso is the naive local wall-clock string of this run's first row
    # (run.rows is time-sorted by split_runs, so rows[0] is t=0 exactly).
    # The explorer's JS reconstructs every other row's wall-clock time as
    # pure calendar arithmetic on this string -- t0_iso's components
    # parsed and t seconds added, using Date.UTC()/getUTC*() throughout
    # even though nothing here is really UTC. That's deliberate: it's
    # the JS equivalent of timezones.py's wall_clock()/time_of_day(),
    # which read timestamp_iso directly rather than reconstructing from
    # epoch ms -- doing the epoch-ms route in the browser would silently
    # use the *viewer's own* timezone instead of the rig's, exactly the
    # bug that module exists to prevent.
    t0_iso = run.rows[0].iso if run.rows else ""

    return {
        "meta": {
            "session": run.session,
            "run_id": run.run_id,
            "run_label": run.run_label,
            "duration_s": duration,
            "t0_iso": t0_iso,
            "utc_offset_s": run.utc_offset_s,
            "nodes": nodes,
            "lanes": lanes,
        },
        "style": {
            "palette": list(style.PALETTE),
        },
        # [lane, t0, t1, key, hatch, open_ended, titleIdx]
        "spans": spans_packed,
        # [lane, t, glyph, key, titleIdx]
        "marks": marks_packed,
        "strings": strings,
    }
