"""
free_feeding.py — Free-feeding (continuous reload) experiment template.

Behavior:
  1. On session start, dispense a pellet on every configured node.
  2. On PelletTaken, wait the shared next-trial advance (``fixed_delay`` or
     ``presence_clear``) then re-dispense that node.
  3. On Fault (jam / timeout / pellet lost), the whole session pauses —
     no node reloads — until an operator **Recover**. The faulted node is
     **halted** (latched); ``on_recover`` re-dispenses it once every
     session node is healthy again.
  4. End after ``duration`` and/or when total `Loaded` milestones reaches
     ``max_pellets``.

CAN EVENT rows already carry Loaded / Dome Opened / Pellet Taken with
behavioral names — this template does not re-log those as EXPERIMENT rows
(one line per event). It only logs session lifecycle, reload decisions, and
fault/recover.

Usage::

    from base_station.experiment.templates.free_feeding import build

    exp = build(nodes=[1, 2, 3], next_trial_wait="fixed_delay",
                fixed_delay_s=2.0, hours=12)
"""

from __future__ import annotations

from typing import Optional, Sequence

from .. import kit
from ..runner import Experiment


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "free_feeding",
    next_trial_wait: Optional[str] = None,
    fixed_delay_s: Optional[float] = None,
    reload_delay_s: Optional[float] = None,  # legacy alias of fixed_delay_s
    iti_quiet_s: float = 1.0,
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
    next_trial_wait:
        "fixed_delay" (wait ``fixed_delay_s`` after PelletTaken) or
        "presence_clear" (wait until that node's pad is clear). See
        ``kit.after_advance``.
    fixed_delay_s / reload_delay_s:
        Seconds to wait after PelletTaken when mode is ``fixed_delay``.
        ``reload_delay_s`` is the legacy alias (default 30).
    iti_quiet_s:
        When mode is ``presence_clear``, seconds presence must stay clear
        before the reload.
    hours / minutes / seconds:
        Session duration (combined). 0 = no duration limit.
    max_pellets:
        End when this many pellets reach `Loaded` (None = no cap).
    """
    exp = kit.session(name, nodes, hours=hours, minutes=minutes, seconds=seconds, max_pellets=max_pellets)
    advance, delay_s = kit.resolve_advance(
        next_trial_wait=next_trial_wait,
        fixed_delay_s=fixed_delay_s, reload_delay_s=reload_delay_s,
        default_delay_s=30.0,
    )

    @exp.on_start
    def _start(control):
        control.log(
            "free_feeding_start",
            nodes=control.nodes,
            next_trial_wait=advance,
            fixed_delay_s=delay_s,
        )
        for n in control.nodes:
            control.next_trial()
            control.dispense(n)

    @exp.on_dome_opened
    def _opened(control, event):
        control.incr("dome_openings")

    @exp.on_pellet_taken
    def _reload(control, event):
        if control.stop_requested:
            return
        node_id = event.node_id
        control.incr("pellets_taken")

        def _do_reload():
            if control.stop_requested:
                return
            if any(control.is_halted(n) for n in control.nodes):
                return
            control.next_trial()
            control.log("reload_dispense", node=node_id)
            control.dispense(node_id)

        # Node-scoped so a fault on this node cancels its pending reload.
        # session_pause: a fault on ANY session node holds every reload
        # until Recover — the whole rig waits together.
        kit.after_advance(
            control, advance, _do_reload,
            node=node_id, delay_s=delay_s, quiet_s=iti_quiet_s,
            session_pause=True,
        )

    @exp.on_fault
    def _fault(control, event):
        """
        Jam/timeout/pellet-lost on a node = no pellet delivered. The runner
        has already halted just this node (cancels its reload, makes its
        dispenses no-ops). Reloads on every other node also pause until an
        operator Recover — we only log here.
        """
        fault_code = event.data.get("fault_code")
        control.log("fault", node=event.node_id, fault_code=fault_code)

    @exp.on_recover
    def _recovered(control, event):
        """Operator cleared the fault — resume this node's dispense cycle
        once every session node is healthy (session-wide pause)."""
        control.log("recovered", node=event.node_id)
        if any(control.is_halted(n) for n in control.nodes):
            return
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
