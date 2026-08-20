"""
two_armed_bandit.py — Two-armed bandit task with reward-probability blocks.

Every trial, BOTH arms run the full dispense motion when ``mimic`` is on, but
only one delivers a pellet — the other gets a no-feed cycle (identical motion,
motor, and dome sound, but nothing to find) so the animal can't use sound or
vibration alone to locate the baited arm. ``mimic=False`` skips the empty
arm's command entirely.

Which arm is "rich" (delivers with probability ``p_high``) flips every
``block_size`` trials: arm A rich for trials 1..block_size, arm B rich for
block_size+1..2*block_size, and so on. The "lean" arm still delivers
occasionally, at probability ``1 - p_high``, so a fixed rule never fully
explains the outcome.

Mutual exclusion is guaranteed by construction — exactly one arm is chosen
"fed" per trial and the other is dispensed with ``feed=False`` when ``mimic``
is on. Before every trial (not just the first), the template waits for both
plates to be clear — pellet sensing, not presence, gates the next trial; see
``kit.wait_plates_clear``. ``ExperimentControl.dispense()`` itself vetoes
(never transmits) a Dispense/DispenseNoFeed command to a node whose plate is
already occupied, logging ``dispense_skipped_pellet_present`` and returning
False. That veto is the primary defense; this template treats it the same as
a halted arm (see the "not all accepted" branch below) and logs
``bandit_trial_invalid`` with a distinguishing reason. Firmware's own
occupancy guard (``DispenserService::dispense()`` — presents an already-there
pellet honestly via ``FeedSkipped`` rather than discarding it) remains as a
backstop for the small race between the client-side check and the command
reaching the bus; ``presented_pellet(empty)`` below still detects that rare
case and logs ``baited_arms=2``.

Sync note: both dispense commands are sent back-to-back synchronously, so the
two nodes start within microseconds of each other. A fed cycle's M1 run then
takes a variable amount of time (pellet-dependent, plus a fixed 2s confirm
hold), so the empty arm cannot know in advance how long to wait — instead it
holds at the drop position and raises when it hears the fed arm's Raising event
on the CAN bus, one frame time later (~0.5 ms). Both plates therefore reach the
top together on every trial, whatever the fed arm's load took.

This template additionally gates the response window on BOTH arms finishing
their raise (``presentation_done``) before doing anything with either one, so
the trial's effective start stays well-defined even if a command is dropped.
None of this erases every difference between the arms: M1 physically turns on
the fed arm and never on the empty one, so the mimic matches the *timing* of a
fed cycle, not its exact sound.

Fault handling: if EITHER arm faults, the whole session pauses — neither arm
dispenses — until an operator recovers the faulted node. A healthy arm never
runs solo while its partner is down; that would give away which node is
armed just as surely as skipping the no-feed cycle would.

One trial:
  1. Dispense the fed arm; no-feed-dispense the other.
  2. Wait for both arms to finish raising (whichever presents first, wait for
     the other) before doing anything — no timeout here, only an internal
     mechanical safeguard in case a command was dropped.
  3. Wait for PelletTaken on the fed arm — no timeout: the trial waits as
     long as it takes the animal to retrieve the pellet.
  4. Advance to the next trial either after a fixed delay, or once the
     animal is off the presence pads (``next_trial_wait``).

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
    mimic: bool = True,
    next_trial_wait: str = "presence_clear",
    fixed_delay_s: float = 5.0,
    iti_quiet_s: float = 1.0,
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
    mimic:
        When True (default), the empty arm runs a no-feed dispense so both
        arms move. When False, only the fed arm is commanded.
    next_trial_wait:
        "fixed_delay" (wait ``fixed_delay_s``) or "presence_clear" (wait
        until the animal is off both presence pads). See ``kit.next_trial_wait``.
    fixed_delay_s:
        Seconds to wait before the next trial when ``next_trial_wait ==
        "fixed_delay"``.
    iti_quiet_s:
        When ``next_trial_wait == "presence_clear"``, seconds presence must
        stay clear (with no sensor activity) before the next trial starts.
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
    mimic = bool(mimic)
    next_trial_wait, fixed_delay_s = kit.resolve_advance(
        next_trial_wait=next_trial_wait, fixed_delay_s=fixed_delay_s,
        default_delay_s=5.0,
    )

    kit.log_faults(exp)
    kit.log_feed_skipped(exp)

    @exp.on_start
    def _log_start(control):
        control.log(
            "two_armed_bandit_start",
            nodes=control.nodes, arm_a=arm_a, arm_b=arm_b,
            block_size=block_size, p_high=p_high, mimic=int(mimic),
            next_trial_wait=next_trial_wait, seed=control.seed,
        )

    def _advance(control):
        return kit.next_trial_wait(
            control, next_trial_wait,
            delay_s=fixed_delay_s, nodes=(arm_a, arm_b), quiet_s=iti_quiet_s,
        )

    @exp.script
    def run(control):
        # --- startup sweep: both plates clear before trial 1 ---
        online = yield control.wait_until(
            lambda c: all(c.is_online(n) for n in (arm_a, arm_b)),
            timeout=20.0,
            label="nodes_online",
        )
        if not online.ok:
            control.log(
                "bandit_startup_nodes_offline",
                nodes=[n for n in (arm_a, arm_b) if not control.is_online(n)],
            )
        yield from kit.wait_plates_clear(control, (arm_a, arm_b))

        while True:
            yield from kit.pause_while_faulted(control, (arm_a, arm_b))
            yield from kit.wait_plates_clear(control, (arm_a, arm_b))

            trial = control.next_trial()
            block = (trial - 1) // block_size
            rich, lean = (arm_a, arm_b) if block % 2 == 0 else (arm_b, arm_a)
            fed = rich if control.chance(p_high) else lean
            empty = arm_b if fed == arm_a else arm_a
            mimics = (empty,) if mimic else ()

            def _on_commanded(result):
                fed_ok = result.accepted.get(fed, False)
                empty_ok = result.accepted.get(empty, False)
                control.log(
                    "bandit_trial", trial=trial, block=block,
                    rich=rich, lean=lean, fed=fed, empty=empty,
                    fed_accepted=int(fed_ok), empty_accepted=int(empty_ok),
                )
                if result.invalid_reason in ("halted", "plate_occupied"):
                    node, role = (fed, "fed") if not fed_ok else (empty, "empty")
                    control.log(
                        "bandit_trial_invalid", trial=trial, valid=0,
                        invalid_reason=f"{role}_arm_{result.invalid_reason}",
                        baited_arms=0,
                    )

            def _on_presented(result):
                control.log(
                    "arm_presented", node=fed, trial=trial, role="fed",
                    presented=control.presentation(fed),
                    delivered=int(control.presented_pellet(fed)),
                )
                if empty in result.mimic_nodes:
                    control.log(
                        "arm_presented", node=empty, trial=trial, role="empty",
                        presented=control.presentation(empty),
                        delivered=int(control.presented_pellet(empty)),
                    )
                if result.invalid_reason == "mimic_baited":
                    control.log(
                        "bandit_trial_invalid", trial=trial, valid=0,
                        invalid_reason="both_arms_baited", baited_arms=2,
                        empty_arm=empty,
                    )

            result = yield from kit.synchronized_cycle(
                control, fed, mimics,
                on_commanded=_on_commanded, on_presented=_on_presented,
            )

            if result.faulted:
                control.log("bandit_trial_aborted", trial=trial, node=result.faulted_node)
                continue  # top of loop pauses the whole session until recovered
            if result.invalid_reason == "arm_never_presented":
                control.log(
                    "bandit_trial_invalid", trial=trial, valid=0,
                    invalid_reason="arm_never_presented",
                    fed_state=control.presentation(fed),
                    empty_state=control.presentation(empty),
                )
                yield _advance(control)
                continue
            if result.invalid_reason in ("halted", "plate_occupied"):
                yield _advance(control)
                continue

            invalid = result.invalid_reason == "mimic_baited"
            control.log(
                "bandit_trial_end", trial=trial, block=block,
                rich=rich, lean=lean, fed=fed, empty=empty,
                delivered_node=fed,
                baited_arms=2 if invalid else 1,
                taken_node=result.taken_node,
                outcome="invalid" if invalid else "taken",
                valid=0 if invalid else 1,
            )

            yield _advance(control)

    @exp.on_end
    def _end(control):
        control.log(
            "two_armed_bandit_end",
            trials=control.trial,
            trials_invalid=control.counter("trials_invalid"),
            pellets=control.counter("pellets"),
            elapsed_s=round(control.elapsed(), 3),
        )

    return exp
