"""
free_feeding.py — Free-feeding (continuous reload) experiment template.

Behavior:
  1. On session start, dispense a pellet on every configured node.
  2. On DomeOpened, log the lift (optional pellet_present from event data).
  3. On PelletTaken, wait ``reload_delay_s`` then re-dispense that node.
  4. On Fault (jam / timeout / pellet lost), the faulted node is **halted**
     (latched) and stops reloading; the other nodes keep free-feeding. The
     node resumes only after an operator **Recover** (``on_recover``
     re-dispenses it).
  5. End after ``duration`` and/or when total `Loaded` milestones reaches
     ``max_pellets``.

Usage::

    from base_station.experiment.templates.free_feeding import build

    exp = build(nodes=[1, 2, 3], reload_delay_s=2.0, hours=12)
    exp.run(interface="vcan0")
"""

from __future__ import annotations

from typing import Optional, Sequence

from .. import kit
from ..runner import Experiment


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "free_feeding",
    reload_delay_s: float = 30.0,
    hours: float = 0.0,
    minutes: float = 0.0,
    seconds: float = 0.0,
    max_pellets: Optional[int] = None,
) -> Experiment:
    """
    Build a free-feeding Experiment.

    Parameters
    ----------
    nodes:
        Node IDs to use. Defaults to [1, 2, 3].
    reload_delay_s:
        Seconds to wait after PelletTaken before re-dispensing.
    hours / minutes / seconds:
        Session duration (combined). 0 = no duration limit.
    max_pellets:
        End when this many pellets reach `Loaded` (None = no cap).
    """
    exp = kit.session(name, nodes, hours=hours, minutes=minutes, seconds=seconds, max_pellets=max_pellets)

    @exp.on_start
    def _start(control):
        control.log("free_feeding_start", nodes=control.nodes, reload_delay_s=reload_delay_s)
        for n in control.nodes:
            control.next_trial()
            control.dispense(n)

    @exp.on_dome_opened
    def _opened(control, event):
        control.incr("dome_openings")
        control.log(
            "dome_opened",
            node=event.node_id,
            pellet_present=event.data.get("pellet_present"),
        )

    @exp.on_pellet_taken
    def _reload(control, event):
        if control.stop_requested:
            return
        node_id = event.node_id
        control.incr("pellets_taken")
        control.log(
            "pellet_taken",
            node=node_id,
            dome_open=event.data.get("dome_open"),
            total=control.counter("pellets_taken"),
        )

        def _do_reload():
            if control.stop_requested:
                return
            control.next_trial()
            control.log("reload_dispense", node=node_id)
            control.dispense(node_id)

        if reload_delay_s <= 0:
            _do_reload()
        else:
            # Node-scoped so a fault on this node cancels its pending reload.
            control.after(reload_delay_s, _do_reload, node=node_id)

    @exp.on_loaded
    def _loaded(control, event):
        # Runner already incr("pellets"); just log for the experiment CSV.
        control.log(
            "loaded",
            node=event.node_id,
            total=control.counter("pellets"),
        )

    @exp.on_fault
    def _fault(control, event):
        """
        Jam/timeout/pellet-lost on a node = no pellet delivered. The runner has
        already halted just this node (cancels its reload, makes its dispenses
        no-ops); the other nodes keep free-feeding. The node stays latched until
        an operator Recover — we only log here.
        """
        fault_code = event.data.get("fault_code")
        control.log("fault", node=event.node_id, fault_code=fault_code)

    @exp.on_recover
    def _recovered(control, event):
        """Operator cleared the fault — resume this node's dispense cycle."""
        control.log("recovered", node=event.node_id)
        control.next_trial()
        control.dispense(event.node_id)

    @exp.on_end
    def _end(control):
        control.log(
            "free_feeding_end",
            pellets=control.counter("pellets"),
            pellets_taken=control.counter("pellets_taken"),
            dome_openings=control.counter("dome_openings"),
            elapsed_s=round(control.elapsed(), 3),
        )

    return exp
