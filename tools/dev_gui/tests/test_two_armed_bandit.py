"""Tests for the two_armed_bandit template (script.py-based)."""

from __future__ import annotations

import pytest

from base_station.experiment import EventKind, ExperimentControl, NodeEvent
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


def test_no_feed_dwell_defaults_to_developer_menu_class_setting() -> None:
    """With no dwell_s passed to build(), the empty arm's DispenseNoFeed
    payload should carry ExperimentControl.default_no_feed_dwell_s — the
    process-wide value the GUI's Developer Menu writes — not a hardcoded
    per-template default."""
    original = ExperimentControl.default_no_feed_dwell_s
    try:
        ExperimentControl.default_no_feed_dwell_s = 4.0
        exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1)
        runner = exp.make_runner()
        runner.start(now=0.0)
        _bring_online(runner, [1, 2])

        no_fed = _no_feed_cmds(runner)
        assert len(no_fed) == 1
        payload = no_fed[0][2]
        assert payload[0] | (payload[1] << 8) == 4000

        started = [e for e in runner.ctx.log_entries if e.name == "two_armed_bandit_start"]
        assert started and "dwell_s" not in started[0].fields
    finally:
        ExperimentControl.default_no_feed_dwell_s = original


def test_no_feed_dwell_explicit_build_kwarg_overrides_developer_menu_default() -> None:
    original = ExperimentControl.default_no_feed_dwell_s
    try:
        ExperimentControl.default_no_feed_dwell_s = 4.0
        exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1, dwell_s=1.5)
        runner = exp.make_runner()
        runner.start(now=0.0)
        _bring_online(runner, [1, 2])

        no_fed = _no_feed_cmds(runner)
        payload = no_fed[0][2]
        assert payload[0] | (payload[1] << 8) == 1500
    finally:
        ExperimentControl.default_no_feed_dwell_s = original


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


def test_fault_on_fed_arm_pauses_whole_session_until_recovered() -> None:
    """A fault on EITHER arm must pause the whole session — the healthy arm
    must not keep dispensing solo, since that would give away which node is
    armed just as surely as skipping the no-feed cycle would."""
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, fixed_delay_s=0.1, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    # Fault during the sync gate — before either arm has finished presenting.
    runner.inject(NodeEvent(EventKind.FAULT, node_id=1, timestamp=1.0))
    aborted = [e for e in runner.ctx.log_entries if e.name == "bandit_trial_aborted"]
    assert len(aborted) == 1
    assert not runner.is_finished

    paused = [e for e in runner.ctx.log_entries if e.name == "paused_for_fault"]
    assert len(paused) == 1
    assert paused[0].fields.get("nodes") == [1]

    # Node 1 is still faulted: no new trial should start dispensing, on arm 1
    # OR arm 2 — the session is paused, not just cooling down.
    dispensed_before = len(_dispense_cmds(runner)) + len(_no_feed_cmds(runner))
    runner.step(now=30.0)
    assert runner.ctx.trial == 1
    assert len(_dispense_cmds(runner)) + len(_no_feed_cmds(runner)) == dispensed_before
    assert [e for e in runner.ctx.log_entries if e.name == "resumed_after_fault"] == []

    # Operator recovers node 1 — the session resumes and trial 2 starts.
    runner.recover_node(1, now=31.0)
    resumed = [e for e in runner.ctx.log_entries if e.name == "resumed_after_fault"]
    assert len(resumed) == 1
    assert runner.ctx.trial == 2
    assert _dispense_cmds(runner)[-1][0] == 1
    assert _no_feed_cmds(runner)[-1][0] == 2


def test_fault_on_empty_arm_also_pauses_the_whole_session() -> None:
    """Symmetric with the fed-arm case: a fault on the MIMIC arm must pause
    the fed arm too, not just the faulted one."""
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, fixed_delay_s=0.1, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    # Node 2 (the empty arm) faults during the sync gate.
    runner.inject(NodeEvent(EventKind.FAULT, node_id=2, timestamp=1.0))
    paused = [e for e in runner.ctx.log_entries if e.name == "paused_for_fault"]
    assert len(paused) == 1
    assert paused[0].fields.get("nodes") == [2]

    dispensed_before = len(_dispense_cmds(runner)) + len(_no_feed_cmds(runner))
    runner.step(now=30.0)
    assert runner.ctx.trial == 1
    assert len(_dispense_cmds(runner)) + len(_no_feed_cmds(runner)) == dispensed_before

    runner.recover_node(2, now=31.0)
    assert [e for e in runner.ctx.log_entries if e.name == "resumed_after_fault"]
    assert runner.ctx.trial == 2


