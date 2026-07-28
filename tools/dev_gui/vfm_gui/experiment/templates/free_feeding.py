"""
free_feeding.py — Free-feeding (continuous reload) experiment template.

Behavior:
  1. On session start, dispense a pellet on every configured node.
  2. On DomeOpened, log the lift (optional pellet_present from event data).
  3. On PelletTaken, wait ``reload_delay_s`` then re-dispense that node.
  4. On Fault (jam / timeout / pellet lost), abort all nodes and **stop the
     session** — a fault means delivery failed; do not continue reloading.
  5. End after ``duration`` and/or when total pellets presented reaches
     ``max_pellets``.

Usage::

    from vfm_gui.experiment.templates.free_feeding import build

    exp = build(nodes=[1, 2, 3], reload_delay_s=2.0, hours=12)
    exp.run(interface="vcan0")
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..runner import Experiment


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "free_feeding",
    reload_delay_s: float = 2.0,
    hours: float = 0.0,
    minutes: float = 0.0,
    seconds: float = 0.0,
    max_pellets: Optional[int] = None,
    pulse_bnc_on_dispense: bool = False,
    bnc_pulse_us: int = 100,
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
        End when this many pellets have been presented (None = no cap).
    pulse_bnc_on_dispense:
        If True, pulse BNC OUT whenever a dispense is issued.
    bnc_pulse_us:
        BNC OUT pulse width in microseconds.
    """
    node_list = list(nodes) if nodes else [1, 2, 3]
    exp = Experiment(nodes=node_list, name=name)

    @exp.on_start
    def _start(ctx):
        ctx.log("free_feeding_start", nodes=ctx.nodes, reload_delay_s=reload_delay_s)
        for n in ctx.nodes:
            ctx.dispense(n)
            if pulse_bnc_on_dispense:
                ctx.bnc_pulse(bnc_pulse_us)

    @exp.on_dome_opened
    def _opened(ctx, ev):
        ctx.incr("dome_openings")
        ctx.log(
            "dome_opened",
            node=ev.node_id,
            pellet_present=ev.data.get("pellet_present"),
        )

    @exp.on_pellet_taken
    def _reload(ctx, ev):
        if ctx.stop_requested:
            return
        node_id = ev.node_id
        ctx.incr("pellets_taken")
        ctx.log(
            "pellet_taken",
            node=node_id,
            dome_open=ev.data.get("dome_open"),
            total=ctx.counter("pellets_taken"),
        )

        def _do_reload():
            if ctx.stop_requested:
                return
            ctx.log("reload_dispense", node=node_id)
            ctx.dispense(node_id)
            if pulse_bnc_on_dispense:
                ctx.bnc_pulse(bnc_pulse_us)

        if reload_delay_s <= 0:
            _do_reload()
        else:
            ctx.after(reload_delay_s, _do_reload)

    @exp.on_pellet_presented
    def _presented(ctx, ev):
        # Runner already incr("pellets"); just log for the experiment CSV.
        ctx.log(
            "pellet_presented",
            node=ev.node_id,
            total=ctx.counter("pellets"),
        )

    @exp.on_fault
    def _fault(ctx, ev):
        """Jam/timeout/pellet-lost = delivery failed; halt the free-feeding run."""
        fault_code = ev.data.get("fault_code")
        ctx.log("fault", node=ev.node_id, fault_code=fault_code)
        ctx.broadcast_abort()
        ctx.stop(reason=f"fault_node_{ev.node_id}_{fault_code}")

    @exp.on_end
    def _end(ctx):
        ctx.log(
            "free_feeding_end",
            pellets=ctx.counter("pellets"),
            pellets_taken=ctx.counter("pellets_taken"),
            dome_openings=ctx.counter("dome_openings"),
            elapsed_s=round(ctx.elapsed(), 3),
        )

    exp.end_after(hours=hours, minutes=minutes, seconds=seconds, pellets=max_pellets)
    return exp


# Alias matching the plan's "free_feeding" naming.
def free_feeding(*args, **kwargs) -> Experiment:
    """Alias for :func:`build`."""
    return build(*args, **kwargs)
