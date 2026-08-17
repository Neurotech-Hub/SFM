"""style.py — palette, CSS, and hatch <defs> shared by every report page.

The report is a static, self-contained, print-first document (Ctrl+P is
the primary "export" path — see report/__init__.py), so this deliberately
commits to a single light theme rather than following a light/dark toggle
contract: there is no viewer chrome to theme, and print always renders on
white regardless of the browser's color scheme.

Colors are the validated 8-slot categorical palette (dataviz skill,
references/palette.md) — chosen for its worst-case adjacent CVD separation
(ΔE >= 8) and normal-vision separation (ΔE >= 15) in light mode. Every
series additionally carries a distinct stroke-dash pattern and marker
glyph, and every area fill a distinct hatch angle, so identity survives
grayscale printing even where hue alone would not (see PALETTE / DASH /
GLYPH / HATCH_ANGLE below — charts.py binds all four to the same "key").
"""

from __future__ import annotations

from typing import List

# ---------------------------------------------------------------------------
# Palette (dataviz skill reference palette, light mode)
# ---------------------------------------------------------------------------

SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
AXIS = "#c3c2b7"

STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_SERIOUS = "#ec835a"
STATUS_CRITICAL = "#d03b3b"

# 8-slot categorical palette, fixed order — never cycle/reassign by rank.
PALETTE: List[str] = [
    "#2a78d6",  # 0 blue
    "#eb6834",  # 1 orange
    "#1baf7a",  # 2 aqua
    "#eda100",  # 3 yellow
    "#e87ba4",  # 4 magenta
    "#008300",  # 5 green
    "#4a3aa7",  # 6 violet
    "#e34948",  # 7 red
]

# Sequential single-hue ramp (blue), light -> dark, for heatmaps.
SEQUENTIAL_BLUE = [
    "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b",
]

# Per-key stroke-dash patterns (SVG stroke-dasharray), so a series is never
# identified by hue alone — critical for a document meant to be printed.
DASH: List[str] = ["none", "6,3", "2,2", "8,2,2,2", "10,4", "3,3,1,3", "12,3", "1,3"]

# Per-key marker glyph name, consumed by charts._marker().
GLYPH: List[str] = ["circle", "triangle", "square", "diamond", "cross", "circle", "triangle", "square"]

# Per-key hatch rotation (degrees) for area/span fills — 45/135 mirror pair
# per the dataviz skill's texture guidance, cycled for >2 keys.
HATCH_ANGLE: List[int] = [45, 135, 45, 135, 45, 135, 45, 135]


def color(key: int) -> str:
    return PALETTE[key % len(PALETTE)]


def dash(key: int) -> str:
    return DASH[key % len(DASH)]


def glyph(key: int) -> str:
    return GLYPH[key % len(GLYPH)]


def hatch_id(key: int) -> str:
    return f"hatch-{key % len(HATCH_ANGLE)}"


def sequential(t: float) -> str:
    """Map t in [0, 1] to a step of the sequential blue ramp."""
    t = max(0.0, min(1.0, t))
    idx = round(t * (len(SEQUENTIAL_BLUE) - 1))
    return SEQUENTIAL_BLUE[idx]


# ---------------------------------------------------------------------------
# Hatch pattern <defs>, emitted once per document by charts.defs()
# ---------------------------------------------------------------------------