def test_fault_before_first_trial_pauses_startup() -> None:
    """A node that comes online already faulted must pause the session
    before trial 1 even starts — not just faults that occur mid-trial."""
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject([
        NodeEvent(EventKind.NODE_ONLINE, node_id=1, timestamp=0.0),
        NodeEvent(EventKind.NODE_ONLINE, node_id=2, timestamp=0.0),
        NodeEvent(EventKind.FAULT, node_id=1, timestamp=0.0),
    ])

    assert runner.ctx.trial == 0
    paused = [e for e in runner.ctx.log_entries if e.name == "paused_for_fault"]
    assert len(paused) == 1
    assert len(_dispense_cmds(runner)) == 0
    assert len(_no_feed_cmds(runner)) == 0

    runner.recover_node(1, now=5.0)
    assert runner.ctx.trial == 1
    assert len(_dispense_cmds(runner)) == 1


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
    occupied = [e for e in runner.ctx.log_entries if e.name == "plate_occupied_wait"]
    assert len(occupied) == 1
    assert runner.ctx.trial == 0  # trial 1 hasn't started yet

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=1.0))
    cleared = [e for e in runner.ctx.log_entries if e.name == "plates_clear"]
    assert len(cleared) == 1
    assert runner.ctx.trial == 1


def test_plate_occupied_stall_names_the_wait() -> None:
    """script_stalled must say plates_clear, not <lambda>."""
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject([
        NodeEvent(EventKind.NODE_ONLINE, node_id=1, timestamp=0.0),
        NodeEvent(EventKind.NODE_ONLINE, node_id=2, timestamp=0.0),
        NodeEvent(EventKind.ON_PLATE, node_id=1, timestamp=0.0),
    ])
    assert [e for e in runner.ctx.log_entries if e.name == "plate_occupied_wait"]

    runner.step(now=120.0)
    stalls = [e for e in runner.ctx.log_entries if e.name == "script_stalled"]
    assert len(stalls) == 1
    assert stalls[0].fields["waiting_on"] == "plates_clear(node=1,2)"
    assert runner.ctx.trial == 0


def test_plate_occupied_after_trial_blocks_next_trial_until_clear() -> None:
    """The plate-clear gate must run before every trial, not just the
    first — a stray pellet on either arm blocks trial 2 exactly as it
    would have blocked trial 1."""
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, fixed_delay_s=0.1, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    _present_both_arms(runner, fed=1, empty=2, ts=1.0)
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0))
    assert runner.ctx.trial == 1

    # A pellet unexpectedly shows up on arm 2's plate before trial 2 starts
    # (leftover debris, sensor artifact, ...) — must block trial 2 exactly
    # like the startup sweep blocks trial 1.
    runner.inject(NodeEvent(EventKind.ON_PLATE, node_id=2, timestamp=2.05))
    runner.step(now=2.2)  # past fixed_delay_s
    occupied = [e for e in runner.ctx.log_entries if e.name == "plate_occupied_wait"]
    assert len(occupied) == 1
    assert occupied[0].fields.get("occupied") == [2]
    assert runner.ctx.trial == 1  # trial 2 has NOT started
    assert _dispense_cmds(runner) == [(1, CanCmd.Dispense, b"")]  # only trial 1's command

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=2, timestamp=3.0))
    cleared = [e for e in runner.ctx.log_entries if e.name == "plates_clear"]
    assert len(cleared) == 1
    assert runner.ctx.trial == 2


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


def test_mimic_off_sends_only_the_fed_dispense() -> None:
    """mimic=False skips the empty arm's no-feed command entirely."""
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1, mimic=False)
    runner = exp.make_runner()
    runner.start(now=0.0)
    _bring_online(runner, [1, 2])

    assert len(_dispense_cmds(runner)) == 1
    assert _dispense_cmds(runner)[0][0] == 1
    assert _no_feed_cmds(runner) == []
