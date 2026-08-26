"""
alternation.py — starter event-based template.

Round-robin reload: dispense the first session node at start; after each
PelletTaken, wait ``delay_s`` then dispense the next node in
``control.nodes`` (wrapping around). One node in the roster is a
continuous reload of that node.

Copy into the engine to register it in the GUI (see README.md next to
this file): ``experiments/alternation.json`` +
``base_station/experiment/templates/alternation.py``.
"""

from __future__ import annotations

from typing import Optional, Sequence

from base_station.experiment.kit import log_faults, session
from base_station.experiment.runner import Experiment


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "alternation",
    delay_s: float = 2.0,
    hours: float = 0.0,
    minutes: float = 0.0,
    seconds: float = 0.0,
    max_pellets: Optional[int] = None,
    **_,
) -> Experiment:
    exp = session(name, nodes, hours=hours, minutes=minutes, seconds=seconds,
                  max_pellets=max_pellets)
    log_faults(exp)
    delay = max(0.0, float(delay_s))

    def _next_node(control, after: int) -> int:
        roster = list(control.nodes)
        if after in roster:
            return roster[(roster.index(after) + 1) % len(roster)]
        return roster[0]

    @exp.on_start
    def _start(control):
        control.log("alternation_start", delay_s=delay, nodes=list(control.nodes))
        control.next_trial()
        control.dispense(control.nodes[0])

    @exp.on_pellet_taken
    def _advance(control, event):
        nxt = _next_node(control, event.node_id)

        def _go():
            control.next_trial()
            control.log("alternation_advance",
                        from_node=event.node_id, to_node=nxt)
            control.dispense(nxt)

        if delay <= 0:
            _go()
        else:
            control.after(delay, _go, node=nxt)

    @exp.on_recover
    def _rearm(control, event):
        if event.node_id in control.nodes:
            control.dispense(event.node_id)

    return exp
