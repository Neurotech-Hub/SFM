"""
troubleshooting.py — Timed actuator (M2-only) cycles on selected nodes.

Runs the plate motion of a mimic arm with nothing to follow: every
``interval_min`` minutes, each selected node lowers to the drop position,
holds for ``hold_s`` seconds, and raises an empty plate. M1 never turns, so no
pellet is delivered or counted. Use it to check or wear-test the actuators
without an animal or a hopper.

How the raise happens on stock firmware:
  ``DispenseNoFeed`` lowers the plate and then holds in ``Dwelling`` until the
  node hears another node's ``Raising`` event — it has no raise command and no
  timer of its own. After the hold, the base station puts one Raising frame on
  the bus from an id no node uses (``control.trigger_peer_raise()``), and
  every dwelling node raises together. The plate stays up (empty) until the
  next cycle lowers it again.

One cycle:
  1. ``dispense(feed=False)`` every selected node that is not halted.
  2. Wait until each is down (``Dwelling``), halted, or already presented.
  3. Hold ``hold_s`` seconds, then trigger the raise.
  4. Wait until each dwelling node has raised (``NoFeedPresented``).
  5. A node that never got down or never came up without faulting is still
     mid-motion: it is sent Recover and logged as ``actuator_stuck``.
  6. One ``actuator_cycle`` row per node: ``outcome`` (ok / fault / stuck /
     pellet), ``lower_s`` (command → Dwelling), ``raise_s`` (trigger → top).

Cycles start every ``interval_min`` minutes, measured start to start; one
that overruns the interval is followed immediately by the next.

Faults: a faulted node is halted and skipped while the others keep cycling.
After an operator Recover it rejoins on the next cycle. Ends on ``minutes``
and/or ``max_cycles`` (0 = no limit), or Stop.

Usage::

    from base_station.experiment.templates.troubleshooting import build

    exp = build(nodes=[1, 3], interval_min=5.0, hold_s=3.0)
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence

from .. import kit
from ..events import EventKind
from ..runner import Experiment

# Longest a node may take to get down, or back up, before it is called stuck.
# Same mechanical safeguard as the synchronized cycle's raise gate.
MOTION_TIMEOUT_S = kit.PRESENTATION_TIMEOUT_S

# How long the first cycle waits for the selected nodes to report in.
STARTUP_ONLINE_WAIT_S = 20.0


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "troubleshooting",
    interval_min: float = 5.0,
    hold_s: float = 3.0,
    max_cycles: int = 0,
    hours: float = 0.0,
    minutes: float = 0.0,
    seconds: float = 0.0,
) -> Experiment:
    """
    Build a Troubleshooting (actuator-only) Experiment.

    Parameters
    ----------
    nodes:
        Nodes to cycle. All of them move together each cycle.
    interval_min:
        Minutes from the start of one cycle to the start of the next.
    hold_s:
        Seconds the plate holds at the drop position before raising —
        roughly a fed node's M1 load time. 0 = raise as soon as every node
        is down.
    max_cycles:
        Stop after this many cycles. 0 = no cap.
    hours / minutes / seconds:
        Session duration. All zero = run until Stop.
    """
    interval_s = max(0.0, float(interval_min)) * 60.0
    hold_s = max(0.0, float(hold_s))
    max_cycles = max(0, int(max_cycles or 0))

    exp = kit.session(name, nodes, hours=hours, minutes=minutes, seconds=seconds)
    node_list = tuple(int(n) for n in exp.nodes)
    kit.log_faults(exp)
    kit.log_feed_skipped(exp)

    # Elapsed-time marks for the cycle in flight, keyed by node.
    cycle: Dict[str, Dict[int, float]] = {"sent": {}, "down": {}, "up": {}}

    @exp.on(EventKind.DWELLING)
    def _down(control, event) -> None:
        if event.node_id in cycle["sent"]:
            cycle["down"].setdefault(event.node_id, control.elapsed())

    @exp.on_no_feed_presented
    def _up(control, event) -> None:
        if event.node_id in cycle["sent"]:
            cycle["up"].setdefault(event.node_id, control.elapsed())

    @exp.on_start
    def _start(control) -> None:
        control.log(
            "troubleshooting_start",
            nodes=list(node_list), interval_min=interval_s / 60.0,
            hold_s=hold_s, max_cycles=max_cycles,
        )

    @exp.on_end
    def _end(control) -> None:
        control.log(
            "troubleshooting_end",
            cycles=control.counter("cycles"),
            elapsed_s=round(control.elapsed(), 3),
        )

    def _is_down(c, n: int) -> bool:
        return n in cycle["down"] or c.is_halted(n) or c.presentation_done(n)

    def _run_cycle(control, active: Sequence[int]):
        trial = control.next_trial()
        control.incr("cycles")
        for marks in cycle.values():
            marks.clear()
        for n in active:
            control.clear_presentation(n)
            if control.dispense(n, feed=False):
                cycle["sent"][n] = control.elapsed()
        commanded = tuple(cycle["sent"])
        if not commanded:
            control.log("actuator_cycle_skipped", cycle=trial, reason="no_node_accepted")
            return

        yield control.wait_until(
            lambda c: all(_is_down(c, n) for n in commanded),
            timeout=MOTION_TIMEOUT_S,
            label="actuator_down",
        )
        if hold_s > 0 and any(n in cycle["down"] for n in commanded):
            yield control.wait(hold_s)

        raising = tuple(
            n for n in commanded
            if n in cycle["down"] and not control.is_halted(n)
        )
        raise_at = None
        if raising:
            raise_at = control.elapsed()
            control.trigger_peer_raise()
            yield control.wait_until(
                lambda c: all(c.presentation_done(n) or c.is_halted(n) for n in raising),
                timeout=MOTION_TIMEOUT_S,
                label="actuator_up",
            )

        for n in commanded:
            sent = cycle["sent"][n]
            down = cycle["down"].get(n)
            up = cycle["up"].get(n)
            if control.is_halted(n):
                outcome = "fault"
            elif control.presented_pellet(n):
                outcome = "pellet"  # plate was occupied: firmware presented it
            elif control.presentation_done(n):
                outcome = "ok"
            else:
                # Neither faulted nor finished: still mid-motion. Stop it so
                # the next cycle starts from a clean Idle.
                outcome = "stuck"
                control.log(
                    "actuator_stuck", node=n, warning=1, cycle=trial,
                    phase="lowering" if down is None else "raising",
                )
                control.recover(n)
            control.log(
                "actuator_cycle", node=n, cycle=trial, outcome=outcome,
                lower_s=round(down - sent, 3) if down is not None else None,
                raise_s=(
                    round(up - raise_at, 3)
                    if up is not None and raise_at is not None else None
                ),
            )

    @exp.script
    def run(control):
        online = yield control.wait_until(
            lambda c: all(c.is_online(n) for n in node_list),
            timeout=STARTUP_ONLINE_WAIT_S,
            label="nodes_online",
        )
        if not online.ok:
            control.log(
                "troubleshooting_startup_nodes_offline",
                nodes=[n for n in node_list if not control.is_online(n)],
            )

        while True:
            started = control.elapsed()
            active = [n for n in node_list if not control.is_halted(n)]
            halted = [n for n in node_list if control.is_halted(n)]
            if halted:
                control.log("actuator_nodes_skipped", nodes=halted, reason="halted")
            if active:
                yield from _run_cycle(control, active)
            else:
                control.log("actuator_cycle_skipped", reason="all_halted")
            if max_cycles and control.counter("cycles") >= max_cycles:
                control.stop("cycle_cap_reached")
                return
            yield control.wait(started + interval_s - control.elapsed())

    return exp
