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

Mutual exclusion is guaranteed by construction — exactly one arm is chosen
"fed" per trial and the other is always dispensed with ``feed=False``. Before
every trial (not just the first), the template waits for both plates to be
clear — pellet sensing, not presence, gates the next trial; see
``_wait_plates_clear`` — and ``ExperimentControl.dispense()`` itself vetoes
(never transmits) a Dispense/DispenseNoFeed command to a node whose plate is
already occupied, logging ``dispense_skipped_pellet_present`` and returning
False. That veto is the primary defense; this template treats it the same as
a halted arm (see the "not fed_ok or not empty_ok" branch below) and logs
``bandit_trial_invalid`` with a distinguishing reason. Firmware's own
occupancy guard (``DispenserService::dispense()`` — presents an already-there
pellet honestly via ``FeedSkipped`` rather than discarding it) remains as a
backstop for the small race between the client-side check and the command
reaching the bus; ``presented_pellet(empty)`` below still detects that rare
case and logs ``baited_arms=2``.

Sync note: both dispense commands are sent back-to-back synchronously, so the
two nodes start within microseconds of each other. But a fed cycle's M1 run
takes a variable amount of time (pellet-dependent, plus a fixed 2s confirm
hold) while a no-feed cycle dwells a FIXED ``dwell_s`` — so the two arms do
NOT reach the top at the same moment. This template gates the response window
on BOTH arms finishing their raise (``presentation_done``) before doing
anything with either one, so the trial's effective start is well-defined and
the animal can't use "which arm finished raising first" as a cue. It cannot
erase every difference between the arms: M1 physically turns on the fed arm
and never on the empty one, so the dwell matches the *duration* of a fed
cycle, not its exact sound.

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

