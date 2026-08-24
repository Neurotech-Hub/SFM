"""analyses/probability.py — probability_delivery: observed vs configured
per-node delivery distribution.

``probability_delivery_start`` logs ``nodes`` and their per-node ``weights``
(index-aligned, not necessarily summing to 100 — treated as relative
proportions). Each realized delivery decision logs a ``probability_pick``
row whose node is carried on the CSV row's own ``node_id`` column, not in
``fields_json`` (that field is empty for this event in real logs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..session import RunData
from ..stats import chi_square_gof


@dataclass
class NodeDelivery:
    node: int
    weight: float
    observed: int
    expected: float


@dataclass
class DeliveryFit:
    nodes: List[NodeDelivery] = field(default_factory=list)
    chi2: float = 0.0
    df: int = 0
    p_value: float = 1.0
    total: int = 0


def delivery_distribution(run: RunData) -> Optional[DeliveryFit]:
    """None if this run has no usable weights/nodes configuration logged."""
    nodes = run.params.get("nodes") or run.nodes
    weights = run.params.get("weights")
    if not nodes or not weights or len(nodes) != len(weights):
        return None

    counts: Dict[int, int] = {int(n): 0 for n in nodes}
    for row in run.exp("probability_pick"):
        if row.node_id in counts:
            counts[row.node_id] += 1

    total = sum(counts.values())
    total_weight = sum(weights) or 1.0

    node_list, observed_list, expected_list = [], [], []
    for n, w in zip(nodes, weights):
        n = int(n)
        obs = counts.get(n, 0)
        exp = total * (float(w) / total_weight)
        node_list.append(NodeDelivery(node=n, weight=float(w), observed=obs, expected=exp))
        observed_list.append(obs)
        expected_list.append(exp)

    chi2, df, p = chi_square_gof(observed_list, expected_list)
    return DeliveryFit(nodes=node_list, chi2=chi2, df=df, p_value=p, total=total)
