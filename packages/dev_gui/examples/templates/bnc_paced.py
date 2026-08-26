"""
bnc_paced.py — starter @exp.script template.

One trial = wait for a BNC IN rising edge, dispense the first session
node, wait for PelletTaken (no timeout — the animal sets the pace),
then a fixed ITI. A fault on that node resolves the take-wait with
``.faulted``; the script logs it and arms the next BNC edge.

Copy into the engine to register it in the GUI (see README.md next to
this file): ``experiments/bnc_paced.json`` +
``base_station/experiment/templates/bnc_paced.py``.
"""

from __future__ import annotations

from typing import Optional, Sequence

from base_station.experiment.kit import log_faults, session
from base_station.experiment.runner import Experiment


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "bnc_paced",
    bnc_channel: int = 0,
    iti_s: float = 1.0,
    hours: float = 0.0,
    minutes: float = 0.0,
    seconds: float = 0.0,
    max_pellets: Optional[int] = None,
    **_,
) -> Experiment:
    exp = session(name, nodes, hours=hours, minutes=minutes, seconds=seconds,
                  max_pellets=max_pellets)
    log_faults(exp)
    channel = int(bnc_channel)
    iti = max(0.0, float(iti_s))

    @exp.script
    def run(control):
        node = control.nodes[0]
        control.log("bnc_paced_start", node=node, bnc_channel=channel, iti_s=iti)

        while True:
            while True:
                edge = yield control.wait_for("bnc_in")
                if not edge.ok or edge.event is None:
                    continue
                data = edge.event.data or {}
                if data.get("edge") != "rising":
                    continue
                if data.get("channel") not in (None, channel):
                    continue
                break

            trial = control.next_trial()
            control.log("bnc_paced_trial", trial=trial, node=node)
            if not control.dispense(node):
                control.log("bnc_paced_skipped", trial=trial, node=node)
                continue

            taken = yield control.wait_for("pellet_taken", node=node)
            if taken.faulted:
                control.log("bnc_paced_faulted", trial=trial,
                            node=taken.faulted_node or node)
                yield control.wait(1.0)
                continue

            if iti > 0:
                yield control.wait(iti)

    return exp