# Bounds the SYNC GATE only (both arms finishing their raise) — a mechanical
# safeguard against a dropped command, not a behavioral timeout. Comfortably
# above the firmware's worst-case feed timeout (30s) plus travel time. Not
# exposed as a parameter: the trial itself has no timeout by design.
_ARM_READY_TIMEOUT_S = 90.0


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "two_armed_bandit",
    block_size: int = 50,
    p_high: float = 0.9,
    dwell_s: Optional[float] = None,
    next_trial_wait: str = "fixed_delay",
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
    dwell_s:
        Seconds the empty arm holds at the drop position on its no-feed
        cycle. ``None`` (the default) picks up
        ``ExperimentControl.default_no_feed_dwell_s`` — set from the GUI's
        Developer Menu, since this is fundamentally a rig timing constant
        (approximating a fed cycle's M1 run: a ~2.0s confirm hold plus ~1s of
        wheel time — tune it per rig by comparing ``Loading``/``OnPlate``
        timestamps from a free-feeding session), not a per-task parameter.
        Pass an explicit value here to override the Developer Menu default
        for this experiment specifically. The response window is gated on
        both arms finishing regardless, so this mainly affects how closely
        the two arms *sound* alike rather than trial timing.
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
    if next_trial_wait not in ("fixed_delay", "presence_clear"):
        next_trial_wait = "fixed_delay"

    kit.log_faults(exp)
    kit.log_feed_skipped(exp)

    def _resolved_dwell_s(control):
        return dwell_s if dwell_s is not None else control.default_no_feed_dwell_s

    @exp.on_start
    def _log_start(control):
        control.log(
            "two_armed_bandit_start",
            nodes=control.nodes, arm_a=arm_a, arm_b=arm_b,
            block_size=block_size, p_high=p_high,
            next_trial_wait=next_trial_wait, seed=control.seed,
        )

    def _advance(control):
        return kit.next_trial_wait(
            control, next_trial_wait,
            delay_s=fixed_delay_s, nodes=(arm_a, arm_b), quiet_s=iti_quiet_s,
        )

    def _wait_plates_clear(control):
        """
        Pause until both arms' plates are clear of a pellet.

        Pellet sensing outranks presence: this runs before every trial,
        not just the first, so a stray/leftover pellet (missed by the
        animal, or sitting there from before the session started) blocks
        the next dispense instead of racing it. In the common case both
        plates are already clear and this returns immediately.
        """
        occupied = [n for n in (arm_a, arm_b) if control.pellet_on_plate(n)]
        if not occupied:
            return
        control.log(
            "bandit_plate_occupied_wait", warning=1,
            occupied=occupied, action="waiting for plate(s) to clear",
        )
        yield control.wait_until(
            lambda c: not any(c.pellet_on_plate(n) for n in (arm_a, arm_b)),
            node=(arm_a, arm_b),
        )
        control.log("bandit_plates_clear")

    def _wait_for_recovery(control):
        """
        Pause the WHOLE experiment while either arm is halted (faulted).

        Both arms always move together by design (see module docstring) — if
        one arm is down and the other kept dispensing solo, the animal would
        get exactly the "which node is armed" cue this template exists to
        prevent. Unscoped (no ``node=``) so this wait can never itself be
        fault-aborted; it is the thing watching for recovery. A no-op
        (yields nothing) on the common healthy path.
        """
        halted = [n for n in (arm_a, arm_b) if control.is_halted(n)]
        if not halted:
            return
        control.log("bandit_paused_for_fault", nodes=halted)
        yield control.wait_until(
            lambda c: not c.is_halted(arm_a) and not c.is_halted(arm_b)
        )
        control.log("bandit_resumed_after_fault")

    @exp.script
    def run(control):
        # --- startup sweep: both plates clear before trial 1 ---
        online = yield control.wait_until(
            lambda c: all(c.is_online(n) for n in (arm_a, arm_b)), timeout=20.0
        )
        if not online.ok:
            control.log(
                "bandit_startup_nodes_offline",
                nodes=[n for n in (arm_a, arm_b) if not control.is_online(n)],
            )
        yield from _wait_plates_clear(control)

        while True:
            yield from _wait_for_recovery(control)
            yield from _wait_plates_clear(control)

            trial = control.next_trial()
            block = (trial - 1) // block_size
            rich, lean = (arm_a, arm_b) if block % 2 == 0 else (arm_b, arm_a)
            fed = rich if control.chance(p_high) else lean
            empty = arm_b if fed == arm_a else arm_a

            resolved_dwell_s = _resolved_dwell_s(control)

            control.clear_presentation(fed)
            control.clear_presentation(empty)
            fed_ok = control.dispense(fed)
            empty_ok = control.dispense(empty, feed=False, dwell_s=resolved_dwell_s)
            control.log(
                "bandit_trial", trial=trial, block=block,
                rich=rich, lean=lean, fed=fed, empty=empty,
                fed_accepted=int(fed_ok), empty_accepted=int(empty_ok),
            )

            if not fed_ok or not empty_ok:
                # dispense() vetoes (returns False, no command sent) for two
                # distinct reasons — a halted node, or this node's own plate
                # already occupied (the _wait_plates_clear gate above should
                # make the latter rare, but a pellet can land in the gap
                # between that check and the command going out).
                node, role = (fed, "fed") if not fed_ok else (empty, "empty")
                cause = "halted" if control.is_halted(node) else "plate_occupied"
                control.log(
                    "bandit_trial_invalid", trial=trial, valid=0,
                    invalid_reason=f"{role}_arm_{cause}", baited_arms=0,
                )
                yield _advance(control)
                continue

            # --- sync gate: BOTH arms finish their raise before the window
            # opens, so neither arm's completion is acted on until both are
            # up (removes the arrival-order cue described in the module
            # docstring). No behavioral timeout — only the mechanical
            # safeguard for a dropped command.
            ready = yield control.wait_until(
                lambda c: c.presentation_done(fed) and c.presentation_done(empty),
                node=(fed, empty), timeout=_ARM_READY_TIMEOUT_S,
            )
            if ready.faulted:
                control.log("bandit_trial_aborted", trial=trial, node=ready.faulted_node)
                continue  # top of loop pauses the whole session until recovered
            if ready.timed_out:
                control.log(
                    "bandit_trial_invalid", trial=trial, valid=0,
                    invalid_reason="arm_never_presented",
                    fed_state=control.presentation(fed),
                    empty_state=control.presentation(empty),
                )
                yield _advance(control)
                continue

            control.log(
                "arm_presented", node=fed, trial=trial, role="fed",
                presented=control.presentation(fed),
                delivered=int(control.presented_pellet(fed)),
            )
            control.log(
                "arm_presented", node=empty, trial=trial, role="empty",
                presented=control.presentation(empty),
                delivered=int(control.presented_pellet(empty)),
            )

            # --- mutual-exclusion violation: the empty arm's plate was
            # already occupied, so DispenseNoFeed routed to FeedSkipped and
            # presented a REAL pellet. Both arms are baited this trial.
            invalid = control.presented_pellet(empty)
            if invalid:
                control.incr("trials_invalid")
                control.log(
                    "bandit_trial_invalid", trial=trial, valid=0,
                    invalid_reason="both_arms_baited", baited_arms=2,
                    empty_arm=empty,
                )

            watch = (fed, empty) if invalid else fed
            r = yield control.wait_for("pellet_taken", node=watch)
            if r.faulted:
                control.log("bandit_trial_aborted", trial=trial, node=r.faulted_node)
                continue  # top of loop pauses the whole session until recovered

            control.log(
                "bandit_trial_end", trial=trial, block=block,
                rich=rich, lean=lean, fed=fed, empty=empty,
                delivered_node=fed,
                baited_arms=2 if invalid else 1,
                taken_node=r.event.node_id if r.event else 0,
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
