"""charts.py — inline-SVG chart primitives for report generation.

Pure functions: given data, return an SVG (or, for `kpi`, plain HTML)
string. No JavaScript, no external CDN, no <script> tag anywhere in this
module's output — every chart must survive as a static image inside a
single self-contained report.html that a browser can print with Ctrl+P.

Print-first design (see style.py's module docstring for why the document
commits to one light theme): every mark binds color + stroke-dash pattern
+ marker glyph to the same ordinal "key" (style.color/dash/glyph), and
every filled area additionally gets a hatch texture — so a series never
depends on hue alone, which matters most exactly when it's printed in
grayscale.

Values are escaped with escape_text() before entering any SVG string;
test_report_charts.py asserts every primitive's output round-trips
through xml.etree.ElementTree.fromstring, which is what actually catches
an unescaped "&" or "<" in a session name or event detail string.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from . import style

# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

def escape_text(s: object) -> str:
    s = str(s)
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------------
# Frame / Scale / Series
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    w: int = 900
    h: int = 260
    l: int = 60
    r: int = 18
    t: int = 18
    b: int = 38

    @property
    def px0(self) -> float:
        return self.l

    @property
    def px1(self) -> float:
        return self.w - self.r

    @property
    def py0(self) -> float:
        return self.t

    @property
    def py1(self) -> float:
        return self.h - self.b


@dataclass(frozen=True)
class Scale:
    d0: float
    d1: float
    p0: float
    p1: float
    log: bool = False

    def __call__(self, v: float) -> float:
        if self.log:
            lo = math.log10(max(self.d0, 1e-9))
            hi = math.log10(max(self.d1, self.d0 + 1e-9))
            vv = math.log10(max(v, 1e-9))
            span = hi - lo
            frac = 0.0 if span == 0 else (vv - lo) / span
        else:
            span = self.d1 - self.d0
            frac = 0.0 if span == 0 else (v - self.d0) / span
        return self.p0 + frac * (self.p1 - self.p0)

    def invert(self, px: float) -> float:
        span = self.p1 - self.p0
        frac = 0.0 if span == 0 else (px - self.p0) / span
        if self.log:
            lo = math.log10(max(self.d0, 1e-9))
            hi = math.log10(max(self.d1, self.d0 + 1e-9))
            return 10 ** (lo + frac * (hi - lo))
        return self.d0 + frac * (self.d1 - self.d0)


@dataclass(frozen=True)
class Series:
    label: str
    values: Sequence[float]
    key: int = 0


@dataclass(frozen=True)
class Span:
    lane: int
    t0: float
    t1: float
    key: int = 0
    hatch: bool = False
    open_ended: bool = False
    title: str = ""


@dataclass(frozen=True)
class Mark:
    lane: int
    t: float
    glyph: str = "tick"     # tick|tri|circle|cross|diamond|square
    key: int = 0
    title: str = ""


def linear(d0: float, d1: float, p0: float, p1: float, *, log: bool = False) -> Scale:
    if d1 <= d0:
        d1 = d0 + (1.0 if not log else max(d0, 1.0))
    return Scale(d0=d0, d1=d1, p0=p0, p1=p1, log=log)


def nice_ticks(d0: float, d1: float, target: int = 6) -> List[float]:
    """Evenly spaced 'nice' tick values covering [d0, d1]. Never divides by zero."""
    if d1 < d0:
        d0, d1 = d1, d0
    span = d1 - d0
    if span <= 0:
        return [d0]
    raw_step = span / max(1, target)
    magnitude = 10 ** math.floor(math.log10(raw_step)) if raw_step > 0 else 1.0
    for m in (1, 2, 2.5, 5, 10):
        step = m * magnitude
        if step >= raw_step:
            break
    start = math.floor(d0 / step) * step
    ticks = []
    v = start
    guard = 0
    while v <= d1 + step * 0.5 and guard < 1000:
        if v >= d0 - step * 0.5:
            ticks.append(round(v, 10))
        v += step
        guard += 1
    return ticks or [d0, d1]


def _fmt_hms(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def time_ticks(dur_s: float, target: int = 8) -> List[Tuple[float, str]]:
    """Tick positions (seconds) + formatted labels ("0:00", "12:30", "1:04:00")."""
    if dur_s <= 0:
        return [(0.0, "0:00")]
    raw_step = dur_s / max(1, target)
    nice_steps = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]
    step = next((s for s in nice_steps if s >= raw_step), nice_steps[-1])
    out: List[Tuple[float, str]] = []
    t = 0.0
    while t <= dur_s + step * 0.5:
        out.append((t, _fmt_hms(t)))
        t += step
    return out


# ---------------------------------------------------------------------------
# Document-level wrappers
# ---------------------------------------------------------------------------

def svg(frame: Frame, body: str, *, title: str, desc: str = "", cls: str = "chart") -> str:
    title_el = f"<title>{escape_text(title)}</title>"
    desc_el = f"<desc>{escape_text(desc)}</desc>" if desc else ""
    return (
        f'<svg class="{cls}" viewBox="0 0 {frame.w} {frame.h}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'style="width:100%;height:auto;max-width:{frame.w}px">'
        f'{title_el}{desc_el}{body}</svg>'
    )


def defs() -> str:
    """Hatch <pattern> defs — emit exactly once per document, before any chart."""
    return f'<svg width="0" height="0" style="position:absolute"><defs>{style.hatch_defs()}</defs></svg>'


# ---------------------------------------------------------------------------
# Axes
# ---------------------------------------------------------------------------

def axes(
    frame: Frame,
    x: Scale,
    y: Scale,
    *,
    x_ticks: Sequence[Tuple[float, str]],
    y_ticks: Sequence[float],
    x_label: str = "",
    y_label: str = "",
    grid: str = "y",
) -> str:
    parts = []
    if grid in ("y", "xy"):
        for yt in y_ticks:
            py = y(yt)
            parts.append(f'<line class="gridline" x1="{frame.px0:.1f}" y1="{py:.1f}" '
                         f'x2="{frame.px1:.1f}" y2="{py:.1f}"/>')
    if grid in ("x", "xy"):
        for xt, _ in x_ticks:
            px = x(xt)
            parts.append(f'<line class="gridline" x1="{px:.1f}" y1="{frame.py0:.1f}" '
                         f'x2="{px:.1f}" y2="{frame.py1:.1f}"/>')

    parts.append(f'<line class="axis-line" x1="{frame.px0:.1f}" y1="{frame.py1:.1f}" '
                 f'x2="{frame.px1:.1f}" y2="{frame.py1:.1f}"/>')
    parts.append(f'<line class="axis-line" x1="{frame.px0:.1f}" y1="{frame.py0:.1f}" '
                 f'x2="{frame.px0:.1f}" y2="{frame.py1:.1f}"/>')

    for xt, label in x_ticks:
        px = x(xt)
        parts.append(f'<text x="{px:.1f}" y="{frame.py1 + 14:.1f}" text-anchor="middle">'
                     f'{escape_text(label)}</text>')
    for yt in y_ticks:
        py = y(yt)
        parts.append(f'<text x="{frame.px0 - 6:.1f}" y="{py + 3:.1f}" text-anchor="end">'
                     f'{escape_text(_fmt_num(yt))}</text>')

    if x_label:
        parts.append(f'<text x="{(frame.px0 + frame.px1) / 2:.1f}" y="{frame.h - 4:.1f}" '
                     f'text-anchor="middle">{escape_text(x_label)}</text>')
    if y_label:
        cx = 12
        cy = (frame.py0 + frame.py1) / 2
        parts.append(f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" '
                     f'transform="rotate(-90 {cx:.1f} {cy:.1f})">{escape_text(y_label)}</text>')
    return "".join(parts)


def _fmt_num(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:g}"


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def _marker_path(cx: float, cy: float, shape: str, size: float, color: str, title: str = "") -> str:
    t = f"<title>{escape_text(title)}</title>" if title else ""
    r = size / 2
    if shape == "circle":
        return (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{color}" '
                f'stroke="{style.SURFACE}" stroke-width="1.5">{t}</circle>')
    if shape == "square":
        return (f'<rect x="{cx - r:.1f}" y="{cy - r:.1f}" width="{size:.1f}" height="{size:.1f}" '
                f'fill="{color}" stroke="{style.SURFACE}" stroke-width="1.5">{t}</rect>')
    if shape == "triangle" or shape == "tri":
        pts = f"{cx:.1f},{cy - r:.1f} {cx - r:.1f},{cy + r:.1f} {cx + r:.1f},{cy + r:.1f}"
        return f'<polygon points="{pts}" fill="{color}" stroke="{style.SURFACE}" stroke-width="1.5">{t}</polygon>'
    if shape == "diamond":
        pts = f"{cx:.1f},{cy - r:.1f} {cx + r:.1f},{cy:.1f} {cx:.1f},{cy + r:.1f} {cx - r:.1f},{cy:.1f}"
        return f'<polygon points="{pts}" fill="{color}" stroke="{style.SURFACE}" stroke-width="1.5">{t}</polygon>'
    if shape == "cross":
        return (f'<g stroke="{color}" stroke-width="2">'
                f'<line x1="{cx - r:.1f}" y1="{cy - r:.1f}" x2="{cx + r:.1f}" y2="{cy + r:.1f}"/>'
                f'<line x1="{cx - r:.1f}" y1="{cy + r:.1f}" x2="{cx + r:.1f}" y2="{cy - r:.1f}"/>{t}</g>')
    # "tick" — a short vertical hairline, used on the raster
    return f'<line x1="{cx:.1f}" y1="{cy - r:.1f}" x2="{cx:.1f}" y2="{cy + r:.1f}" stroke="{color}" stroke-width="2">{t}</line>'


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------

def bars(
    frame: Frame,
    x: Scale,
    y: Scale,
    cats: Sequence[str],
    series: Sequence[Series],
    *,
    horizontal: bool = False,
    stacked: bool = False,
    value_labels: bool = True,
) -> str:
    """Grouped or stacked bars, one group per category in `cats`."""
    if not cats or not series:
        return _empty_state("no data")

    n_groups = len(cats)
    n_series = len(series)
    band = (frame.px1 - frame.px0) / max(1, n_groups) if not horizontal else (frame.py1 - frame.py0) / max(1, n_groups)
    gap = band * 0.18
    slot = band - gap
    bar_w = slot / (n_series if not stacked else 1)
    bar_w = min(bar_w, 24)

    parts = []
    for gi, cat in enumerate(cats):
        group_start = frame.px0 + gi * band + gap / 2 if not horizontal else frame.py0 + gi * band + gap / 2
        stack_base = 0.0
        for si, s in enumerate(series):
            v = s.values[gi] if gi < len(s.values) else None
            if v is None:
                continue
            col = style.color(s.key)
            if not horizontal:
                bx = group_start if stacked else group_start + si * bar_w
                y0 = y(stack_base if stacked else 0)
                y1 = y(stack_base + v if stacked else v)
                yy = min(y0, y1)
                hh = abs(y1 - y0)
                parts.append(f'<rect x="{bx:.1f}" y="{yy:.1f}" width="{bar_w:.1f}" height="{hh:.1f}" '
                             f'rx="3" fill="{col}"><title>{escape_text(s.label)}: {_fmt_num(v)}</title></rect>')
                if value_labels and not stacked:
                    parts.append(f'<text class="value-label" x="{bx + bar_w / 2:.1f}" y="{yy - 3:.1f}" '
                                 f'text-anchor="middle">{escape_text(_fmt_num(v))}</text>')
            else:
                by = group_start if stacked else group_start + si * bar_w
                x0 = x(stack_base if stacked else 0)
                x1 = x(stack_base + v if stacked else v)
                xx = min(x0, x1)
                ww = abs(x1 - x0)
                parts.append(f'<rect x="{xx:.1f}" y="{by:.1f}" width="{ww:.1f}" height="{bar_w:.1f}" '
                             f'rx="3" fill="{col}"><title>{escape_text(s.label)}: {_fmt_num(v)}</title></rect>')
                if value_labels and not stacked:
                    parts.append(f'<text class="value-label" x="{x1 + 4:.1f}" y="{by + bar_w / 2 + 3:.1f}">'
                                 f'{escape_text(_fmt_num(v))}</text>')
            stack_base += v
        label_pos = group_start + slot / 2
        if not horizontal:
            parts.append(f'<text x="{label_pos:.1f}" y="{frame.py1 + 14:.1f}" text-anchor="middle">'
                         f'{escape_text(cat)}</text>')
        else:
            parts.append(f'<text x="{frame.px0 - 6:.1f}" y="{label_pos + 3:.1f}" text-anchor="end">'
                         f'{escape_text(cat)}</text>')

    baseline = frame.py1 if not horizontal else frame.px0
    axis_attr = (f'x1="{frame.px0:.1f}" y1="{baseline:.1f}" x2="{frame.px1:.1f}" y2="{baseline:.1f}"'
                 if not horizontal else
                 f'x1="{baseline:.1f}" y1="{frame.py0:.1f}" x2="{baseline:.1f}" y2="{frame.py1:.1f}"')
    parts.insert(0, f'<line class="axis-line" {axis_attr}/>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------

def line(
    frame: Frame,
    x: Scale,
    y: Scale,
    pts: Sequence[Tuple[float, float]],
    *,
    key: int = 0,
    step: bool = False,
    markers: bool = False,
    band: Optional[Sequence[Tuple[float, float, float]]] = None,
) -> str:
    """A single line/step series, optional CI band (list of (t, lo, hi))."""
    if not pts:
        return ""
    col = style.color(key)
    parts = []

    if band:
        top = [(x(t), y(hi)) for t, _lo, hi in band]
        bot = [(x(t), y(lo)) for t, lo, _hi in reversed(band)]
        path = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in top + bot) + " Z"
        parts.append(f'<path d="{path}" fill="{col}" fill-opacity="0.12" stroke="none"/>')

    d_parts = []
    prev = None
    for t, v in pts:
        px, py = x(t), y(v)
        if prev is None:
            d_parts.append(f"M {px:.1f},{py:.1f}")
        elif step:
            ppx, ppy = prev
            d_parts.append(f"L {px:.1f},{ppy:.1f} L {px:.1f},{py:.1f}")
        else:
            d_parts.append(f"L {px:.1f},{py:.1f}")
        prev = (px, py)
    path_d = " ".join(d_parts)
    dash = style.dash(key)
    dash_attr = f' stroke-dasharray="{dash}"' if dash != "none" else ""
    parts.append(f'<path d="{path_d}" fill="none" stroke="{col}" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"{dash_attr}/>')

    if markers:
        glyph = style.glyph(key)
        for t, v in pts:
            parts.append(_marker_path(x(t), y(v), glyph, 8, col))

    return "".join(parts)


def error_bars(
    frame: Frame,
    x: Scale,
    y: Scale,
    pts: Sequence[Tuple[float, float]],
    lo: Sequence[float],
    hi: Sequence[float],
    *,
    key: int = 0,
    cap: float = 4.0,
) -> str:
    col = style.color(key)
    parts = []
    for (t, _v), l, h in zip(pts, lo, hi):
        px, py_lo, py_hi = x(t), y(l), y(h)
        parts.append(f'<line x1="{px:.1f}" y1="{py_lo:.1f}" x2="{px:.1f}" y2="{py_hi:.1f}" '
                     f'stroke="{col}" stroke-width="1.5"/>')
        parts.append(f'<line x1="{px - cap:.1f}" y1="{py_lo:.1f}" x2="{px + cap:.1f}" y2="{py_lo:.1f}" '
                     f'stroke="{col}" stroke-width="1.5"/>')
        parts.append(f'<line x1="{px - cap:.1f}" y1="{py_hi:.1f}" x2="{px + cap:.1f}" y2="{py_hi:.1f}" '
                     f'stroke="{col}" stroke-width="1.5"/>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Timeline: spans + raster
# ---------------------------------------------------------------------------

def spans(frame: Frame, x: Scale, lanes: Sequence[str], items: Sequence[Span], *, lane_h: int = 14) -> str:
    if not lanes:
        return _empty_state("no lanes")
    n = len(lanes)
    lane_gap = (frame.py1 - frame.py0) / n
    parts = []
    for li, label in enumerate(lanes):
        ly = frame.py0 + li * lane_gap + (lane_gap - lane_h) / 2
        parts.append(f'<text x="{frame.px0 - 6:.1f}" y="{ly + lane_h / 2 + 3:.1f}" text-anchor="end">'
                     f'{escape_text(label)}</text>')
    for sp in items:
        if sp.lane >= n:
            continue
        ly = frame.py0 + sp.lane * lane_gap + (lane_gap - lane_h) / 2
        x0, x1 = x(sp.t0), x(sp.t1)
        w = max(1.0, x1 - x0)
        col = style.color(sp.key)
        fill = f"url(#{style.hatch_id(sp.key)})" if sp.hatch else col
        opacity = "" if sp.hatch else ' fill-opacity="0.55"'
        dash_attr = ' stroke-dasharray="4,3"' if sp.open_ended else ""
        t_el = f"<title>{escape_text(sp.title)}</title>" if sp.title else ""
        parts.append(f'<rect x="{x0:.1f}" y="{ly:.1f}" width="{w:.1f}" height="{lane_h}" '
                     f'fill="{fill}"{opacity} stroke="{col}" stroke-width="1"{dash_attr}>{t_el}</rect>')
    return "".join(parts)


def raster(frame: Frame, x: Scale, lanes: Sequence[str], marks: Sequence[Mark], *, lane_h: int = 14) -> str:
    if not lanes:
        return _empty_state("no lanes")
    n = len(lanes)
    lane_gap = (frame.py1 - frame.py0) / n
    parts = []
    for li, label in enumerate(lanes):
        ly = frame.py0 + li * lane_gap + lane_gap / 2
        parts.append(f'<text x="{frame.px0 - 6:.1f}" y="{ly + 3:.1f}" text-anchor="end">'
                     f'{escape_text(label)}</text>')
    for m in marks:
        if m.lane >= n:
            continue
        cy = frame.py0 + m.lane * lane_gap + lane_gap / 2
        col = style.color(m.key)
        parts.append(_marker_path(x(m.t), cy, m.glyph, min(10, lane_h * 0.7), col, m.title))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------

def histogram(frame: Frame, values: Sequence[float], *, bins: Optional[int] = None, key: int = 0, x_label: str = "") -> str:
    values = [v for v in values if v is not None]
    if not values:
        return _empty_state("no data")
    lo, hi = min(values), max(values)
    if bins is None:
        # Freedman-Diaconis, floor 5 / cap 40 bins.
        sv = sorted(values)
        n = len(sv)
        if n >= 4:
            q1 = sv[max(0, n // 4)]
            q3 = sv[min(n - 1, (3 * n) // 4)]
            iqr_ = max(q3 - q1, 1e-9)
            width = 2 * iqr_ * (n ** (-1 / 3))
            bins = int((hi - lo) / width) if width > 0 else 10
        else:
            bins = 5
        bins = max(5, min(40, bins))
    bins = max(1, bins)
    span = max(hi - lo, 1e-9)
    step = span / bins
    counts = [0] * bins
    for v in values:
        idx = min(bins - 1, int((v - lo) / step)) if step > 0 else 0
        counts[idx] += 1

    x = linear(lo, hi, frame.px0, frame.px1)
    y = linear(0, max(counts) or 1, frame.py1, frame.py0)
    parts = [axes(frame, x, y, x_ticks=[(lo + i * step, _fmt_num(lo + i * step)) for i in range(0, bins + 1, max(1, bins // 6))],
                   y_ticks=nice_ticks(0, max(counts) or 1, 4), x_label=x_label, y_label="count")]
    col = style.color(key)
    for i, c in enumerate(counts):
        bx0 = x(lo + i * step)
        bx1 = x(lo + (i + 1) * step)
        by = y(c)
        parts.append(f'<rect x="{bx0:.1f}" y="{by:.1f}" width="{max(1.0, bx1 - bx0 - 1):.1f}" '
                     f'height="{frame.py1 - by:.1f}" fill="{col}" fill-opacity="0.75">'
                     f'<title>{_fmt_num(lo + i * step)}–{_fmt_num(lo + (i + 1) * step)}: {c}</title></rect>')
    return "".join(parts)


def ecdf(frame: Frame, series: Sequence[Series], *, censored: Optional[Sequence[float]] = None,
         x_label: str = "", log_x: bool = False) -> str:
    all_vals = [v for s in series for v in s.values if v is not None]
    if not all_vals:
        return _empty_state("no data")
    lo = min(all_vals) if not log_x else max(1e-3, min(all_vals))
    hi = max(all_vals)
    x = linear(lo, hi, frame.px0, frame.px1, log=log_x)
    y = linear(0, 1, frame.py1, frame.py0)

    ticks = nice_ticks(lo, hi, 6) if not log_x else [lo, hi]
    parts = [axes(frame, x, y, x_ticks=[(t, _fmt_num(t)) for t in ticks],
                   y_ticks=[0, 0.25, 0.5, 0.75, 1.0], x_label=x_label, y_label="cumulative fraction")]
    for s in series:
        vals = sorted(v for v in s.values if v is not None)
        n = len(vals)
        if n == 0:
            continue
        pts = [(vals[0], 0.0)]
        for i, v in enumerate(vals):
            pts.append((v, (i + 1) / n))
        parts.append(line(frame, x, y, pts, key=s.key, step=True))
    return "".join(parts)


def dot_strip(frame: Frame, series: Sequence[Series], *, jitter: float = 0.25, show_median: bool = True) -> str:
    all_vals = [v for s in series for v in s.values if v is not None]
    if not all_vals:
        return _empty_state("no data")
    lo, hi = min(all_vals), max(all_vals)
    x = linear(lo, hi, frame.px0, frame.px1)
    n = max(1, len(series))
    row_h = (frame.py1 - frame.py0) / n

    parts = [axes(frame, x, linear(0, 1, frame.py1, frame.py0),
                   x_ticks=[(t, _fmt_num(t)) for t in nice_ticks(lo, hi, 6)], y_ticks=[], grid="x")]
    for si, s in enumerate(series):
        cy = frame.py0 + si * row_h + row_h / 2
        col = style.color(s.key)
        glyph = style.glyph(s.key)
        vals = [v for v in s.values if v is not None]
        for j, v in enumerate(vals):
            offset = (((j * 37) % 11) / 10 - 0.5) * row_h * jitter * 2
            parts.append(_marker_path(x(v), cy + offset, glyph, 7, col, f"{s.label}: {_fmt_num(v)}"))
        if show_median and vals:
            vals_sorted = sorted(vals)
            med = vals_sorted[len(vals_sorted) // 2]
            parts.append(f'<line x1="{x(med):.1f}" y1="{cy - row_h * 0.35:.1f}" '
                         f'x2="{x(med):.1f}" y2="{cy + row_h * 0.35:.1f}" stroke="{col}" stroke-width="2.5"/>')
        parts.append(f'<text x="{frame.px0 - 6:.1f}" y="{cy + 3:.1f}" text-anchor="end">{escape_text(s.label)}</text>')
    return "".join(parts)


def distribution(frame: Frame, series: Sequence[Series], *, x_label: str = "") -> str:
    """Picks the honest form: dot_strip when any series has < 8 samples, else ecdf."""
    max_n = max((len([v for v in s.values if v is not None]) for s in series), default=0)
    if max_n < 8:
        return dot_strip(frame, series)
    return ecdf(frame, series, x_label=x_label)


def heatmap(frame: Frame, matrix: Sequence[Sequence[Optional[float]]], row_labels: Sequence[str],
            col_labels: Sequence[str], *, vmin: float = 0.0, vmax: float = 1.0, fmt: str = "{:.0%}") -> str:
    if not matrix or not row_labels or not col_labels:
        return _empty_state("no data")
    nrows, ncols = len(row_labels), len(col_labels)
    cell_w = (frame.px1 - frame.px0) / ncols
    cell_h = (frame.py1 - frame.py0) / nrows
    parts = []
    for ri, row_vals in enumerate(matrix):
        parts.append(f'<text x="{frame.px0 - 6:.1f}" y="{frame.py0 + ri * cell_h + cell_h / 2 + 3:.1f}" '
                     f'text-anchor="end">{escape_text(row_labels[ri])}</text>')
        for ci in range(ncols):
            v = row_vals[ci] if ci < len(row_vals) else None
            cx0 = frame.px0 + ci * cell_w
            cy0 = frame.py0 + ri * cell_h
            if v is None:
                fill = style.GRIDLINE
                label = "—"
            else:
                span = max(vmax - vmin, 1e-9)
                t = max(0.0, min(1.0, (v - vmin) / span))
                fill = style.sequential(t)
                label = fmt.format(v)
            parts.append(f'<rect x="{cx0:.1f}" y="{cy0:.1f}" width="{cell_w - 2:.1f}" height="{cell_h - 2:.1f}" '
                         f'fill="{fill}"><title>{escape_text(row_labels[ri])} / {escape_text(col_labels[ci])}: '
                         f'{escape_text(label)}</title></rect>')
            parts.append(f'<text class="value-label" x="{cx0 + cell_w / 2:.1f}" y="{cy0 + cell_h / 2 + 3:.1f}" '
                         f'text-anchor="middle">{escape_text(label)}</text>')
    for ci, label in enumerate(col_labels):
        cx = frame.px0 + ci * cell_w + cell_w / 2
        parts.append(f'<text x="{cx:.1f}" y="{frame.py1 + 14:.1f}" text-anchor="middle">{escape_text(label)}</text>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Small helpers used outside <svg>
# ---------------------------------------------------------------------------

def legend(items: Sequence[Tuple[str, int]], *, inline: bool = True, prefix: str = "") -> str:
    """Color-swatch legend for line/bar/area series. Pass ``prefix`` (e.g.
    "Areas:") to label the group when it sits next to another legend row —
    see ``legend_glyphs()`` for the point-marker counterpart."""
    cells = []
    if prefix:
        cells.append(f'<span class="legend-label">{escape_text(prefix)}</span>')
    for label, key in items:
        cells.append(
            f'<span class="legend-item"><span class="legend-swatch" '
            f'style="background:{style.color(key)}"></span>{escape_text(label)}</span>'
        )
    cls = "legend" if inline else "legend legend-block"
    return f'<div class="{cls}">{"".join(cells)}</div>'


def legend_glyphs(items: Sequence[Tuple[str, str, int]], *, prefix: str = "") -> str:
    """
    Legend for point markers (raster() Marks) — each swatch is the actual
    marker glyph (circle/triangle/diamond/cross/tick), not a plain square, so
    it visually matches what's drawn on the chart. ``items`` is
    ``(label, glyph, key)``.
    """
    cells = []
    if prefix:
        cells.append(f'<span class="legend-label">{escape_text(prefix)}</span>')
    for label, glyph_name, key in items:
        icon = _marker_path(7, 7, glyph_name, 10, style.color(key))
        cells.append(
            f'<span class="legend-item">'
            f'<svg class="legend-glyph" width="14" height="14" viewBox="0 0 14 14">{icon}</svg>'
            f'{escape_text(label)}</span>'
        )
    return f'<div class="legend">{"".join(cells)}</div>'


def sparkline(values: Sequence[float], *, w: int = 90, h: int = 20) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return ""
    frame = Frame(w=w, h=h, l=1, r=1, t=1, b=1)
    x = linear(0, max(1, len(values) - 1), frame.px0, frame.px1)
    y = linear(min(values), max(values), frame.py1, frame.py0)
    pts = list(enumerate(values))
    body = line(frame, x, y, pts, key=0)
    return svg(frame, body, title="trend", cls="chart sparkline")


def kpi(label: str, value: str, sub: str = "", severity: str = "") -> str:
    sev_cls = f" sev-{severity}" if severity else ""
    sub_html = f'<div class="kpi-sub">{escape_text(sub)}</div>' if sub else ""
    return (
        f'<div class="kpi{sev_cls}"><div class="kpi-label">{escape_text(label)}</div>'
        f'<div class="kpi-value">{escape_text(value)}</div>{sub_html}</div>'
    )


def _empty_state(message: str) -> str:
    return f'<text x="50%" y="50%" text-anchor="middle" class="note">{escape_text(message)}</text>'