def hatch_defs() -> str:
    parts = []
    for key, angle in enumerate(HATCH_ANGLE):
        col = color(key)
        parts.append(
            f'<pattern id="{hatch_id(key)}" width="6" height="6" '
            f'patternUnits="userSpaceOnUse" patternTransform="rotate({angle})">'
            f'<rect width="6" height="6" fill="{SURFACE}"/>'
            f'<line x1="0" y1="0" x2="0" y2="6" stroke="{col}" stroke-width="1.4" opacity="0.55"/>'
            f'</pattern>'
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Document-level CSS
# ---------------------------------------------------------------------------

PAGE_CSS = f"""
:root {{
  --surface: {SURFACE};
  --page: {PAGE};
  --ink-primary: {INK_PRIMARY};
  --ink-secondary: {INK_SECONDARY};
  --ink-muted: {INK_MUTED};
  --gridline: {GRIDLINE};
  --axis: {AXIS};
  --status-good: {STATUS_GOOD};
  --status-warning: {STATUS_WARNING};
  --status-serious: {STATUS_SERIOUS};
  --status-critical: {STATUS_CRITICAL};
}}

* {{ box-sizing: border-box; }}

body {{
  background: var(--page);
  color: var(--ink-primary);
  font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0;
  padding: 24px;
}}

.report {{
  max-width: 1000px;
  margin: 0 auto;
  background: var(--surface);
  padding: 24px 32px 48px;
}}

h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 16px; margin: 28px 0 10px; page-break-after: avoid; border-bottom: 1px solid var(--gridline); padding-bottom: 4px; }}
h3 {{ font-size: 13px; margin: 16px 0 6px; page-break-after: avoid; color: var(--ink-secondary); }}
p {{ margin: 4px 0; }}

table {{ border-collapse: collapse; width: 100%; font-size: 12px; margin: 8px 0 16px; }}
th, td {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--gridline); font-variant-numeric: tabular-nums; }}
th {{ color: var(--ink-secondary); font-weight: 600; white-space: nowrap; }}
tbody tr:hover {{ background: var(--page); }}

.kpi-band {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 12px 0 20px; }}
.kpi {{ flex: 1 1 140px; border: 1px solid var(--gridline); border-radius: 6px; padding: 10px 12px; min-width: 120px; }}
.kpi .kpi-label {{ font-size: 11px; color: var(--ink-secondary); }}
.kpi .kpi-value {{ font-size: 22px; font-weight: 600; margin-top: 2px; }}
.kpi .kpi-sub {{ font-size: 11px; color: var(--ink-muted); margin-top: 2px; }}
.kpi.sev-warning {{ border-color: var(--status-warning); }}
.kpi.sev-serious {{ border-color: var(--status-serious); }}
.kpi.sev-critical {{ border-color: var(--status-critical); }}

.banner {{ border-left: 4px solid var(--status-warning); background: #fff8ea; padding: 8px 12px; margin: 10px 0; font-size: 12px; }}
.banner.critical {{ border-left-color: var(--status-critical); background: #fdecea; }}
.note {{ color: var(--ink-secondary); font-size: 11px; margin: 4px 0; }}

.section {{ page-break-inside: avoid; margin-bottom: 8px; }}
figure {{ margin: 8px 0 18px; page-break-inside: avoid; }}
figcaption {{ font-size: 11px; color: var(--ink-secondary); margin-top: 4px; }}

.chart {{ width: 100%; height: auto; display: block; }}
.chart text {{ fill: var(--ink-secondary); font-size: 10px; }}
.chart .axis-line {{ stroke: var(--axis); stroke-width: 1; }}
.chart .gridline {{ stroke: var(--gridline); stroke-width: 1; }}
.chart .value-label {{ fill: var(--ink-primary); font-size: 10px; }}
.chart .node-divider {{ stroke: var(--axis); stroke-width: 1; stroke-dasharray: 2,2; }}

.legend {{ display: flex; flex-wrap: wrap; align-items: center; gap: 12px; font-size: 11px; color: var(--ink-secondary); margin: 4px 0 10px; }}
.legend-item {{ display: inline-flex; align-items: center; gap: 5px; }}
.legend-swatch {{ width: 12px; height: 12px; border-radius: 2px; display: inline-block; }}
.legend-label {{ font-weight: 600; color: var(--ink-muted); }}
.legend-glyph {{ display: inline-block; vertical-align: middle; flex-shrink: 0; }}

details.error {{ border: 1px solid var(--status-critical); border-radius: 6px; padding: 8px 12px; margin: 8px 0; }}
details.error summary {{ color: var(--status-critical); font-weight: 600; cursor: pointer; }}
details.error pre {{ font-size: 11px; overflow-x: auto; white-space: pre-wrap; }}

footer {{ margin-top: 32px; font-size: 10px; color: var(--ink-muted); border-top: 1px solid var(--gridline); padding-top: 8px; }}

@media print {{
  body {{ background: var(--surface); padding: 0; }}
  .report {{ max-width: none; padding: 0; }}
  .section, figure, table {{ page-break-inside: avoid; }}
  h2 {{ page-break-after: avoid; }}
  .no-print {{ display: none; }}
}}

@page {{ size: A4 portrait; margin: 12mm; }}
"""
