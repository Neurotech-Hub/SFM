"""sections/alternation.py — HTML for the alternation starter analysis.

Copy to ``sfm_analysis/report/sections/alternation.py``. A section reads
``ctx.runs`` / ``ctx.metrics`` only — it must not touch the filesystem.
"""

from __future__ import annotations

from typing import Optional

from ..analyses.alternation import switch_counts, switches
from ..charts import escape_text
from ..schema import SectionContext, SectionResult


def switch_table_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    any_data = False
    for run, _m in zip(ctx.runs, ctx.metrics):
        rows = switches(run)
        if not rows:
            continue
        any_data = True
        heading = f"<h3>{escape_text(run.run_label)}</h3>" if len(ctx.runs) > 1 else ""
        counts = switch_counts(run)
        body = "".join(
            f"<tr><td>{a} → {b}</td><td>{n}</td></tr>"
            for (a, b), n in sorted(counts.items())
        )
        parts.append(f"""
        {heading}
        <p>{len(rows)} switch(es) logged as <code>alternation_advance</code>.</p>
        <table>
          <tr><th>From → to</th><th>Count</th></tr>
          {body}
        </table>
        """)

    if not any_data:
        return SectionResult(
            section_id="alternation.switch_table",
            title="Alternation switches",
            html="",
            empty=True,
        )
    return SectionResult(
        section_id="alternation.switch_table",
        title="Alternation switches",
        html="".join(parts),
        summary={"switches": sum(len(switches(r)) for r in ctx.runs)},
    )


SECTIONS = {
    "switch_table": switch_table_section,
}
