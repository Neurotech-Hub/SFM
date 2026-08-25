"""explorer_render.py — renders the explorer payload into an embeddable,
reusable interactive widget: real pan/zoom/brush-zoom over a run's
timeline, drawn on `<canvas>`.

This used to render a whole standalone `<html>` document (one run's
timeline, one file). It now renders a *widget* -- a `<div>` fragment plus
shared CSS/JS -- so `sections/timeline.py`'s `explorer_section` can embed
one instance per run directly inside the printed report, and a report
covering several runs gets several independent widgets on one page.

Self-containment is the same hard invariant as the rest of the report
(see render.py's module docstring): no external `http(s)://` reference,
nothing that requires network access. This module's JS is what makes
`extra_js` non-empty on the document it's embedded in -- see
`sections/timeline.py`'s `explorer_section` for how `EXPLORER_CSS`/
`EXPLORER_JS` reach `render.py`, and `render.py` for how the document
caps at exactly one inline `<script>` however many widgets exist.

The JSON payload is embedded via `json.dumps(..., separators=(",", ":"))`
inline, with `</script` runs escaped so a title or event detail string
containing that literal text can never terminate the script tag early
(the same class of injection risk `render.py` already guards against for
`<style>`, applied here to `<script>`).
"""

from __future__ import annotations

import json
from typing import Any, Dict

# Each widget is identified by a caller-supplied dom_id (unique within
# the document -- explorer_section.py uses "sfm-explorer-<i>" per run)
# rather than a module-level counter, so two independent renders in the
# same process (e.g. two tests) can't collide.

# Scoped under .sfm-explorer so multiple widgets, and the report's own
# unrelated styles, can coexist on one page without bleeding into each
# other. Legend styling (.legend / .legend-item / .legend-swatch / ...)
# is deliberately NOT duplicated here -- report/style.py's PAGE_CSS
# already defines it, and the widget's legends are built with the same
# charts.legend()/charts.legend_glyphs() the rest of the report uses.
EXPLORER_CSS = """
.sfm-explorer { margin: 4px 0 18px; }
.sfm-explorer .toolbar {
  display: flex; align-items: center; gap: 10px; margin: 6px 0;
  font-size: 12px; color: var(--ink-secondary);
}
.sfm-explorer .toolbar button {
  font: inherit; padding: 4px 10px; border: 1px solid var(--axis);
  background: var(--surface); border-radius: 4px; cursor: pointer;
}
.sfm-explorer .toolbar button:hover { background: var(--gridline); }
.sfm-explorer .sfm-readout { font-variant-numeric: tabular-nums; color: var(--ink-primary); }
.sfm-explorer .sfm-readout b { font-weight: 600; }
.sfm-explorer .canvas-wrap {
  position: relative; border: 1px solid var(--gridline); border-radius: 4px;
  background: var(--surface); overflow: hidden;
}
.sfm-explorer canvas { display: block; width: 100%; }
.sfm-explorer .sfm-overview { cursor: grab; }
.sfm-explorer .sfm-overview.dragging { cursor: grabbing; }
.sfm-explorer .sfm-main { cursor: crosshair; }
.sfm-explorer .sfm-tooltip {
  position: absolute; pointer-events: none; display: none;
  background: var(--ink-primary); color: var(--surface);
  font-size: 11px; padding: 4px 7px; border-radius: 3px;
  white-space: nowrap; z-index: 10; transform: translate(-50%, -100%);
  margin-top: -8px;
}
.sfm-explorer .sfm-hint { color: var(--ink-muted); font-size: 11px; margin-top: 6px; }
"""


def _escape_for_script(payload_json: str) -> str:
    # A title/detail string containing the literal characters "</script"
    # (case-insensitively) could otherwise terminate the script element
    # early, regardless of it sitting inside a JS string literal -- the
    # HTML parser tokenizes the closing tag before JS ever sees it.
    return payload_json.replace("</script", "<\\/script").replace("<!--", "<\\!--")


