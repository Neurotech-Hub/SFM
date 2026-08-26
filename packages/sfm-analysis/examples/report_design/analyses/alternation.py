"""analyses/alternation.py — starter numbers for the alternation example template.

Counts ``alternation_advance`` rows the template logs (from_node → to_node)
and the take that preceded each switch. Copy this file to
``sfm_analysis/report/analyses/alternation.py``.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Tuple

from ..session import RunData


def switches(run: RunData) -> List[Tuple[int, int, float]]:
    """(from_node, to_node, t) for every alternation_advance row."""
    out: List[Tuple[int, int, float]] = []
    for row in run.exp("alternation_advance"):
        src = row.fields.get("from_node")
        dst = row.fields.get("to_node")
        if src is None or dst is None:
            continue
        out.append((int(src), int(dst), row.t))
    return out


def switch_counts(run: RunData) -> Dict[Tuple[int, int], int]:
    return Counter((a, b) for a, b, _ in switches(run))
