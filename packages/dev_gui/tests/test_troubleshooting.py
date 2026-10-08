"""Tests for the troubleshooting (actuator-only) template."""

from __future__ import annotations

from types import SimpleNamespace

from base_station.experiment import EventKind, NodeEvent
from base_station.experiment.context import PEER_RAISE_SRC_ID, ExperimentControl
from base_station.experiment.schema import build_experiment, load_experiment_defs, DEFAULT_EXPERIMENTS_DIR
from base_station.experiment.templates.troubleshooting import MOTION_TIMEOUT_S, build
from base_station.protocol import CanCmd, ServiceStatus


def _no_feed_cmds(runner):
    return [c[0] for c in runner.ctx.commands_sent if c[1] == CanCmd.DispenseNoFeed]


def _recover_cmds(runner):
    return [c[0] for c in runner.ctx.commands_sent if c[1] == CanCmd.Recover]


def _rows(runner, name):
    return [e for e in runner.ctx.log_entries if e.name == name]


def _start(nodes, **kw):
    exp = build(nodes=nodes, **kw)
    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject([NodeEvent(EventKind.NODE_ONLINE, node_id=n, timestamp=0.0) for n in nodes])
    return runner


def _down(runner, nodes, ts):
    runner.inject([NodeEvent(EventKind.DWELLING, node_id=n, timestamp=ts) for n in nodes])


def _up(runner, nodes, ts):
    runner.inject([NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=n, timestamp=ts) for n in nodes])


def test_one_cycle_lowers_holds_then_raises_selected_nodes() -> None:
    runner = _start([1, 3], interval_min=1.0, hold_s=3.0)
    assert sorted(_no_feed_cmds(runner)) == [1, 3]
    assert not any(c[1] == CanCmd.Dispense for c in runner.ctx.commands_sent)

    _down(runner, [1, 3], 2.0)
    runner.step(now=4.0)
    assert _rows(runner, "peer_raise") == []  # still holding
    runner.step(now=5.0)
    assert len(_rows(runner, "peer_raise")) == 1
    assert runner.ctx.raw_frames_sent == [(0x300 + PEER_RAISE_SRC_ID, b"\x09")]

    _up(runner, [1, 3], 6.5)
    rows = {e.node_id: e.fields for e in _rows(runner, "actuator_cycle")}
    assert rows[1]["outcome"] == rows[3]["outcome"] == "ok"
    assert rows[1]["lower_s"] == 2.0
    assert rows[1]["raise_s"] == 1.5
    assert _recover_cmds(runner) == []


def test_zero_hold_raises_as_soon_as_every_node_is_down() -> None:
    runner = _start([1, 2], hold_s=0.0)
    _down(runner, [1], 1.0)
    assert _rows(runner, "peer_raise") == []
    _down(runner, [2], 1.5)
    assert len(_rows(runner, "peer_raise")) == 1


def test_cycles_start_on_a_fixed_clock() -> None:
    runner = _start([1], interval_min=1.0, hold_s=3.0)
    _down(runner, [1], 5.0)
    runner.step(now=8.0)
    _up(runner, [1], 10.0)
    runner.step(now=59.0)
    assert len(_no_feed_cmds(runner)) == 1
    runner.step(now=60.0)  # 60 s after the first cycle started, not after it ended
    assert len(_no_feed_cmds(runner)) == 2


def test_faulted_node_is_skipped_while_others_keep_cycling() -> None:
    runner = _start([1, 2], interval_min=1.0, hold_s=1.0)
    runner.inject(NodeEvent(
        EventKind.FAULT, node_id=2, timestamp=1.0,
        data={"fault_code": ServiceStatus.ActuatorTimeout},
    ))
    _down(runner, [1], 2.0)
    runner.step(now=3.0)
    assert len(_rows(runner, "peer_raise")) == 1
    _up(runner, [1], 4.0)
    outcomes = {e.node_id: e.fields["outcome"] for e in _rows(runner, "actuator_cycle")}
    assert outcomes == {1: "ok", 2: "fault"}
    assert 2 not in _recover_cmds(runner)  # left for the operator

    runner.step(now=60.0)
    assert _no_feed_cmds(runner)[-1:] == [1]
    assert _no_feed_cmds(runner).count(2) == 1
    assert _rows(runner, "actuator_nodes_skipped")[-1].fields["nodes"] == [2]

    # Operator recovers node 2: it rejoins on the next cycle.
    runner.recover_node(2, now=61.0)
    _down(runner, [1], 62.0)
    runner.step(now=63.0)
    _up(runner, [1], 64.0)
    runner.step(now=120.0)
    assert sorted(_no_feed_cmds(runner)[-2:]) == [1, 2]


def test_node_that_never_gets_down_is_recovered_and_the_rest_still_raise() -> None:
    runner = _start([1, 2], interval_min=5.0, hold_s=0.0)
    _down(runner, [1], 2.0)
    runner.step(now=MOTION_TIMEOUT_S + 1.0)
    assert len(_rows(runner, "peer_raise")) == 1
    _up(runner, [1], MOTION_TIMEOUT_S + 2.0)

    assert _recover_cmds(runner) == [2]
    stuck = _rows(runner, "actuator_stuck")
    assert [(e.node_id, e.fields["phase"]) for e in stuck] == [(2, "lowering")]
    outcomes = {e.node_id: e.fields["outcome"] for e in _rows(runner, "actuator_cycle")}
    assert outcomes == {1: "ok", 2: "stuck"}


def test_max_cycles_ends_the_session() -> None:
    runner = _start([1], interval_min=0.5, hold_s=0.0, max_cycles=2)
    for t in (0.0, 30.0):
        runner.step(now=t)
        _down(runner, [1], t + 1.0)
        _up(runner, [1], t + 2.0)
    runner.step(now=40.0)
    assert runner.is_finished
    assert runner.ctx.stop_reason == "cycle_cap_reached"
    assert len(_no_feed_cmds(runner)) == 2


def test_trigger_peer_raise_sends_a_raising_frame_from_an_unused_id() -> None:
    sent = []
    can = SimpleNamespace(send_raw=lambda arb, data: sent.append((arb, data)) or True)
    ctx = ExperimentControl(nodes=[1], can=can)
    assert ctx.trigger_peer_raise()
    assert sent == [(0x3FF, b"\x09")]


def test_schema_builds_the_template() -> None:
    defs = {d.name: d for d in load_experiment_defs(DEFAULT_EXPERIMENTS_DIR)}
    exp = build_experiment(
        defs["troubleshooting"],
        params={"interval_min": 2.0, "hold_s": 1.0, "max_cycles": 3},
        nodes=[4, 5],
    )
    assert exp.nodes == [4, 5]
