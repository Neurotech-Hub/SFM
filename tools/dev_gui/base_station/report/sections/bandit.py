"""sections/bandit.py — HTML rendering for two_armed_bandit analyses.

Consumes base_station.report.analyses.bandit; adds no new metrics of its
own. Every section here degrades gracefully to a short note instead of a
misleading chart when a run has too few trials to say anything — the
real drive's own two_armed_bandit sessions have as few as 3-5 trials, and
a curve drawn from n=1 is worse than no curve.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .. import charts
from ..analyses.bandit import (
    BanditTrial, block_curve, build_trials, choice_node, reversal_curve,
    rewarded, side_bias, trials_to_criterion, validity_summary,
    win_stay_lose_shift,
)
from ..charts import Frame, Series, escape_text
from ..schema import SectionContext, SectionResult

_MIN_TRIALS_FOR_CURVE = 8


def choice_summary_section(ctx: SectionContext) -> Optional[SectionResult]:
    choice_source = ctx.opts.get("choice_source", "first_visit")
    parts = []
    any_trials = False

    for run, m in zip(ctx.runs, ctx.metrics):
        trials = build_trials(run)
        if not trials:
            continue
        any_trials = True
        vs = validity_summary(trials)
        bias = side_bias(trials, choice_source)

        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        reasons = "".join(f"<li>{escape_text(k)}: {v}</li>" for k, v in sorted(vs.invalid_reasons.items()))
        agreement = "".join(
            f"<tr><td>{escape_text(k.replace('_', ' '))}</td><td>{v * 100:.0f}%</td></tr>"
            for k, v in sorted(vs.choice_agreement.items())
        )

        parts.append(f"""
        {heading}
        <div class="kpi-band">
          {charts.kpi("Trials", str(vs.n_total))}
          {charts.kpi("Valid", str(vs.n_valid))}
          {charts.kpi("Analyzed (choice observed)", str(vs.n_analyzed))}
          {charts.kpi("Side bias (P chose arm)", f"{bias * 100:.0f}%" if bias is not None else "—")}
        </div>
        {"<p class='note'>Invalid trial reasons: <ul>" + reasons + "</ul></p>" if reasons else ""}
        <p class="note">Choice source for every metric below: <b>{escape_text(choice_source)}</b>
        (first_visit = first approach after both arms presented; also compared against
        first_dome and taken as a validity check).</p>
        <table><tr><th>Comparison</th><th>Agreement</th></tr>{agreement}</table>
        """)

    if not any_trials:
        return SectionResult(section_id="bandit.choice_summary", title="Choice Summary", html="", empty=True)

    return SectionResult(section_id="bandit.choice_summary", title="Choice Summary", html="".join(parts))


def block_curve_section(ctx: SectionContext) -> Optional[SectionResult]:
    choice_source = ctx.opts.get("choice_source", "first_visit")
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        trials = build_trials(run)
        points = block_curve(trials, choice_source)
        if not points:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        n_total = sum(p.n for p in points)
        if n_total < _MIN_TRIALS_FOR_CURVE:
            table = "".join(f"<tr><td>{p.block}</td><td>{p.k}/{p.n}</td>"
                            f"<td>{p.lo * 100:.0f}%–{p.hi * 100:.0f}%</td></tr>" for p in points)
            parts.append(f"""{heading}<p class="note">Only {n_total} analyzed trial(s) — too few for a
            reliable curve. Raw per-block counts:</p>
            <table><tr><th>Block</th><th>Chose rich</th><th>95% CI</th></tr>{table}</table>""")
            continue

        frame = Frame()
        x = charts.linear(points[0].block, points[-1].block, frame.px0, frame.px1)
        y = charts.linear(0, 1, frame.py1, frame.py0)
        axes = charts.axes(frame, x, y, x_ticks=[(p.block, str(p.block)) for p in points],
                            y_ticks=[0, 0.25, 0.5, 0.75, 1.0], x_label="block", y_label="P(choose rich)")
        pts = [(p.block, p.p) for p in points]
        line = charts.line(frame, x, y, pts, key=0, markers=True)
        errs = charts.error_bars(frame, x, y, pts, [p.lo for p in points], [p.hi for p in points], key=0)
        chart = charts.svg(frame, axes + errs + line, title="P(choose rich) by block")
        parts.append(f'{heading}<figure>{chart}<figcaption>Wilson 95% CI per block '
                     f'(n={n_total} analyzed trials).</figcaption></figure>')

    if not any_data:
        return SectionResult(section_id="bandit.block_curve", title="Block-wise Choice", html="", empty=True)

    return SectionResult(section_id="bandit.block_curve", title="Block-wise Choice", html="".join(parts))


def reversal_curve_section(ctx: SectionContext) -> Optional[SectionResult]:
    choice_source = ctx.opts.get("choice_source", "first_visit")
    pre = int(ctx.opts.get("pre", 5))
    post = int(ctx.opts.get("post", 15))
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        trials = build_trials(run)
        points = reversal_curve(trials, choice_source, pre=pre, post=post)
        if not points:
            continue
        n_total = sum(p.n for p in points)
        if n_total < _MIN_TRIALS_FOR_CURVE:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        frame = Frame()
        x = charts.linear(-pre, post, frame.px0, frame.px1)
        y = charts.linear(0, 1, frame.py1, frame.py0)
        axes = charts.axes(frame, x, y, x_ticks=[(p.rel, str(p.rel)) for p in points if p.rel % 5 == 0 or p.rel == points[0].rel],
                            y_ticks=[0, 0.25, 0.5, 0.75, 1.0], x_label="trials from block flip", y_label="P(choose new-rich)")
        band = [(p.rel, p.lo, p.hi) for p in points]
        line = charts.line(frame, x, y, [(p.rel, p.p) for p in points], key=0, markers=True, band=band)
        zero_x = x(0)
        marker = f'<line x1="{zero_x:.1f}" y1="{frame.py0:.1f}" x2="{zero_x:.1f}" y2="{frame.py1:.1f}" stroke="#898781" stroke-dasharray="3,3"/>'
        chart = charts.svg(frame, axes + marker + line, title="Reversal-aligned choice curve")

        criteria = [c for c in trials_to_criterion(trials, choice_source, max_post=post) if c is not None]
        crit_note = (f"Median trials-to-criterion (3 consecutive new-rich choices): {sorted(criteria)[len(criteria)//2]}"
                    if criteria else "No flip reached criterion within the window shown.")
        parts.append(f'{heading}<figure>{chart}<figcaption>{escape_text(crit_note)}</figcaption></figure>')

    if not any_data:
        return SectionResult(section_id="bandit.reversal_curve", title="Reversal Learning", html="", empty=True)

    return SectionResult(section_id="bandit.reversal_curve", title="Reversal Learning", html="".join(parts))


def wsls_section(ctx: SectionContext) -> Optional[SectionResult]:
    choice_source = ctx.opts.get("choice_source", "first_visit")
    bridge_invalid = bool(ctx.opts.get("bridge_invalid", False))
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        trials = build_trials(run)
        result = win_stay_lose_shift(trials, choice_source, bridge_invalid=bridge_invalid)
        if result.win_stay_n == 0 and result.lose_shift_n == 0:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        ws_p, ws_lo, ws_hi = result.win_stay
        ls_p, ls_lo, ls_hi = result.lose_shift
        frame = Frame(h=220)
        x = charts.linear(0, 2, frame.px0, frame.px1)
        y = charts.linear(0, 1, frame.py1, frame.py0)
        cats = ["win-stay", "lose-shift"]
        series = [Series("rate", [ws_p, ls_p], key=0)]
        axes = charts.axes(frame, x, y, x_ticks=[], y_ticks=[0, 0.25, 0.5, 0.75, 1.0], y_label="rate")
        bars = charts.bars(frame, x, y, cats, series, value_labels=False)
        pts = [(0.5, ws_p), (1.5, ls_p)]
        errs = charts.error_bars(frame, x, y, pts, [ws_lo, ls_lo], [ws_hi, ls_hi], key=0)
        chart = charts.svg(frame, axes + bars + errs, title="Win-stay / lose-shift")
        parts.append(f"""
        {heading}
        <figure>{chart}<figcaption>win-stay {result.win_stay_k}/{result.win_stay_n},
        lose-shift {result.lose_shift_k}/{result.lose_shift_n} (95% Wilson CI shown).</figcaption></figure>
        """)

    if not any_data:
        return SectionResult(section_id="bandit.wsls", title="Win-Stay / Lose-Shift", html="", empty=True)

    return SectionResult(section_id="bandit.wsls", title="Win-Stay / Lose-Shift", html="".join(parts))


def latency_by_role_section(ctx: SectionContext) -> Optional[SectionResult]:
    """Dome/retrieval latency split by whether the cycle was the fed (baited)
    or empty arm for that trial — directly from Cycle.fed, no trial join needed."""
    parts = []
    any_data = False

    for run, m in zip(ctx.runs, ctx.metrics):
        fed_dome = [c.dome_latency for c in m.cycles if c.fed and c.dome_latency is not None]
        empty_dome = [c.dome_latency for c in m.cycles if not c.fed and c.dome_latency is not None]
        fed_retrieval = [c.retrieval_latency for c in m.cycles if c.fed and c.retrieval_latency is not None]
        if not fed_dome and not empty_dome and not fed_retrieval:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""

        frame = Frame()
        series = [Series("fed arm", fed_dome, key=0), Series("empty arm", empty_dome, key=1)]
        chart = charts.svg(frame, charts.distribution(frame, series, x_label="dome latency (s)"),
                            title="Dome latency: fed vs empty arm")
        legend = charts.legend([("fed arm", 0), ("empty arm", 1)])
        parts.append(f"""
        {heading}
        <figure>{chart}{legend}<figcaption>Time from pellet ready to dome open, split by whether the
        cycle was baited (fed) or not (empty) — the empty-arm number is the cost of exploration
        (n={len(fed_dome)} fed, {len(empty_dome)} empty).</figcaption></figure>
        """)

    if not any_data:
        return SectionResult(section_id="bandit.latency_by_role", title="Latency by Arm Role", html="", empty=True)

    return SectionResult(section_id="bandit.latency_by_role", title="Latency by Arm Role", html="".join(parts))


SECTIONS = {
    "choice_summary": choice_summary_section,
    "block_curve": block_curve_section,
    "reversal_curve": reversal_curve_section,
    "wsls": wsls_section,
    "latency_by_role": latency_by_role_section,
}
