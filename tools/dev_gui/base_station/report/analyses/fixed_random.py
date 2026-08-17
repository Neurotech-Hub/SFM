"""analyses/fixed_random.py — fixed_and_random: per-node role summary and
observed vs configured random-dispense rate.

``fixed_and_random_start`` logs ``roles`` as a dict keyed by *string* node
id (a JSON-object constraint: ``{"1": "fixed", "2": "random"}``), and
``random_prob``. Each realized random pick logs a ``random_dispense`` row
whose node is on the CSV row's own ``node_id`` column (fields_json is
empty for this event in real logs, matching probability_pick).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..session import RunData
from ..stats import wilson_ci


def _roles(run: RunData) -> Dict[int, str]:
    raw = run.params.get("roles") or {}
    return {int(k): v for k, v in raw.items()}


@dataclass
class RoleSummary:
    node: int
    role: str
    presented: int
    taken: int


def role_summary(run: RunData) -> List[RoleSummary]:
    roles = _roles(run)
    presented: Dict[int, int] = {}
    taken: Dict[int, int] = {}
    for row in run.by_event("Loaded"):
        presented[row.node_id] = presented.get(row.node_id, 0) + 1
    for row in run.by_event("Pellet Taken"):
        taken[row.node_id] = taken.get(row.node_id, 0) + 1
    return [
        RoleSummary(node=n, role=role, presented=presented.get(n, 0), taken=taken.get(n, 0))
        for n, role in sorted(roles.items())
    ]


@dataclass
class RandomRateFit:
    configured: float
    k: int
    n: int
    p: float
    lo: float
    hi: float


def observed_random_rate(run: RunData) -> Optional[RandomRateFit]:
    """
    Observed vs configured firing rate on random-role nodes.

    Denominator is the count of ``trial`` rows (one per trigger/decision
    point the template advances on) — an approximation when multiple
    random-role nodes are each sampled independently per trigger, noted
    wherever this is rendered.
    """
    configured = run.params.get("random_prob")
    if configured is None:
        return None
    roles = _roles(run)
    random_nodes = {n for n, r in roles.items() if r == "random"}
    if not random_nodes:
        return None

    k = sum(1 for row in run.exp("random_dispense") if row.node_id in random_nodes)
    n = len(run.exp("trial"))
    if n == 0:
        return None
    p, lo, hi = wilson_ci(k, n)
    return RandomRateFit(configured=float(configured), k=k, n=n, p=p, lo=lo, hi=hi)
