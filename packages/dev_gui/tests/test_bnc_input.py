"""
BNC inputs: pulse and level modes, and starting a session from an external signal.

A pulse from other equipment can start a real session with real dispensing, so
the rules are tested one by one: nothing starts unless it was armed by hand,
only once, and never when a node is offline or in fault.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from base_station.engine import BaseStationEngine
from base_station.io_manager import BncEdgeEvent
from base_station.log_manager import LogManager
from base_station.node_registry import NodeRegistry
from base_station.protocol import CanCmd, DispenseState

T0 = 1_760_000_000.0


class _FakeCan:
    def __init__(self) -> None:
        self.commands: list = []
        self.broadcasts: list = []

    def poll_rx(self):
        return []

    def send_command(self, node_id, cmd, payload=b""):
        self.commands.append((node_id, cmd, payload))
        return True

    def send_broadcast(self, cmd, payload=b""):
        self.broadcasts.append((cmd, payload))
        return True


class _FakeIo:
    def __init__(self, high: bool = False) -> None:
        self.high = high

    def read_bnc_in1(self):
        return self.high

    def read_bnc_in2(self):
        return self.high

    def pulse_bnc_out(self, width_us, purpose=""):
        return True


class _Engine(BaseStationEngine):
    def __init__(self, args) -> None:
        super().__init__(args)
        self.notices: list = []

    def _ui_notice(self, level, text):
        self.notices.append((level, text))


@pytest.fixture
def engine(tmp_path):
    e = _Engine(argparse.Namespace(interface="can0", bitrate=250000, nodes=2, log_dir=str(tmp_path)))
    e._can = _FakeCan()
    e._io = _FakeIo()
    e._registry = NodeRegistry(3)
    for n in (1, 2, 3):
        e._registry.get(n).online = True
    e._log = LogManager(log_dir=str(tmp_path), auto_save=True)
    e._exp_log_dir = str(tmp_path)
    yield e
    if e._exp.is_running:
        e.stop_experiment()
    e._log.close()


def _edge(rising: bool, t: float, which: str = "IN1") -> BncEdgeEvent:
    return BncEdgeEvent(
        channel=0 if which == "IN1" else 1, which=which, rising=rising,
        t_mono=t, t_wall=T0 + t,
    )


def _pulse(engine, start: float, width_s: float, which: str = "IN1", active_high: bool = True):
    engine._handle_bnc_event(_edge(active_high, start, which))
    engine._handle_bnc_event(_edge(not active_high, start + width_s, which))


def _configure(engine, **kw):
    cfg = engine._bnc_in1_cfg
    cfg.enabled = True
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


def _rows(engine, name):
    return [e for e in engine._log.all_entries() if e.event_name == name]


def _collect(nodes=(1, 2), params=None):
    return lambda: (dict(params or {}), list(nodes))


def _arm(engine, nodes=(1, 2), name="pulse_run"):
    return engine.arm_start(engine._default_experiment_def(), name, _collect(nodes))


# ---------------------------------------------------------------------------
# Pulse mode
# ---------------------------------------------------------------------------

def test_a_wide_enough_pulse_runs_its_action(engine) -> None:
    _configure(engine, mode="pulse", action="ping_all", min_width_ms=5.0)
    _pulse(engine, 10.000, 0.010)

    assert engine._can.broadcasts == [(CanCmd.Ping, b"")]
    row = _rows(engine, "IN1 pulse")[0]
    assert row.timestamp == T0 + 10.000            # when the pulse began
    assert row.fields["width_ms"] == pytest.approx(10.0, abs=1e-6)
    assert row.fields["accepted"] is True and row.source == "BNC"


def test_a_ten_ms_pulse_is_measured_from_its_edge_times_not_when_it_was_read(engine) -> None:
    """Both edges are handled back to back, as when the engine was busy. The width is still 10 ms."""
    _configure(engine, mode="pulse", action="ping_all", min_width_ms=8.0)
    _pulse(engine, 3.0, 0.010)
    assert len(_rows(engine, "IN1 pulse")) == 1


def test_a_glitch_shorter_than_the_minimum_is_rejected_and_logged(engine) -> None:
    _configure(engine, mode="pulse", action="ping_all", min_width_ms=5.0)
    _pulse(engine, 10.0, 0.001)

    assert engine._can.broadcasts == []
    rejected = _rows(engine, "IN1 pulse rejected")
    assert len(rejected) == 1 and rejected[0].fields["accepted"] is False
    assert _rows(engine, "IN1 pulse") == []


def test_an_active_low_input_treats_low_as_the_signal(engine) -> None:
    _configure(engine, mode="pulse", action="ping_all", min_width_ms=5.0, active_high=False)
    _pulse(engine, 1.0, 0.020, active_high=False)
    assert engine._can.broadcasts == [(CanCmd.Ping, b"")]
    assert _rows(engine, "IN1 pulse")[0].fields["active_high"] is False


def test_a_signal_that_ends_without_us_seeing_it_start_is_ignored(engine) -> None:
    _configure(engine, mode="pulse", action="ping_all", min_width_ms=1.0)
    engine._handle_bnc_event(_edge(False, 1.0))      # a falling edge with no rising edge before it
    assert engine._can.broadcasts == [] and _rows(engine, "IN1 pulse") == []


def test_the_two_inputs_keep_their_own_pulses(engine) -> None:
    _configure(engine, mode="pulse", action="ping_all", min_width_ms=1.0)
    engine._bnc_in2_cfg.enabled = True
    engine._bnc_in2_cfg.mode = "pulse"
    engine._bnc_in2_cfg.action = "recover_all"
    engine._handle_bnc_event(_edge(True, 1.000, "IN1"))
    engine._handle_bnc_event(_edge(True, 1.002, "IN2"))
    engine._handle_bnc_event(_edge(False, 1.010, "IN1"))
    engine._handle_bnc_event(_edge(False, 1.020, "IN2"))
    assert engine._can.broadcasts == [(CanCmd.Ping, b""), (CanCmd.Recover, b"")]


# ---------------------------------------------------------------------------
# The other modes
# ---------------------------------------------------------------------------

def test_edge_mode_still_acts_on_each_edge_at_once(engine) -> None:
    _configure(engine, rising_action="ping_all", falling_action="recover_all")
    assert engine._bnc_in1_cfg.mode == "edge"
    engine._handle_bnc_event(_edge(True, 1.0))
    assert engine._can.broadcasts == [(CanCmd.Ping, b"")]
    engine._handle_bnc_event(_edge(False, 1.01))
    assert engine._can.broadcasts[-1] == (CanCmd.Recover, b"")


def test_log_only_mode_logs_edges_and_does_nothing_else(engine) -> None:
    _configure(engine, mode="log_only", rising_action="ping_all", action="ping_all")
    _pulse(engine, 1.0, 0.05)
    assert engine._can.broadcasts == []
    assert [r.event_name for r in engine._log.all_entries() if r.frame_type == "BNC"] == [
        "IN1 rising", "IN1 falling"
    ]


def test_a_disabled_input_logs_but_does_nothing(engine) -> None:
    _configure(engine, mode="pulse", action="ping_all", min_width_ms=1.0)
    engine._bnc_in1_cfg.enabled = False
    _pulse(engine, 1.0, 0.05)
    assert engine._can.broadcasts == []
    assert len([r for r in engine._log.all_entries() if r.frame_type == "BNC"]) == 2


def test_an_unknown_mode_falls_back_to_edge_behaviour(engine) -> None:
    _configure(engine, mode="nonsense", rising_action="ping_all")
    engine._handle_bnc_event(_edge(True, 1.0))
    assert engine._can.broadcasts == [(CanCmd.Ping, b"")]


def test_level_mode_runs_one_action_going_active_and_another_going_inactive(engine) -> None:
    _configure(engine, mode="level", action="ping_all", release_action="recover_all")
    engine._handle_bnc_event(_edge(True, 1.0))
    engine._handle_bnc_event(_edge(False, 1.5))
    assert engine._can.broadcasts == [(CanCmd.Ping, b""), (CanCmd.Recover, b"")]


# ---------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------

def test_nothing_is_armed_at_launch(engine) -> None:
    assert engine.start_armed is False
    assert _Engine(argparse.Namespace(interface="can0", bitrate=1, nodes=1, log_dir="")).start_armed is False


def test_arming_needs_a_bus_a_name_nodes_and_a_start_input(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment")
    exp_def = engine._default_experiment_def()

    assert engine.arm_start(exp_def, "  ", _collect()) == "Enter a session name before arming."
    assert engine.arm_start(exp_def, "ok", _collect(nodes=())) == "Enable at least one node to run."
    engine._bnc_in1_cfg.action = "ping_all"
    assert engine.arm_start(exp_def, "ok", _collect()) == "No enabled BNC input is set to start an experiment."
    engine._bnc_in1_cfg.action = "start_experiment"
    engine._bnc_in1_cfg.enabled = False
    assert "No enabled BNC input" in engine.arm_start(exp_def, "ok", _collect())
    engine._can = None
    assert engine.arm_start(exp_def, "ok", _collect()) == "Connect to the bus first."
    assert engine.start_armed is False


def test_arming_checks_that_the_experiment_can_be_built(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment")
    bandit = next(d for d in engine._exp_defs if d.name == "two_armed_bandit")
    message = engine.arm_start(bandit, "ok", _collect(nodes=(1, 2, 3)))
    assert "exactly 2 nodes" in message
    assert engine.start_armed is False
    assert engine.arm_start(bandit, "ok", _collect(nodes=(1, 2))) == ""


def test_arming_is_refused_while_a_level_input_is_already_active(engine) -> None:
    _configure(engine, mode="level", action="start_experiment", label="BNC IN 0")
    engine._io.high = True
    message = _arm(engine)
    assert "already active" in message and "BNC IN 0" in message
    engine._io.high = False
    assert _arm(engine) == ""


def test_arming_is_refused_while_a_session_runs(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment")
    assert _arm(engine) == ""
    _pulse(engine, 1.0, 0.01)
    assert engine._exp.is_running
    assert _arm(engine) == "A session is already running."


def test_arming_and_disarming_are_logged(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment")
    assert _arm(engine) == ""
    assert engine.start_armed and len(_rows(engine, "BNC Armed")) == 1
    engine.disarm_start()
    assert not engine.start_armed and len(_rows(engine, "BNC Disarmed")) == 1


# ---------------------------------------------------------------------------
# Starting from a pulse
# ---------------------------------------------------------------------------

def test_a_pulse_starts_the_armed_session_once(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    assert _arm(engine) == ""

    _pulse(engine, 20.0, 0.010)

    assert engine._exp.is_running
    assert engine._log.is_named_session
    assert engine.start_armed is False                       # used up
    assert len(_rows(engine, "BNC Start")) == 1
    assert engine.notices[-1] == ("info", "Session started by IN1.")


def test_an_unarmed_pulse_is_rejected_visibly(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    _pulse(engine, 20.0, 0.010)

    assert not engine._exp.is_running
    row = _rows(engine, "BNC Start Rejected")[0]
    assert row.fields == {"input": "IN1", "reason": "start is not armed"}
    assert engine.last_notice == ("warning", "BNC start rejected (IN1): start is not armed.")
    assert engine.notices[-1] == engine.last_notice


def test_a_start_pulse_during_a_run_is_ignored_and_logged(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    _arm(engine)
    _pulse(engine, 20.0, 0.010)
    assert engine._exp.is_running
    session = engine._log.session_name

    _pulse(engine, 30.0, 0.010)

    assert engine._exp.is_running and engine._log.session_name == session
    rejected = _rows(engine, "BNC Start Rejected")
    assert rejected[-1].fields["reason"] == "a session is already running"
    assert engine.notices[-1][0] == "info"              # not an alarm: nothing went wrong


def test_the_next_pulse_after_a_run_needs_arming_again(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    _arm(engine)
    _pulse(engine, 20.0, 0.010)
    engine.stop_experiment()
    assert engine.start_armed is False

    _pulse(engine, 40.0, 0.010)

    assert not engine._exp.is_running
    assert _rows(engine, "BNC Start Rejected")[-1].fields["reason"] == "start is not armed"


def test_stopping_a_session_withdraws_an_arming(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment")
    _arm(engine)
    engine.stop_experiment()
    assert engine.start_armed is False


def test_an_offline_node_blocks_the_start_and_leaves_it_armed(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    _arm(engine)
    engine._registry.get(2).online = False

    _pulse(engine, 20.0, 0.010)

    assert not engine._exp.is_running
    assert engine._log.is_named_session is False             # no session file was opened
    assert "node(s) [2] offline" in _rows(engine, "BNC Start Rejected")[0].fields["reason"]
    assert engine.start_armed is True

    engine._registry.get(2).online = True
    _pulse(engine, 30.0, 0.010)                               # the next pulse goes through
    assert engine._exp.is_running


def test_a_node_in_fault_blocks_the_start(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    _arm(engine)
    engine._registry.get(1).dispense_state = DispenseState.Fault
    _pulse(engine, 20.0, 0.010)
    assert not engine._exp.is_running
    assert "node(s) [1] in fault" in _rows(engine, "BNC Start Rejected")[0].fields["reason"]


def test_a_node_outside_the_task_does_not_block(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    _arm(engine, nodes=(1, 2))
    engine._registry.get(3).online = False                    # node 3 isn't in this session
    _pulse(engine, 20.0, 0.010)
    assert engine._exp.is_running


def test_re_discover_in_progress_blocks_the_start(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    _arm(engine)
    engine._discovery = SimpleNamespace(is_reassigning=True)
    _pulse(engine, 20.0, 0.010)
    assert not engine._exp.is_running
    assert "Re-discover" in _rows(engine, "BNC Start Rejected")[0].fields["reason"]


def test_several_problems_are_all_reported(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    _arm(engine)
    engine._registry.get(1).online = False
    engine._registry.get(2).dispense_state = DispenseState.Fault
    _pulse(engine, 20.0, 0.010)
    reason = _rows(engine, "BNC Start Rejected")[0].fields["reason"]
    assert "offline" in reason and "in fault" in reason


def test_a_glitch_never_reaches_the_start(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    _arm(engine)
    _pulse(engine, 20.0, 0.001)
    assert not engine._exp.is_running and engine.start_armed is True
    assert _rows(engine, "BNC Start Rejected") == []        # it was noise, not a request


def test_the_starting_pulse_does_not_count_as_a_trial_pulse(engine) -> None:
    _configure(engine, mode="pulse", action="start_experiment", min_width_ms=5.0)
    forwarded: list = []
    real = engine._exp.forward_bnc
    engine._exp.forward_bnc = lambda *a, **k: (forwarded.append((a, k)), real(*a, **k))[1]
    _arm(engine)

    _pulse(engine, 20.0, 0.010)                  # starts the session
    assert engine._exp.is_running and forwarded == []

    _pulse(engine, 30.0, 0.010)                  # a later pulse is an ordinary BNC input
    assert len(forwarded) == 2


# ---------------------------------------------------------------------------
# Level mode start and stop
# ---------------------------------------------------------------------------

def test_a_level_input_starts_when_active_and_stops_when_released(engine) -> None:
    _configure(engine, mode="level", action="start_experiment", release_action="stop_experiment")
    assert _arm(engine) == ""

    engine._handle_bnc_event(_edge(True, 5.0))
    assert engine._exp.is_running

    engine._handle_bnc_event(_edge(False, 65.0))
    assert not engine._exp.is_running
    assert engine.start_armed is False


def test_releasing_a_level_input_when_nothing_runs_does_nothing(engine) -> None:
    _configure(engine, mode="level", action="start_experiment", release_action="stop_experiment")
    engine._handle_bnc_event(_edge(False, 5.0))
    assert not engine._exp.is_running and _rows(engine, "BNC Start Rejected") == []


# ---------------------------------------------------------------------------
# Legacy "edge" mode with a start action
# ---------------------------------------------------------------------------

def test_edge_mode_start_uses_the_armed_session_when_no_front_end_handles_it(engine) -> None:
    _configure(engine, rising_action="start_experiment")
    engine._handle_bnc_event(_edge(True, 5.0))
    assert not engine._exp.is_running                   # not armed, so refused
    assert _rows(engine, "BNC Start Rejected")

    _arm(engine)
    engine._handle_bnc_event(_edge(True, 6.0))
    assert engine._exp.is_running
