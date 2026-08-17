"""sections/fixed_random.py — HTML rendering for fixed_and_random."""

from __future__ import annotations

from typing import Optional

from .. import charts
from ..analyses.fixed_random import observed_random_rate, role_summary
from ..charts import Frame, escape_text
from ..schema import SectionContext, SectionResult
from ..stats import safe_ratio


def role_comparison_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        roles = role_summary(run)
        if not roles:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        rows = "".join(
            f"<tr><td>{r.node}</td><td>{escape_text(r.role)}</td><td>{r.presented}</td>"
            f"<td>{r.taken}</td><td>{_fmt_pct(safe_ratio(r.taken, r.presented))}</td></tr>"
            for r in roles
        )
        parts.append(f"""
        {heading}
        <table>
          <tr><th>Node</th><th>Role</th><th>Presented</th><th>Taken</th><th>Take rate</th></tr>
          {rows}
        </table>
        """)

    if not any_data:
        return SectionResult(section_id="fixed_random.role_comparison", title="Fixed vs Random Nodes", html="", empty=True)

    return SectionResult(section_id="fixed_random.role_comparison", title="Fixed vs Random Nodes", html="".join(parts))


def random_rate_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        fit = observed_random_rate(run)
        if fit is None:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        frame = Frame(h=180)
        x = charts.linear(0, 1, frame.px0, frame.px1)
        y = charts.linear(0, 1, frame.py1, frame.py0)
        axes = charts.axes(frame, x, y, x_ticks=[], y_ticks=[0, 0.5, 1.0], y_label="rate")
        configured_x = x(0.5)
        ref_line = (f'<line x1="{frame.px0:.1f}" y1="{y(fit.configured):.1f}" '
                   f'x2="{frame.px1:.1f}" y2="{y(fit.configured):.1f}" '
                   f'stroke="#898781" stroke-dasharray="3,3"/>'
                   f'<text x="{frame.px1 - 4:.1f}" y="{y(fit.configured) - 4:.1f}" text-anchor="end">'
                   f'configured {fit.configured:.0%}</text>')
        marker = charts._marker_path(configured_x, y(fit.p), "circle", 10, charts.style.color(0),
                                       f"observed {fit.p:.0%} ({fit.k}/{fit.n})")
        errbar = charts.error_bars(frame, x, y, [(0.5, fit.p)], [fit.lo], [fit.hi], key=0)
        chart = charts.svg(frame, axes + ref_line + errbar + marker, title="Observed vs configured random-fire rate")

        parts.append(f"""
        {heading}
        <figure>{chart}<figcaption>Observed {fit.k}/{fit.n} ({fit.p:.0%}, 95% CI {fit.lo:.0%}–{fit.hi:.0%})
        vs configured {fit.configured:.0%}. Denominator is total trial/trigger events — an approximation
        when more than one random-role node is sampled per trigger.</figcaption></figure>
        """)

    if not any_data:
        return SectionResult(section_id="fixed_random.random_rate", title="Random-Fire Rate", html="", empty=True)

    return SectionResult(section_id="fixed_random.random_rate", title="Random-Fire Rate", html="".join(parts))


def _fmt_pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


SECTIONS = {
    "role_comparison": role_comparison_section,
    "random_rate": random_rate_section,
}
