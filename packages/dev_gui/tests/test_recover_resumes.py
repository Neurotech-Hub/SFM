"""Operator / auto Recover during a running experiment must never strand it.

Firmware ``recover()`` returns the node to Idle from ANY phase. An Idle node
never emits Loaded / NoFeedPresented / PelletTaken, so a script await waiting on
one of those for the recovered node can only end if the scheduler aborts it.
"""

from __future__ import annotations

import argparse

import pytest

from base_station.experiment import EventKind, NodeEvent
from base_station.experiment.templates.two_armed_bandit import build as build_bandit
from base_station.protocol import CanCmd


def _bandit():
    exp = build_bandit(nodes=[1, 2], p_high=1.0, block_size=50,
                       next_trial_wait="fixed_delay", fixed_delay_s=0.1, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject([NodeEvent(EventKind.NODE_ONLINE, node_id=n, timestamp=0.0) for n in (1, 2)])
    assert runner.ctx.trial == 1
    return runner


def _cmds(runner, cmd):
    return [c for c in runner.ctx.commands_sent if c[1] == cmd]


def _pellet_gate(node, active, ts):
    return NodeEvent(EventKind.PG_CHANGED, node_id=node, timestamp=ts,
                     data={"gate": "pellet", "active": active, "source": "event"})


def test_recover_while_waiting_for_take_moves_to_next_trial() -> None:
    """The 110426_1.2F trial-121 pattern: operator hits Recover on the fed arm
    while the script waits for PelletTaken. The node goes Idle; the mouse then
    eats the pellet (only an InputChanged pellet edge — no PelletTaken from
    Idle). The session must still reach trial 2."""
    runner = _bandit()
    runner.inject([
        NodeEvent(EventKind.ON_PLATE, node_id=1, timestamp=1.0),
        NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.5),
        NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=2, timestamp=1.5),
    ])
    runner.recover_node(1, now=5.0)
    runner.inject(_pellet_gate(1, False, 6.0))  # mouse takes it from the Idle plate
    runner.step(now=6.5)
    assert runner.ctx.trial == 2
    assert any(e.name == "bandit_trial_aborted" for e in runner.ctx.log_entries)


def test_recover_during_raise_does_not_wait_out_the_presentation_timeout() -> None:
    runner = _bandit()
    runner.inject([
        NodeEvent(EventKind.RAISING, node_id=1, timestamp=1.0),
        NodeEvent(EventKind.RAISING, node_id=2, timestamp=1.0),
    ])
    recovers_before = len(_cmds(runner, CanCmd.Recover))
    runner.recover_node(1, now=2.0)
    runner.step(now=2.1)
    assert runner.ctx.trial == 2
    # The empty arm was mid-cycle too; it is cleared rather than left parked.
    recovered_nodes = [c[0] for c in _cmds(runner, CanCmd.Recover)[recovers_before:]]
    assert 2 in recovered_nodes
    assert not any(e.name == "script_timeout" for e in runner.ctx.log_entries)


def test_recover_of_faulted_node_resumes_and_runs_a_full_trial() -> None:
    runner = _bandit()
    runner.inject(NodeEvent(EventKind.FAULT, node_id=1, timestamp=1.0,
                            data={"fault_code": 5}))
    runner.step(now=100.0)
    assert runner.ctx.trial == 1
    runner.recover_node(1, now=101.0)
    assert runner.ctx.trial == 2
    runner.inject([
        NodeEvent(EventKind.LOADED, node_id=1, timestamp=105.0),
        NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=2, timestamp=105.0),
        NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=106.0),
    ])
    runner.step(now=106.5)
    assert runner.ctx.trial == 3


def test_recover_of_idle_node_between_trials_is_harmless() -> None:
    runner = _bandit()
    runner.inject([
        NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0),
        NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=2, timestamp=1.0),
        NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0),
    ])
    runner.recover_node(2, now=2.05)  # during the 0.1 s ITI
    runner.step(now=2.3)
    assert runner.ctx.trial == 2
    assert not any(e.name == "bandit_trial_aborted" for e in runner.ctx.log_entries)


