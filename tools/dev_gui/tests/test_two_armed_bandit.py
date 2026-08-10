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


def test_requires_exactly_two_nodes() -> None:
    with pytest.raises(ValueError):
        build_bandit(nodes=[1, 2, 3])
    with pytest.raises(ValueError):
        build_bandit(nodes=[1])


def test_first_trial_feeds_rich_arm_and_no_feeds_the_other() -> None:
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)

    assert runner.ctx.trial == 1
    dispensed = _dispense_cmds(runner)
    no_fed = _no_feed_cmds(runner)
    assert len(dispensed) == 1
    assert len(no_fed) == 1
    assert dispensed[0][0] == 1  # block 0 → arm 1 is rich; p_high=1.0 → always fed
    assert no_fed[0][0] == 2


def test_advances_to_next_trial_after_take_and_iti() -> None:
    exp = build_bandit(
        nodes=[1, 2], p_high=1.0, block_size=50, iti_quiet_s=0.0, iti_max_s=1.0, seed=1
    )
    runner = exp.make_runner()
    runner.start(now=0.0)

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=1.0))
    runner.step(now=3.0)  # past iti_max_s with iti_quiet_s=0 → ITI resolves

    assert runner.ctx.trial == 2
    assert len(_dispense_cmds(runner)) == 2  # trial 1 + trial 2, still arm 1 (same block)


def test_block_flip_switches_the_rich_arm() -> None:
    exp = build_bandit(
        nodes=[1, 2], p_high=1.0, block_size=1, iti_quiet_s=0.0, iti_max_s=1.0, seed=1
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    assert _dispense_cmds(runner)[-1][0] == 1  # trial 1, block 0 → arm 1

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=1.0))
    runner.step(now=3.0)

    assert runner.ctx.trial == 2
    assert _dispense_cmds(runner)[-1][0] == 2  # trial 2, block 1 → arm 2 now rich


def test_fault_on_fed_arm_aborts_trial_and_starts_a_new_one() -> None:
    exp = build_bandit(
        nodes=[1, 2], p_high=1.0, block_size=50, iti_quiet_s=0.0, iti_max_s=1.0, seed=1
    )
    runner = exp.make_runner()
    runner.start(now=0.0)

    runner.inject(NodeEvent(EventKind.FAULT, node_id=1, timestamp=1.0))
    aborted = [e for e in runner.ctx.log_entries if e.name == "bandit_trial_aborted"]
    assert len(aborted) == 1
    assert not runner.is_finished

    # After the 5s cooldown, a new trial starts (still trial 1's arm, node 1,
    # but node 1 is now halted so its dispense is a no-op/logged skip).
    runner.step(now=7.0)
    assert runner.ctx.trial == 2


def test_trial_timeout_is_logged_and_counted_then_session_continues() -> None:
    exp = build_bandit(
        nodes=[1, 2],
        p_high=1.0,
        block_size=50,
        trial_timeout_s=2.0,
        iti_quiet_s=0.0,
        iti_max_s=1.0,
        seed=1,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)

    # Past trial_timeout_s with no PelletTaken: the trial is abandoned, and
    # since domes_closed()/quiet_for(0.0) are trivially true here (no dome
    # ever opened), the ITI condition resolves immediately too — trial 2
    # starts in this same step.
    runner.step(now=3.0)
    abandoned = [e for e in runner.ctx.log_entries if e.name == "bandit_trial_abandoned"]
    assert len(abandoned) == 1
    assert runner.ctx.counter("trials_abandoned") == 1
    assert not runner.is_finished
    assert runner.ctx.trial == 2
