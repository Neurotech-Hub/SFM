"""sections/free_feeding.py — HTML rendering for free_feeding."""

from __future__ import annotations

from typing import Optional

from .. import charts
from ..analyses.free_feeding import inter_take_intervals, meal_bouts, reload_latencies
from ..charts import Frame, Series, escape_text
from ..schema import SectionContext, SectionResult
from ..stats import median


def reload_latency_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        latencies = reload_latencies(run)
        if not latencies:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        configured = run.params.get("reload_delay_s")

        frame = Frame()
        series = [Series(f"node {n}", vals, key=i) for i, (n, vals) in enumerate(sorted(latencies.items()))]
        chart = charts.svg(frame, charts.distribution(frame, series, x_label="observed reload delay (s)"),
                            title="Reload delay: taken -> reload_dispense")
        legend = charts.legend([(s.label, s.key) for s in series]) if len(series) > 1 else ""
        configured_note = f" (configured: {configured}s)" if configured is not None else ""
        parts.append(f"""
        {heading}
        <figure>{chart}{legend}<figcaption>Time from Pellet Taken to the next reload_dispense on that
        node{configured_note}. Systematic drift from the configured value means the scheduler is
        slipping, not the animal.</figcaption></figure>
        """)

    if not any_data:
        return SectionResult(section_id="free_feeding.reload_latency", title="Reload Latency", html="", empty=True)

    return SectionResult(section_id="free_feeding.reload_latency", title="Reload Latency", html="".join(parts))


def meal_bouts_section(ctx: SectionContext) -> Optional[SectionResult]:
    gap_s = float(ctx.opts.get("meal_gap_s", 300))
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        meals_by_node = meal_bouts(run, gap_s=gap_s)
        if not meals_by_node:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        rows = []
        for node, meals in sorted(meals_by_node.items()):
            n_meals = len(meals)
            avg_pellets = sum(mm.pellets for mm in meals) / n_meals if n_meals else 0
            durs = [mm.duration for mm in meals]
            rows.append(f"<tr><td>{node}</td><td>{n_meals}</td><td>{avg_pellets:.1f}</td>"
                        f"<td>{_fmt_duration(median(durs))}</td></tr>")

        intervals = inter_take_intervals(run)
        all_gaps = [g for vals in intervals.values() for g in vals]
        gap_note = ""
        if len(all_gaps) >= 8:
            frame = Frame(h=200)
            chart = charts.svg(frame, charts.ecdf(frame, [Series("inter-take interval", all_gaps, key=0)],
                                                   x_label="seconds", log_x=True),
                               title="Inter-take interval ECDF")
            gap_note = f'<figure>{chart}<figcaption>Log-scale ECDF of gaps between consecutive takes ' \
                       f'(any node), for judging the {gap_s:.0f}s meal-gap threshold used above.</figcaption></figure>'

        parts.append(f"""
        {heading}
        <table>
          <tr><th>Node</th><th>Meals</th><th>Pellets/meal</th><th>Median meal duration</th></tr>
          {"".join(rows)}
        </table>
        {gap_note}
        """)

    if not any_data:
        return SectionResult(section_id="free_feeding.meal_bouts", title="Meal-Bout Analysis", html="", empty=True)

    return SectionResult(section_id="free_feeding.meal_bouts", title="Meal-Bout Analysis", html="".join(parts))


def _fmt_duration(seconds):
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


SECTIONS = {
    "reload_latency": reload_latency_section,
    "meal_bouts": meal_bouts_section,
}
