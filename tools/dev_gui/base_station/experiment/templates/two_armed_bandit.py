"""
two_armed_bandit.py — Two-armed bandit task with reward-probability blocks.

Every trial, BOTH arms run the full dispense motion, but only one delivers a
pellet — the other gets a no-feed cycle (identical motion, motor, and dome
sound, but nothing to find) so the animal can't use sound or vibration alone
to locate the baited arm.

Which arm is "rich" (delivers with probability ``p_high``) flips every
``block_size`` trials: arm A rich for trials 1..block_size, arm B rich for
block_size+1..2*block_size, and so on. The "lean" arm still delivers
occasionally, at probability ``1 - p_high``, so a fixed rule never fully
explains the outcome.

One trial:
  1. Dispense the fed arm; no-feed-dispense the other.
  2. Wait for PelletTaken on the fed arm (bounded by ``trial_timeout_s`` —
     an abandoned trial is logged and counted, not treated as a fault).
  3. Wait for both domes closed and ``iti_quiet_s`` of inactivity (bounded by
     ``iti_max_s``) before starting the next trial.

Requires exactly two nodes — the two arms.

Usage::

    from base_station.experiment.templates.two_armed_bandit import build

    exp = build(nodes=[1, 2], block_size=50, p_high=0.9, seed=42)
    exp.run(interface="vcan0")
"""

from __future__ import annotations

from typing import Optional, Sequence

from .. import kit
from ..runner import Experiment


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "two_armed_bandit",
    block_size: int = 50,
    p_high: float = 0.9,
    dwell_s: float = 6.0,
    iti_quiet_s: float = 5.0,
    iti_max_s: float = 30.0,
    trial_timeout_s: float = 60.0,
    hours: float = 0.0,
    minutes: float = 0.0,
    seconds: float = 0.0,
    max_pellets: Optional[int] = None,
    seed: Optional[int] = None,
) -> Experiment:
    """
    Build a two-armed bandit Experiment.

    Parameters
    ----------
    nodes:
        Exactly two node IDs — the two arms. Defaults to [1, 2].
    block_size:
        Trials per reward-probability block before the rich/lean arms flip.
    p_high:
        Probability (0..1) that the rich arm delivers on a given trial (the
        lean arm delivers at ``1 - p_high``).
    dwell_s:
        Seconds the empty arm holds at the drop position on its no-feed cycle
        (matches the time a fed cycle spends loading).
    iti_quiet_s:
        Seconds of no activity (on either arm, with both domes closed)
        required before the next trial starts.
    iti_max_s:
        Cap on how long to wait for that quiet period before starting the
        next trial anyway.
    trial_timeout_s:
        Cap on how long to wait for the animal to take the pellet before
        abandoning the trial and moving on.
    seed:
        RNG seed for reproducible sessions. Always logged in session_start
        (generated if not supplied) so any run can be replayed.
    """
    exp = kit.session(name, nodes, hours=hours, minutes=minutes, seconds=seconds, max_pellets=max_pellets)
    if len(exp.nodes) != 2:
        raise ValueError(
            f"two_armed_bandit requires exactly 2 nodes (the two arms); got {exp.nodes}"
        )
    exp.seed = seed
    arm_a, arm_b = exp.nodes[0], exp.nodes[1]
    p_high = max(0.0, min(1.0, float(p_high)))
    block_size = max(1, int(block_size))

    kit.log_faults(exp)

    @exp.on_start
    def _log_start(control):
        control.log(
            "two_armed_bandit_start",
            nodes=control.nodes, arm_a=arm_a, arm_b=arm_b,
            block_size=block_size, p_high=p_high, seed=control.seed,
        )

    @exp.script
    def run(control):
        while True:
            trial = control.next_trial()
            block = (trial - 1) // block_size
            rich, lean = (arm_a, arm_b) if block % 2 == 0 else (arm_b, arm_a)
            fed = rich if control.chance(p_high) else lean
            empty = arm_b if fed == arm_a else arm_a

            control.log(
                "bandit_trial", trial=trial, block=block, rich=rich, fed=fed, empty=empty
            )
            control.dispense(fed)
            control.dispense(empty, feed=False, dwell_s=dwell_s)

            r = yield control.wait_for("pellet_taken", node=fed, timeout=trial_timeout_s)
            if r.faulted:
                control.log("bandit_trial_aborted", trial=trial, node=r.faulted_node)
                yield control.wait(5.0)  # give the operator a moment before the next trial
                continue
            if r.timed_out:
                control.incr("trials_abandoned")
                control.log("bandit_trial_abandoned", trial=trial, node=fed)

            yield control.wait_until(
                lambda c: c.domes_closed() and c.quiet_for(iti_quiet_s),
                timeout=iti_max_s,
            )

    @exp.on_end
    def _end(control):
        control.log(
            "two_armed_bandit_end",
            trials=control.trial,
            trials_abandoned=control.counter("trials_abandoned"),
            pellets=control.counter("pellets"),
            elapsed_s=round(control.elapsed(), 3),
        )

    return exp