# ---------------------------------------------------------------------------
# GUI "Recover All" (and BNC recover_all) during an experiment
# ---------------------------------------------------------------------------


class _FakeCan:
    def __init__(self) -> None:
        self.commands: list = []
        self.broadcasts: list = []

    def send_command(self, node_id, cmd, payload=b""):
        self.commands.append((node_id, cmd, payload))
        return True

    def send_broadcast(self, cmd, payload=b""):
        self.broadcasts.append((cmd, payload))
        return True


def test_recover_all_broadcast_resumes_a_fault_paused_experiment() -> None:
    try:
        from base_station.app import SFMApp
    except ModuleNotFoundError as exc:
        if "dearpygui" in str(exc).lower():
            pytest.skip("dearpygui not installed")
        raise
    from base_station.experiment.schema import DEFAULT_EXPERIMENTS_DIR, load_experiment_def
    from base_station.log_manager import LogManager
    from base_station.node_registry import NodeRegistry

    app = SFMApp(argparse.Namespace(interface="can0", bitrate=250000, nodes=2, log_dir="~/sfm_logs"))
    app._can = _FakeCan()
    app._registry = NodeRegistry(2)
    app._log = LogManager(auto_save=False)
    app._refresh_tile = lambda *a, **k: None
    app._refresh_all_tiles = lambda *a, **k: None

    ff = load_experiment_def(DEFAULT_EXPERIMENTS_DIR / "two_armed_bandit.json")
    assert app._exp.start(ff, params={"seed": 1}, nodes=[1, 2], log=app._log)
    runner = app._exp.runner
    runner.inject([NodeEvent(EventKind.NODE_ONLINE, node_id=n, timestamp=0.0) for n in (1, 2)])
    runner.inject(NodeEvent(EventKind.FAULT, node_id=1, timestamp=1.0, data={"fault_code": 5}))
    assert runner.ctx.is_halted(1)

    app._broadcast(CanCmd.Recover)

    assert not runner.ctx.is_halted(1)
    assert any(e.name == "resumed_after_fault" for e in runner.ctx.log_entries)
    assert runner.ctx.trial == 2


# ---------------------------------------------------------------------------
# Other templates
# ---------------------------------------------------------------------------


def _fed_nodes(runner):
    return [c[0] for c in runner.ctx.commands_sent if c[1] == CanCmd.Dispense]


def test_synchronized_loop_template_resumes_after_recover_mid_take_wait() -> None:
    """probability_delivery / fixed_and_random share kit.wire_synchronized_loop."""
    from base_station.experiment.templates.probability_delivery import build

    exp = build(nodes=[1], next_trial_wait="fixed_delay", fixed_delay_s=0.1, seed=1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject(NodeEvent(EventKind.NODE_ONLINE, node_id=1, timestamp=0.0))
    first = runner.ctx.trial
    fed = _fed_nodes(runner)
    if not fed:
        pytest.skip("seed did not feed on the first cycle")
    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    runner.recover_node(1, now=2.0)
    runner.inject(_pellet_gate(1, False, 3.0))
    runner.step(now=3.5)
    assert runner.ctx.trial == first + 1


def test_free_feeding_reloads_after_recover_of_a_presenting_node() -> None:
    from base_station.experiment.templates.free_feeding import build

    exp = build(nodes=[1], next_trial_wait="fixed_delay", fixed_delay_s=0.1)
    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject(NodeEvent(EventKind.NODE_ONLINE, node_id=1, timestamp=0.0))
    runner.step(now=0.1)
    n0 = len(_fed_nodes(runner))
    assert n0 >= 1
    runner.inject([
        NodeEvent(EventKind.ON_PLATE, node_id=1, timestamp=1.0),
        NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.5),
    ])
    runner.recover_node(1, now=2.0)          # node Idle, pellet still up
    runner.step(now=2.5)
    assert len(_fed_nodes(runner)) == n0     # not while the pellet is there
    runner.inject(_pellet_gate(1, False, 3.0))  # mouse takes it (no PelletTaken from Idle)
    runner.step(now=3.5)
    assert len(_fed_nodes(runner)) == n0 + 1
