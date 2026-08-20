"""sections/probability.py — HTML rendering for probability_delivery."""

from __future__ import annotations

from typing import Optional

from .. import charts
from ..analyses.probability import delivery_distribution
from ..charts import Frame, Series, escape_text
from ..schema import SectionContext, SectionResult
from ..stats import safe_ratio


def delivery_fit_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        fit = delivery_distribution(run)
        if fit is None or fit.total == 0:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        cats = [f"node {n.node}" for n in fit.nodes]
        series = [
            Series("observed", [n.observed for n in fit.nodes], key=0),
            Series("expected", [n.expected for n in fit.nodes], key=1),
        ]
        frame = Frame(h=220)
        x = charts.linear(0, len(cats), frame.px0, frame.px1)
        y = charts.linear(0, max((n.observed for n in fit.nodes), default=1) or 1, frame.py1, frame.py0)
        chart = charts.svg(frame, charts.bars(frame, x, y, cats, series), title="Observed vs configured delivery share")
        legend = charts.legend([("observed", 0), ("expected", 1)])

        sig = "significantly different from configured" if fit.p_value < 0.05 else "consistent with configured"
        parts.append(f"""
        {heading}
        <figure><figcaption>&chi;&sup2;={fit.chi2:.2f}, df={fit.df}, p={fit.p_value:.3f}
        &mdash; observed distribution is {sig} (n={fit.total} deliveries).</figcaption>{legend}{chart}</figure>
        """)

    if not any_data:
        return SectionResult(section_id="probability.delivery_fit", title="Delivery Distribution", html="", empty=True)

    return SectionResult(section_id="probability.delivery_fit", title="Delivery Distribution", html="".join(parts))


def matching_law_section(ctx: SectionContext) -> Optional[SectionResult]:
    """Visit share (presence bouts) vs realized reward share per node — the
    behavioral question of whether the animal's checking tracks payoff,
    independent of the delivery mechanism's own fit above."""
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        bouts, _ = m.presence
        if not bouts:
            continue
        visits: dict = {}
        for b in bouts:
            visits[b.node] = visits.get(b.node, 0) + 1
        taken = {n: a.taken_total for n, a in m.pellets.items()}
        nodes = sorted(set(visits) | set(taken))
        if len(nodes) < 2:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        total_visits = sum(visits.get(n, 0) for n in nodes) or 1
        total_taken = sum(taken.get(n, 0) for n in nodes) or 1
        rows = "".join(
            f"<tr><td>{n}</td><td>{_fmt_pct(safe_ratio(visits.get(n, 0), total_visits))}</td>"
            f"<td>{_fmt_pct(safe_ratio(taken.get(n, 0), total_taken))}</td></tr>"
            for n in nodes
        )
        parts.append(f"""
        {heading}
        <table>
          <tr><th>Node</th><th>Visit share</th><th>Reward share</th></tr>
          {rows}
        </table>
        <p class="note">Matching law: if visit share tracks reward share, the animal is allocating checking
        effort in proportion to payoff.</p>
        """)

    if not any_data:
        return SectionResult(section_id="probability.matching_law", title="Matching Law", html="", empty=True)

    return SectionResult(section_id="probability.matching_law", title="Matching Law", html="".join(parts))


def _fmt_pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


SECTIONS = {
    "delivery_fit": delivery_fit_section,
    "matching_law": matching_law_section,
}
