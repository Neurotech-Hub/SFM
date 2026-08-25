"""analyses/free_feeding.py — free_feeding: reload latency and meal-bout
clustering.

``free_feeding_start`` logs ``reload_delay_s`` — the configured wait after
a take before the node reloads. The *observed* reload delay (Pellet Taken
-> next reload_dispense on that node) should track it; systematic
deviation means the scheduler is slipping, not a hardware fault.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


def reload_latencies(run) -> Dict[int, List[float]]:
    """Per node: seconds from each Pellet Taken to the next reload_dispense."""
    reloads_by_node: Dict[int, List[float]] = {}
    for row in run.exp("reload_dispense"):
        reloads_by_node.setdefault(row.node_id, []).append(row.t)
    for v in reloads_by_node.values():
        v.sort()

    out: Dict[int, List[float]] = {}
    for row in run.by_event("Pellet Taken"):
        node = row.node_id
        reload_ts = reloads_by_node.get(node, [])
        nxt = next((t for t in reload_ts if t > row.t), None)
        if nxt is not None:
            out.setdefault(node, []).append(nxt - row.t)
    return out


@dataclass
class Meal:
    node: int
    t0: float
    t1: float
    pellets: int

    @property
    def duration(self) -> float:
        return self.t1 - self.t0


def meal_bouts(run, gap_s: float = 300.0) -> Dict[int, List[Meal]]:
    """
    Cluster each node's Pellet Taken events into meals, split wherever the
    gap between consecutive takes exceeds ``gap_s`` (default 5 min).

    The threshold is a modeling choice, not a measured constant — render a
    log-survivor plot of inter-take intervals alongside this (see
    sections/free_feeding.py) so the choice can be justified by eye rather
    than inherited blindly.
    """
    takes_by_node: Dict[int, List[float]] = {}
    for row in run.by_event("Pellet Taken"):
        takes_by_node.setdefault(row.node_id, []).append(row.t)

    out: Dict[int, List[Meal]] = {}
    for node, ts in takes_by_node.items():
        ts = sorted(ts)
        meals: List[Meal] = []
        start = prev = ts[0]
        count = 1
        for t in ts[1:]:
            if t - prev > gap_s:
                meals.append(Meal(node=node, t0=start, t1=prev, pellets=count))
                start, count = t, 0
            count += 1
            prev = t
        meals.append(Meal(node=node, t0=start, t1=prev, pellets=count))
        out[node] = meals
    return out


def inter_take_intervals(run) -> Dict[int, List[float]]:
    """Per-node consecutive-take gaps, for justifying the meal_bouts gap_s threshold."""
    takes_by_node: Dict[int, List[float]] = {}
    for row in run.by_event("Pellet Taken"):
        takes_by_node.setdefault(row.node_id, []).append(row.t)
    out: Dict[int, List[float]] = {}
    for node, ts in takes_by_node.items():
        ts = sorted(ts)
        out[node] = [b - a for a, b in zip(ts, ts[1:])]
    return out