def explorer_widget_html(payload: Dict[str, Any], *, dom_id: str) -> str:
    """
    Markup for one widget instance: toolbar, overview strip, main canvas,
    tooltip. No `<script>` or payload data here -- `explorer_section`
    collects every widget's payload into one `window.__SFM_EXPLORERS__`
    array and one shared `<script>` for the whole document (see
    render.py, which is what keeps the document capped at one inline
    script regardless of how many runs/widgets it holds).
    """
    meta = payload["meta"]
    main_h = max(220, 30 + len(meta["lanes"]) * 20)
    return f"""<div class="sfm-explorer" id="{dom_id}">
  <p class="note">Drag on the main view to zoom into a range, drag the overview
  strip to pan, scroll to zoom at the cursor.</p>
  <div class="toolbar">
    <button class="sfm-reset" type="button">Reset zoom</button>
    <span class="sfm-readout">&nbsp;</span>
  </div>
  <div class="canvas-wrap">
    <canvas class="sfm-overview" height="46"></canvas>
  </div>
  <div class="canvas-wrap" style="margin-top:8px">
    <canvas class="sfm-main" height="{main_h}"></canvas>
    <div class="sfm-tooltip"></div>
  </div>
  <p class="sfm-hint">Time-of-day and wall-clock use the recording rig's own
  local clock, not the viewer's.</p>
</div>"""


def explorer_bootstrap_js(entries) -> str:
    """
    ``entries`` is a list of ``{"dom_id": str, "payload": dict}``. Returns
    the JS that seeds ``window.__SFM_EXPLORERS__`` and boots one
    ``initSfmExplorer`` instance per entry -- appended after ``EXPLORER_JS``
    to form one section's ``extra_js`` (see explorer_section).
    """
    packed = _escape_for_script(json.dumps(entries, separators=(",", ":")))
    return (
        f"\nwindow.__SFM_EXPLORERS__ = (window.__SFM_EXPLORERS__ || []).concat({packed});\n"
        "(function () { var q = window.__SFM_EXPLORERS__; "
        "for (var i = 0; i < q.length; i++) { "
        "if (q[i].booted) continue; q[i].booted = true; "
        "var root = document.getElementById(q[i].dom_id); "
        "if (root) initSfmExplorer(root, q[i].payload); } })();\n"
    )


