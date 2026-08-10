"""Tests for the two_armed_bandit template (script.py-based)."""

from __future__ import annotations

import pytest

from base_station.experiment import EventKind, NodeEvent
from base_station.experiment.templates.two_armed_bandit import build as build_bandit
from base_station.protocol import CanCmd


def _dispense_cmds(runner):
    return [c for c in runner.ctx.commands_sent if c[1] == CanCmd.Dispense]


def _no_feed_cmds(runner):
    return [c for c in runner.ctx.commands_sent if c[1] == CanCmd.DispenseNoFeed]


def _bring_online(runner, nodes, ts=0.0):
    """Startup-sweep prerequisite: both arms must be online before trial 1."""
    runner.inject([NodeEvent(EventKind.NODE_ONLINE, node_id=n, timestamp=ts) for n in nodes])


def _present_both_arms(runner, fed, empty, ts):
    """Satisfy the sync gate: both arms finish raising."""
    runner.inject([
        NodeEvent(EventKind.LOADED, node_id=fed, timestamp=ts),
        NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=empty, timestamp=ts),
    ])


def test_requires_exactly_two_nodes() -> None:
    with pytest.raises(ValueError):
        build_bandit(nodes=[1, 2, 3])
    with pytest.raises(ValueError):
        build_bandit(nodes=[1])


def test_first_trial_feeds_rich_arm_and_no_feeds_the_other() -> None:
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    assert runner.ctx.trial == 1
    dispensed = _dispense_cmds(runner)
    no_fed = _no_feed_cmds(runner)
    assert len(dispensed) == 1
    assert len(no_fed) == 1
    assert dispensed[0][0] == 1  # block 0 → arm 1 is rich; p_high=1.0 → always fed
    assert no_fed[0][0] == 2


def test_advances_to_next_trial_after_sync_gate_and_take() -> None:
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, fixed_delay_s=0.1, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    _present_both_arms(runner, fed=1, empty=2, ts=1.0)
    presented = [e for e in runner.ctx.log_entries if e.name == "arm_presented"]
    assert len(presented) == 2  # sync gate resolved: both arms logged as presented

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0))
    ended = [e for e in runner.ctx.log_entries if e.name == "bandit_trial_end"]
    assert len(ended) == 1
    assert ended[0].fields.get("outcome") == "taken"

    runner.step(now=2.2)  # past fixed_delay_s
    assert runner.ctx.trial == 2
    assert len(_dispense_cmds(runner)) == 2  # trial 1 + trial 2, still arm 1 (same block)


def test_sync_gate_waits_for_both_arms_before_watching_for_take() -> None:
    """Only the fed arm presenting must NOT be enough to open the response
    window — that would reintroduce the arrival-order cue."""
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    assert [e for e in runner.ctx.log_entries if e.name == "arm_presented"] == []

    # A PelletTaken on the fed arm at this point must not be treated as the
    # trial's take, since the sync gate hasn't opened the window yet.
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=1.5))
    assert [e for e in runner.ctx.log_entries if e.name == "bandit_trial_end"] == []

    runner.inject(NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=2, timestamp=2.0))
    presented = [e for e in runner.ctx.log_entries if e.name == "arm_presented"]
    assert len(presented) == 2


def test_block_flip_switches_the_rich_arm() -> None:
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=1, fixed_delay_s=0.1, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])
    assert _dispense_cmds(runner)[-1][0] == 1  # trial 1, block 0 → arm 1

    _present_both_arms(runner, fed=1, empty=2, ts=1.0)
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0))
    runner.step(now=2.2)

    assert runner.ctx.trial == 2
    assert _dispense_cmds(runner)[-1][0] == 2  # trial 2, block 1 → arm 2 now rich


def test_fault_on_fed_arm_aborts_trial_and_starts_a_new_one() -> None:
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, fixed_delay_s=0.1, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    # Fault during the sync gate — before either arm has finished presenting.
    runner.inject(NodeEvent(EventKind.FAULT, node_id=1, timestamp=1.0))
    aborted = [e for e in runner.ctx.log_entries if e.name == "bandit_trial_aborted"]
    assert len(aborted) == 1
    assert not runner.is_finished

    # After the 5s cooldown, a new trial starts. Node 1 is still halted, so
    # its dispense is a no-op and the trial is logged invalid — but the
    # session keeps running and the trial counter still advances.
    runner.step(now=7.0)
    assert runner.ctx.trial == 2
    invalid = [e for e in runner.ctx.log_entries if e.name == "bandit_trial_invalid"]
    assert any(e.fields.get("invalid_reason") == "fed_arm_halted" for e in invalid)