EXPLORER_JS = r"""
function initSfmExplorer(root, DATA) {
  "use strict";

  var meta = DATA.meta;
  var lanes = meta.lanes;
  var duration = meta.duration_s;
  var palette = DATA.style.palette;
  var strings = DATA.strings;
  var spans = DATA.spans;   // [lane, t0, t1, key, hatch, openEnded, titleIdx]
  var marks = DATA.marks;   // [lane, t, glyph, key, titleIdx]

  var PAD_LEFT = 130, PAD_RIGHT = 14, PAD_TOP = 8, PAD_BOTTOM = 22;
  var MIN_VIEW_S = 1.0;

  // ---- rig-local time, deliberately not real timezone conversion ----
  // t0_iso is a naive local wall-clock string (no offset) written by the
  // rig itself. Every read below uses Date.UTC()/getUTC*() as a pure
  // calendar-math trick -- nothing here is really UTC. Using the
  // non-UTC Date accessors instead would silently substitute *this
  // browser's* timezone for the rig's, which is exactly the bug this
  // avoids (see sfm_analysis.report.timezones).
  function parseNaiveIso(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?/.exec(iso || "");
    if (!m) return 0;
    var frac = m[7] ? Math.round(parseFloat("0." + m[7]) * 1000) : 0;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], frac);
  }
  var t0Local = parseNaiveIso(meta.t0_iso);

  function wallClockAt(t) {
    return new Date(t0Local + t * 1000);
  }
  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  var MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

  function fmtWallClock(t) {
    var d = wallClockAt(t);
    return MONTHS[d.getUTCMonth()] + " " + d.getUTCDate() + " " +
      pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes()) + ":" + pad2(d.getUTCSeconds());
  }
  function fmtTimeOfDay(t) {
    var d = wallClockAt(t);
    return pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes()) + ":" + pad2(d.getUTCSeconds());
  }
  function fmtRunRelative(t) {
    t = Math.max(0, t);
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = Math.floor(t % 60);
    if (h) return h + "h " + pad2(m) + "m " + pad2(s) + "s";
    if (m) return m + "m " + pad2(s) + "s";
    return s + "s";
  }

  function color(key) {
    var n = palette.length;
    return palette[((key % n) + n) % n];
  }

  // ---- view state ----
  var view = { t0: 0, t1: duration };

  var mainCanvas = root.querySelector(".sfm-main");
  var mainCtx = mainCanvas.getContext("2d");
  var overviewCanvas = root.querySelector(".sfm-overview");
  var overviewCtx = overviewCanvas.getContext("2d");
  var tooltip = root.querySelector(".sfm-tooltip");
  var readout = root.querySelector(".sfm-readout");

  function dpr() { return window.devicePixelRatio || 1; }

  function sizeCanvas(canvas) {
    var rect = canvas.getBoundingClientRect();
    var ratio = dpr();
    var cssH = parseFloat(canvas.getAttribute("height")) || rect.height;
    canvas.width = Math.max(1, Math.round(rect.width * ratio));
    canvas.height = Math.max(1, Math.round(cssH * ratio));
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { w: rect.width, h: cssH };
  }

  function laneGap(h) {
    return (h - PAD_TOP - PAD_BOTTOM) / Math.max(1, lanes.length);
  }

  function xScale(t0, t1, w) {
    var span = Math.max(t1 - t0, 0.001);
    var scale = (w - PAD_LEFT - PAD_RIGHT) / span;
    return {
      toPx: function (t) { return PAD_LEFT + (t - t0) * scale; },
      toT: function (px) { return t0 + (px - PAD_LEFT) / scale; },
    };
  }

  function niceTimeTicks(t0, t1, target) {
    var span = t1 - t0;
    if (span <= 0) return [t0];
    var steps = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400];
    var raw = span / target;
    var step = steps[steps.length - 1];
    for (var i = 0; i < steps.length; i++) { if (steps[i] >= raw) { step = steps[i]; break; } }
    var start = Math.ceil(t0 / step) * step;
    var out = [];
    for (var t = start; t <= t1; t += step) out.push(t);
    return out;
  }

  function drawGlyph(ctx, cx, cy, glyph, col) {
    var r = 4;
    ctx.fillStyle = col;
    ctx.strokeStyle = "#fcfcfb";
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    if (glyph === "circle") {
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fill(); ctx.stroke();
    } else if (glyph === "square") {
      ctx.rect(cx - r, cy - r, 2 * r, 2 * r);
      ctx.fill(); ctx.stroke();
    } else if (glyph === "triangle" || glyph === "tri") {
      ctx.moveTo(cx, cy - r); ctx.lineTo(cx - r, cy + r); ctx.lineTo(cx + r, cy + r); ctx.closePath();
      ctx.fill(); ctx.stroke();
    } else if (glyph === "diamond") {
      ctx.moveTo(cx, cy - r); ctx.lineTo(cx + r, cy); ctx.lineTo(cx, cy + r); ctx.lineTo(cx - r, cy); ctx.closePath();
      ctx.fill(); ctx.stroke();
    } else if (glyph === "cross") {
      ctx.strokeStyle = col; ctx.lineWidth = 2;
      ctx.moveTo(cx - r, cy - r); ctx.lineTo(cx + r, cy + r);
      ctx.moveTo(cx - r, cy + r); ctx.lineTo(cx + r, cy - r);
      ctx.stroke();
    } else {
      // "tick" -- a short vertical hairline
      ctx.strokeStyle = col; ctx.lineWidth = 2;
      ctx.moveTo(cx, cy - r); ctx.lineTo(cx, cy + r);
      ctx.stroke();
    }
  }

  function drawHatch(ctx, x, y, w, h) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, y, w, h);
    ctx.clip();
    ctx.strokeStyle = "rgba(255,255,255,0.6)";
    ctx.lineWidth = 1;
    var step = 5;
    for (var off = -h; off < w; off += step) {
      ctx.beginPath();
      ctx.moveTo(x + off, y + h);
      ctx.lineTo(x + off + h, y);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawFrame(ctx, dims, t0, t1, opts) {
    var w = dims.w, h = dims.h;
    ctx.clearRect(0, 0, w, h);
    var gap = laneGap(h);
    var laneH = opts.laneH != null ? opts.laneH : Math.min(14, gap * 0.75);
    var sc = xScale(t0, t1, w);

    if (opts.showLabels) {
      ctx.font = "10px system-ui, sans-serif";
      ctx.fillStyle = "#52514e";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (var li = 0; li < lanes.length; li++) {
        if (!lanes[li]) continue;
        var ly = PAD_TOP + li * gap + gap / 2;
        ctx.fillText(lanes[li], PAD_LEFT - 6, ly);
      }
    }

    if (opts.showAxis) {
      var ticks = niceTimeTicks(t0, t1, 7);
      ctx.strokeStyle = "#e1e0d9";
      ctx.fillStyle = "#52514e";
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (var ti = 0; ti < ticks.length; ti++) {
        var px = sc.toPx(ticks[ti]);
        ctx.beginPath();
        ctx.moveTo(px, PAD_TOP);
        ctx.lineTo(px, h - PAD_BOTTOM);
        ctx.stroke();
        ctx.fillText(fmtRunRelative(ticks[ti]), px, h - PAD_BOTTOM + 4);
      }
    }

    ctx.strokeStyle = "#c3c2b7";
    ctx.beginPath();
    ctx.moveTo(PAD_LEFT, h - PAD_BOTTOM);
    ctx.lineTo(w - PAD_RIGHT, h - PAD_BOTTOM);
    ctx.stroke();

    for (var si = 0; si < spans.length; si++) {
      var sp = spans[si];
      var lane = sp[0], s0 = sp[1], s1 = sp[2], key = sp[3], hatch = sp[4], openEnded = sp[5];
      if (s1 < t0 || s0 > t1) continue;
      var cs0 = Math.max(s0, t0), cs1 = Math.min(s1, t1);
      var x0 = sc.toPx(cs0), x1 = sc.toPx(cs1);
      var ww = Math.max(1, x1 - x0);
      var sy = PAD_TOP + lane * gap + (gap - laneH) / 2;
      var col = color(key);
      ctx.globalAlpha = hatch ? 1 : 0.55;
      ctx.fillStyle = col;
      ctx.fillRect(x0, sy, ww, laneH);
      ctx.globalAlpha = 1;
      if (hatch) drawHatch(ctx, x0, sy, ww, laneH);
      ctx.strokeStyle = col;
      ctx.lineWidth = 1;
      if (openEnded) ctx.setLineDash([4, 3]);
      ctx.strokeRect(x0, sy, ww, laneH);
      if (openEnded) ctx.setLineDash([]);
    }

    for (var mi = 0; mi < marks.length; mi++) {
      var mk = marks[mi];
      var mlane = mk[0], mt = mk[1], glyph = mk[2], mkey = mk[3];
      if (mt < t0 || mt > t1) continue;
      var mx = sc.toPx(mt);
      var my = PAD_TOP + mlane * gap + gap / 2;
      drawGlyph(ctx, mx, my, glyph, color(mkey));
    }
  }

  function drawMain() {
    var dims = sizeCanvas(mainCanvas);
    drawFrame(mainCtx, dims, view.t0, view.t1, { showLabels: true, showAxis: true });
    if (brush) drawBrush(dims);
  }

  function drawOverview() {
    var dims = sizeCanvas(overviewCanvas);
    drawFrame(overviewCtx, dims, 0, duration, { showLabels: false, showAxis: false, laneH: Math.max(2, laneGap(dims.h) * 0.6) });
    var sc = xScale(0, duration, dims.w);
    var x0 = sc.toPx(view.t0), x1 = sc.toPx(view.t1);
    overviewCtx.fillStyle = "rgba(42,120,214,0.12)";
    overviewCtx.fillRect(x0, 0, Math.max(1, x1 - x0), dims.h);
    overviewCtx.strokeStyle = "#2a78d6";
    overviewCtx.lineWidth = 1.5;
    overviewCtx.strokeRect(x0, 0, Math.max(1, x1 - x0), dims.h);
  }

  function redraw() {
    drawMain();
    drawOverview();
  }

  function clampView(t0, t1) {
    var span = Math.max(t1 - t0, MIN_VIEW_S);
    if (span > duration) span = duration;
    if (t0 < 0) { t0 = 0; t1 = span; }
    if (t1 > duration) { t1 = duration; t0 = duration - span; }
    return { t0: t0, t1: t1 };
  }

  function setView(t0, t1) {
    view = clampView(t0, t1);
    redraw();
    updateReadout(null);
  }

  root.querySelector(".sfm-reset").addEventListener("click", function () {
    setView(0, duration);
  });

  // ---- brush-to-zoom on the main canvas ----
  var brush = null; // {startPx, curPx}
  var dragThresholdPx = 4;

  function mainMouseDown(e) {
    var rect = mainCanvas.getBoundingClientRect();
    var px = e.clientX - rect.left;
    brush = { startPx: px, curPx: px, moved: false };
  }
  function drawBrush(dims) {
    var lo = Math.min(brush.startPx, brush.curPx);
    var hi = Math.max(brush.startPx, brush.curPx);
    mainCtx.fillStyle = "rgba(42,120,214,0.15)";
    mainCtx.fillRect(lo, PAD_TOP, hi - lo, dims.h - PAD_TOP - PAD_BOTTOM);
    mainCtx.strokeStyle = "#2a78d6";
    mainCtx.strokeRect(lo, PAD_TOP, hi - lo, dims.h - PAD_TOP - PAD_BOTTOM);
  }
  function mainMouseMove(e) {
    // Registered on window (not just the canvas) so a brush drag keeps
    // tracking even if the cursor leaves the canvas mid-drag. When NOT
    // dragging, that means this also fires for mouse movement anywhere
    // on the page -- over another widget, the legend, the footer -- so
    // the hover/readout logic below must only run while the cursor is
    // actually within *this* canvas's own bounds, or hovering unrelated
    // page elements (including a sibling widget's canvas) would show a
    // bogus cursor-time readout on this one.
    var rect = mainCanvas.getBoundingClientRect();
    var px = e.clientX - rect.left, py = e.clientY - rect.top;
    if (brush) {
      brush.curPx = px;
      if (Math.abs(brush.curPx - brush.startPx) > dragThresholdPx) brush.moved = true;
      drawMain();
      return;
    }
    if (px < 0 || px > rect.width || py < 0 || py > rect.height) {
      return;
    }
    hover(px, py, e);
  }
  function mainMouseUp() {
    if (!brush) return;
    var sc = xScale(view.t0, view.t1, mainCanvas.getBoundingClientRect().width);
    if (brush.moved) {
      var t0 = sc.toT(Math.min(brush.startPx, brush.curPx));
      var t1 = sc.toT(Math.max(brush.startPx, brush.curPx));
      brush = null;
      setView(t0, t1);
    } else {
      brush = null;
      drawMain();
    }
  }
  function mainMouseLeave() {
    brush = null;
    tooltip.style.display = "none";
    updateReadout(null);
    drawMain();
  }
  mainCanvas.addEventListener("mousedown", mainMouseDown);
  window.addEventListener("mousemove", mainMouseMove);
  window.addEventListener("mouseup", mainMouseUp);
  mainCanvas.addEventListener("mouseleave", mainMouseLeave);

  // wheel: zoom at cursor
  mainCanvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    var rect = mainCanvas.getBoundingClientRect();
    var px = e.clientX - rect.left;
    var sc = xScale(view.t0, view.t1, rect.width);
    var tAtCursor = sc.toT(px);
    var factor = e.deltaY > 0 ? 1.2 : 1 / 1.2;
    var newSpan = Math.max(MIN_VIEW_S, Math.min(duration, (view.t1 - view.t0) * factor));
    var frac = (tAtCursor - view.t0) / Math.max(view.t1 - view.t0, 0.0001);
    var t0 = tAtCursor - frac * newSpan;
    setView(t0, t0 + newSpan);
  }, { passive: false });

  // ---- overview: drag to pan ----
  var panDrag = null;
  overviewCanvas.addEventListener("mousedown", function (e) {
    var rect = overviewCanvas.getBoundingClientRect();
    var sc = xScale(0, duration, rect.width);
    var t = sc.toT(e.clientX - rect.left);
    panDrag = { anchorT: t, startView: { t0: view.t0, t1: view.t1 } };
    overviewCanvas.classList.add("dragging");
  });
  window.addEventListener("mousemove", function (e) {
    if (!panDrag) return;
    var rect = overviewCanvas.getBoundingClientRect();
    var sc = xScale(0, duration, rect.width);
    var t = sc.toT(e.clientX - rect.left);
    var span = panDrag.startView.t1 - panDrag.startView.t0;
    var center = t;
    setView(center - span / 2, center + span / 2);
  });
  window.addEventListener("mouseup", function () {
    if (panDrag) { panDrag = null; overviewCanvas.classList.remove("dragging"); }
  });

  // ---- hover: tooltip + persistent readout ----
  function hover(px, py, e) {
    var rect = mainCanvas.getBoundingClientRect();
    var dims = { w: rect.width, h: parseFloat(mainCanvas.style.height) || rect.height };
    var gap = laneGap(dims.h);
    var laneH = Math.min(14, gap * 0.75);
    var sc = xScale(view.t0, view.t1, dims.w);
    var t = sc.toT(px);
    updateReadout(t);

    var lane = Math.floor((py - PAD_TOP) / gap);
    var best = null, bestDist = 8;

    for (var mi = 0; mi < marks.length; mi++) {
      var mk = marks[mi];
      if (mk[0] !== lane) continue;
      if (mk[1] < view.t0 || mk[1] > view.t1) continue;
      var mx = sc.toPx(mk[1]);
      var d = Math.abs(mx - px);
      if (d < bestDist) { bestDist = d; best = strings[mk[4]]; }
    }
    if (best === null) {
      for (var si = 0; si < spans.length; si++) {
        var sp = spans[si];
        if (sp[0] !== lane) continue;
        if (sp[2] < view.t0 || sp[1] > view.t1) continue;
        if (t >= sp[1] && t <= sp[2]) { best = strings[sp[6]]; break; }
      }
    }

    if (best !== null) {
      tooltip.style.display = "block";
      tooltip.style.left = px + "px";
      tooltip.style.top = py + "px";
      tooltip.textContent = best;
    } else {
      tooltip.style.display = "none";
    }
  }

  function updateReadout(t) {
    if (t === null || t === undefined) {
      readout.innerHTML = "View: " + fmtRunRelative(view.t0) + " – " + fmtRunRelative(view.t1);
      return;
    }
    t = Math.max(0, Math.min(duration, t));
    readout.innerHTML =
      "<b>" + fmtWallClock(t) + "</b> &nbsp;·&nbsp; time-of-day " + fmtTimeOfDay(t) +
      " &nbsp;·&nbsp; +" + fmtRunRelative(t) + " into run";
  }

  window.addEventListener("resize", function () { redraw(); });

  redraw();
  updateReadout(null);
}
"""