def test_arm_ready_timeout_marks_trial_invalid() -> None:
    """If one arm's command is dropped and it never presents, the internal
    mechanical safeguard (not a user-facing timeout) marks the trial invalid
    instead of hanging the session forever."""
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    # Only the fed arm ever presents; the empty arm's NoFeedPresented never
    # arrives (simulates a dropped command).
    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))

    runner.step(now=1.0 + 90.0 + 1.0)  # past the internal _ARM_READY_TIMEOUT_S
    invalid = [e for e in runner.ctx.log_entries if e.name == "bandit_trial_invalid"]
    assert len(invalid) == 1
    assert invalid[0].fields.get("invalid_reason") == "arm_never_presented"
    assert not runner.is_finished


def test_both_arms_baited_is_logged_invalid_and_session_continues() -> None:
    """If the empty arm's plate was already occupied, DispenseNoFeed routes
    to FeedSkipped and presents a REAL pellet — both arms end up baited.
    Must be detected, logged, and NOT silently treated as a normal trial."""
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, fixed_delay_s=0.1, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    # fed=1 presents normally; empty=2 also ends up LOADED (real pellet).
    runner.inject([
        NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0),
        NodeEvent(EventKind.LOADED, node_id=2, timestamp=1.0),
    ])
    invalid = [e for e in runner.ctx.log_entries if e.name == "bandit_trial_invalid"]
    assert len(invalid) == 1
    assert invalid[0].fields.get("invalid_reason") == "both_arms_baited"
    assert invalid[0].fields.get("baited_arms") == 2
    assert runner.ctx.counter("trials_invalid") == 1

    # Session continues, and the take is watched on BOTH nodes.
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=2, timestamp=2.0))
    ended = [e for e in runner.ctx.log_entries if e.name == "bandit_trial_end"]
    assert len(ended) == 1
    assert ended[0].fields.get("outcome") == "invalid"
    assert ended[0].fields.get("valid") == 0
    assert not runner.is_finished

    runner.step(now=2.2)
    assert runner.ctx.trial == 2  # session kept going


def test_startup_sweep_waits_for_occupied_plate() -> None:
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    # Node 1 already has a pellet on the plate (leftover from earlier).
    runner.inject([
        NodeEvent(EventKind.NODE_ONLINE, node_id=1, timestamp=0.0),
        NodeEvent(EventKind.NODE_ONLINE, node_id=2, timestamp=0.0),
        NodeEvent(EventKind.ON_PLATE, node_id=1, timestamp=0.0),
    ])
    occupied = [e for e in runner.ctx.log_entries if e.name == "bandit_startup_plate_occupied"]
    assert len(occupied) == 1
    assert runner.ctx.trial == 0  # trial 1 hasn't started yet

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=1.0))
    cleared = [e for e in runner.ctx.log_entries if e.name == "bandit_startup_plates_clear"]
    assert len(cleared) == 1
    assert runner.ctx.trial == 1


def test_presence_clear_mode_gates_next_trial() -> None:
    exp = build_bandit(
        nodes=[1, 2], p_high=1.0, block_size=50,
        next_trial_wait="presence_clear", iti_quiet_s=0.0, seed=1,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])
    # Prime presence True on node 1 BEFORE the ITI wait is armed, so it does
    # not resolve trivially (presence defaults False until a reading arrives).
    runner.ctx.observe_event(NodeEvent(
        EventKind.PRESENCE_CHANGED, node_id=1, timestamp=0.0,
        data={"active": True, "source": "event"},
    ))

    _present_both_arms(runner, fed=1, empty=2, ts=1.0)
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0))
    assert runner.ctx.trial == 1  # still trial 1 — presence hasn't cleared

    runner.inject(NodeEvent(
        EventKind.PRESENCE_CHANGED, node_id=1, timestamp=3.0,
        data={"active": False, "source": "event"},
    ))
    assert runner.ctx.trial == 2
