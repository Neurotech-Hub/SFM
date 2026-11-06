"""
BNC OUT: what it signals, one thing at a time, and pulsing straight from the
CAN receive thread so a node event doesn't wait for the engine's next tick.
"""

from __future__ import annotations

import argparse
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from base_station.can_manager import CanManager
from base_station.engine import BaseStationEngine
from base_station.io_manager import MAX_PULSE_US, IOManager
from base_station.log_manager import LogManager
from base_station.node_registry import NodeRegistry
from base_station.protocol import (
    CanCmd,
    CanEvent,
    DispenseState,
    HeartbeatPayload,
    InputId,
    ServiceStatus,
    build_event_frame,
    build_heartbeat_frame,
    parse_event,
)


class _FakeCan:
    def __init__(self) -> None:
        self.broadcasts: list = []

    def poll_rx(self):
        return []

    def send_command(self, node_id, cmd, payload=b""):
        return True

    def send_broadcast(self, cmd, payload=b""):
        self.broadcasts.append((cmd, payload))
        return True


class _FakeIo:
    """Records the pulses it is asked for. ``accept`` is what it answers."""

    def __init__(self) -> None:
        self.calls: list = []
        self.accept = True

    def pulse_bnc_out(self, width_us, purpose=""):
        self.calls.append((purpose, width_us))
        return self.accept

    def read_bnc_in1(self):
        return False

    read_bnc_in2 = read_bnc_in1


@pytest.fixture
def engine(tmp_path):
    e = BaseStationEngine(argparse.Namespace(interface="can0", bitrate=250000, nodes=2, log_dir=str(tmp_path)))
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


def _msg(frame):
    arb, data = frame
    return SimpleNamespace(arbitration_id=arb, data=bytes(data))


def _event(node, ev, extra=b""):
    return _msg(build_event_frame(node, ev, bytes(extra)))


def _start(engine, nodes=(1, 2)):
    started, message = engine.start_experiment(
        engine._default_experiment_def(), "out_test", lambda: ({}, list(nodes))
    )
    assert started, message


def _rows(engine, name):
    return [e for e in engine._log.all_entries() if e.event_name == name]


# ---------------------------------------------------------------------------
# Choosing a mapping
# ---------------------------------------------------------------------------

def test_the_default_mapping_is_the_original_behaviour(engine) -> None:
    assert engine._bnc_out_cfg.mapping == "legacy"


def test_a_mapping_is_applied_and_logged(engine) -> None:
    assert engine.set_bnc_out_mapping("trial", width_us=20_000) == ""
    cfg = engine._bnc_out_cfg
    assert (cfg.mapping, cfg.pulse_width_us, cfg.enabled) == ("trial", 20_000, True)
    row = _rows(engine, "BNC OUT Mapping")[0]
    assert row.fields["mapping"] == "trial" and row.fields["pulse_width_us"] == 20_000
    assert engine.set_bnc_out_mapping("off") == ""
    assert engine._bnc_out_cfg.enabled is False


@pytest.mark.parametrize("kwargs, expected", [
    ({"mapping": "nonsense"}, "Unknown BNC OUT mapping"),
    ({"mapping": "trial", "width_us": 0}, "Pulse width must be between"),
    ({"mapping": "trial", "width_us": MAX_PULSE_US + 1}, "Pulse width must be between"),
    ({"mapping": "session_start_sync", "sync_width_us": 0}, "Sync pulse width must be between"),
    ({"mapping": "node_event"}, "Choose a node event"),
    ({"mapping": "node_event", "trigger": "NotAnEvent"}, "Choose a node event"),
    ({"mapping": "periodic", "interval_s": 0.1}, "at least 0.5 s"),
    ({"mapping": "periodic", "interval_s": 1.0, "width_us": 2_000_000}, "shorter than the interval"),
])
def test_an_invalid_mapping_is_refused_and_changes_nothing(engine, kwargs, expected) -> None:
    before = (engine._bnc_out_cfg.mapping, engine._bnc_out_cfg.pulse_width_us, engine._bnc_out_cfg.trigger)
    message = engine.set_bnc_out_mapping(**kwargs)
    assert expected in message
    assert (engine._bnc_out_cfg.mapping, engine._bnc_out_cfg.pulse_width_us, engine._bnc_out_cfg.trigger) == before
    assert _rows(engine, "BNC OUT Mapping") == []


def test_the_mapping_is_locked_while_a_session_runs(engine) -> None:
    _start(engine)
    assert engine.set_bnc_out_mapping("trial") == "BNC OUT is locked while a session runs."
    assert engine._bnc_out_cfg.mapping == "legacy"


def test_node_events_can_be_chosen_by_name(engine) -> None:
    names = engine.bnc_out_trigger_names()
    assert names[0] == "any_event"
    for wanted in ("Dome Opened", "Dome closed", "Pellet Taken", "MousePresence Detected", "Loaded"):
        assert wanted in names
    assert engine.set_bnc_out_mapping("node_event", trigger="Dome Opened") == ""
    assert engine.set_bnc_out_mapping("node_event", trigger="dome_opened") == ""   # spelling variants match


# ---------------------------------------------------------------------------
# Session start
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mapping, expected", [
    ("legacy", [("session_start_sync", 500_000)]),
    ("session_start_sync", [("session_start_sync", 250_000)]),
    ("session_start_stop", [("session_start", 15_000)]),
    ("off", []),
    ("trial", [("trial_start", 15_000), ("trial_start", 15_000)]),   # free feeding: one trial per node
    ("node_event", []),
    ("periodic", [("periodic", 15_000)]),
])
def test_what_a_session_start_pulses(engine, mapping, expected) -> None:
    engine._bnc_out_cfg.mapping = mapping
    engine._bnc_out_cfg.pulse_width_us = 15_000
    engine._bnc_out_cfg.sync_width_us = 250_000
    engine._bnc_out_cfg.trigger = "Loaded"
    _start(engine)
    engine._tick_periodic(time.monotonic())
    assert engine._io.calls == expected


def test_the_led_flash_is_sent_whatever_the_mapping(engine) -> None:
    engine._bnc_out_cfg.mapping = "off"
    _start(engine)
    assert any(cmd == CanCmd.SyncFlash for cmd, _ in engine._can.broadcasts)
    sync = _rows(engine, "SyncFlash (broadcast)")[0]
    assert sync.details.startswith("500ms LED") and "BNC OUT" not in sync.details


def test_the_sync_row_still_mentions_the_pulse_when_there_is_one(engine) -> None:
    _start(engine)
    assert "500ms LED + BNC OUT" in _rows(engine, "SyncFlash (broadcast)")[0].details


# ---------------------------------------------------------------------------
# Session end and trials
# ---------------------------------------------------------------------------

def test_the_session_end_pulse_is_twice_as_wide_as_the_start(engine) -> None:
    engine._bnc_out_cfg.mapping = "session_start_stop"
    engine._bnc_out_cfg.pulse_width_us = 10_000
    _start(engine)
    engine.stop_experiment()
    assert engine._io.calls == [("session_start", 10_000), ("session_end", 20_000)]


def test_trials_pulse_and_the_end_closes_the_last_one(engine) -> None:
    engine._bnc_out_cfg.mapping = "trial"
    engine._bnc_out_cfg.pulse_width_us = 10_000
    _start(engine)
    engine._exp.runner.ctx.next_trial()
    engine.stop_experiment()
    assert engine._io.calls == [
        ("trial_start", 10_000), ("trial_start", 10_000), ("trial_start", 10_000), ("session_end", 20_000),
    ]


def test_other_mappings_ignore_trials_and_the_end(engine) -> None:
    engine._bnc_out_cfg.mapping = "off"
    engine._on_experiment_event("trial", {})
    engine._on_experiment_event("session_end", {})
    engine._bnc_out_cfg.mapping = "legacy"
    engine._on_experiment_event("trial", {})
    engine._on_experiment_event("session_end", {})
    assert engine._io.calls == []


# ---------------------------------------------------------------------------
# Periodic
# ---------------------------------------------------------------------------

def test_a_periodic_pulse_fires_at_the_start_and_then_every_interval(engine) -> None:
    engine._bnc_out_cfg.mapping = "periodic"
    engine._bnc_out_cfg.pulse_width_us = 5_000
    engine._bnc_out_cfg.periodic_interval_s = 10.0
    _start(engine)
    engine._io.calls.clear()

    engine._tick_periodic(100.0)
    engine._tick_periodic(105.0)
    engine._tick_periodic(110.0)
    engine._tick_periodic(119.0)
    engine._tick_periodic(120.0)
    assert engine._io.calls == [("periodic", 5_000)] * 3


def test_a_stalled_engine_does_not_fire_a_burst_of_catch_up_pulses(engine) -> None:
    engine._bnc_out_cfg.mapping = "periodic"
    engine._bnc_out_cfg.periodic_interval_s = 10.0
    _start(engine)
    engine._io.calls.clear()
    engine._tick_periodic(100.0)
    engine._tick_periodic(1000.0)           # nothing ticked for 15 minutes
    assert len(engine._io.calls) == 2
    engine._tick_periodic(1005.0)
    assert len(engine._io.calls) == 2       # the next one is a full interval later


def test_periodic_pulses_only_while_a_session_runs(engine) -> None:
    engine._bnc_out_cfg.mapping = "periodic"
    engine._tick_periodic(100.0)
    assert engine._io.calls == []
    assert engine._next_periodic is None


# ---------------------------------------------------------------------------
# The receive-thread fast path
# ---------------------------------------------------------------------------

def _follow(engine, trigger, width_us=7_000):
    engine._rx_tap_installed = True
    assert engine.set_bnc_out_mapping("node_event", trigger=trigger, width_us=width_us) == ""
    assert engine._fast_out is not None


def test_a_matching_event_pulses_from_the_receive_thread(engine) -> None:
    _follow(engine, "Dome Opened")
    engine._rx_tap(_event(1, CanEvent.DomeOpened, bytes([1, 0, 1])))
    assert engine._io.calls == [("event:Dome Opened", 7_000)]


def test_other_events_and_frames_do_not_pulse(engine) -> None:
    _follow(engine, "Dome Opened")
    engine._rx_tap(_event(1, CanEvent.Loaded, bytes([5, 0])))
    hb = _msg(build_heartbeat_frame(1, HeartbeatPayload(
        DispenseState.Idle, False, False, False, False, ServiceStatus.Ok)))
    engine._rx_tap(hb)
    engine._rx_tap(SimpleNamespace(arbitration_id=0x080, data=b"\x00" * 7))     # discovery
    assert engine._io.calls == []


def test_the_trigger_spelling_does_not_matter(engine) -> None:
    _follow(engine, "pellet_taken")
    engine._rx_tap(_event(2, CanEvent.PelletTaken, bytes([5, 0, 1])))
    assert engine._io.calls == [("event:Pellet Taken", 7_000)]


def test_any_event_matches_every_logged_event_but_not_suppressed_ones(engine) -> None:
    _follow(engine, "any_event")
    engine._rx_tap(_event(1, CanEvent.Lowering))
    engine._rx_tap(_event(2, CanEvent.Loaded, bytes([1, 0])))
    # A raw pellet-sensor edge is not logged, so it must not pulse either.
    engine._rx_tap(_event(1, CanEvent.InputChanged, bytes([InputId.Pellet, 1])))
    assert [p for p, _ in engine._io.calls] == ["event:Lowering", "event:Loaded"]


def test_input_events_use_their_behavioural_names(engine) -> None:
    _follow(engine, "MousePresence Detected")
    engine._rx_tap(_event(1, CanEvent.InputChanged, bytes([InputId.MousePresence, 1])))
    engine._rx_tap(_event(1, CanEvent.InputChanged, bytes([InputId.MousePresence, 0])))
    assert engine._io.calls == [("event:MousePresence Detected", 7_000)]


def test_an_event_from_node_zero_is_ignored(engine) -> None:
    _follow(engine, "any_event")
    engine._rx_tap(SimpleNamespace(arbitration_id=0x300, data=bytes([int(CanEvent.Loaded), 1, 0])))
    assert engine._io.calls == []


def test_a_refused_pulse_is_queued_for_the_engine_thread_to_log(engine) -> None:
    _follow(engine, "Loaded")
    engine._io.accept = False
    engine._rx_tap(_event(1, CanEvent.Loaded, bytes([1, 0])))
    assert _rows(engine, "BNC OUT pulse refused") == []          # the receive thread never logs
    engine._log_finished_pulses()
    row = _rows(engine, "BNC OUT pulse refused")[0]
    assert row.fields == {"purpose": "event:Loaded", "requested_us": 7_000}
    assert row.source == "BNC"


def test_one_event_makes_one_pulse_not_two(engine) -> None:
    """The receive thread pulses; the engine thread, handling the same frame, must not."""
    _follow(engine, "Loaded")
    frame = _event(1, CanEvent.Loaded, bytes([1, 0]))
    engine._rx_tap(frame)
    engine._dispatch_rx(frame)
    assert engine._io.calls == [("event:Loaded", 7_000)]


def test_without_the_tap_the_engine_thread_does_the_pulse(engine) -> None:
    assert engine.set_bnc_out_mapping("node_event", trigger="Loaded", width_us=7_000) == ""
    assert engine._fast_out is None                       # no tap installed
    engine._dispatch_rx(_event(1, CanEvent.Loaded, bytes([1, 0])))
    assert engine._io.calls == [("event:Loaded", 7_000)]


def test_the_original_trigger_still_works_in_legacy_mode(engine) -> None:
    engine._bnc_out_cfg.enabled = True
    engine._bnc_out_cfg.trigger = "Loaded"
    engine._bnc_out_cfg.pulse_width_us = 4_000
    engine._dispatch_rx(_event(1, CanEvent.Loaded, bytes([1, 0])))
    assert engine._io.calls == [("event:Loaded", 4_000)]
    engine._rx_tap(_event(1, CanEvent.Loaded, bytes([1, 0])))     # the tap does not act in legacy mode
    assert len(engine._io.calls) == 1


def test_changing_to_another_mapping_switches_the_fast_path_off(engine) -> None:
    _follow(engine, "Loaded")
    assert engine.set_bnc_out_mapping("off") == ""
    assert engine._fast_out is None


def test_the_fast_path_pulses_without_any_tick(tmp_path) -> None:
    """With the real pulse thread: the pulse is made with no engine tick at all."""
    engine = BaseStationEngine(argparse.Namespace(interface="can0", bitrate=1, nodes=1, log_dir=str(tmp_path)))
    engine._registry = NodeRegistry(2)
    engine._log = LogManager(auto_save=False)
    writes, done = [], threading.Event()
    engine._io._write_output = lambda pin, high: writes.append(high)
    engine._io.on_bnc_pulse(lambda rec: done.set())
    _follow(engine, "Loaded", width_us=5_000)

    engine._rx_tap(_event(1, CanEvent.Loaded, bytes([1, 0])))
    assert done.wait(2.0)
    engine._io.shutdown()
    assert writes[:2] == [True, False]


# ---------------------------------------------------------------------------
# The fast path names events exactly as the engine thread logs them
# ---------------------------------------------------------------------------

_EXTRA = bytes([5, 0, 1, 1, 1, 1, 1, 1])


def _logged_name(engine, node, ev, extra):
    before = len(engine._log.all_entries())
    engine._dispatch_rx(_event(node, ev, extra))
    rows = [e for e in engine._log.all_entries()[before:] if e.frame_type == "EVENT"]
    return rows[-1].event_name if rows else None


def _fast_name(engine, node, ev, extra):
    parsed = parse_event(bytes(build_event_frame(node, ev, bytes(extra))[1]))
    return engine._event_match_name(node, parsed)


@pytest.mark.parametrize("ev", [e for e in CanEvent if e != CanEvent.InputChanged])
def test_every_event_is_named_the_same_in_both_places(engine, ev) -> None:
    extra = bytes([ServiceStatus.Jam]) if ev == CanEvent.Fault else (bytes(6) if ev == CanEvent.Pong else _EXTRA)
    assert _fast_name(engine, 1, ev, extra) == _logged_name(engine, 1, ev, extra)


@pytest.mark.parametrize("input_id", list(InputId))
@pytest.mark.parametrize("active", [0, 1])
@pytest.mark.parametrize("state", [DispenseState.Idle, DispenseState.Loaded, DispenseState.Fault])
def test_input_changes_are_named_the_same_in_both_places(engine, input_id, active, state) -> None:
    extra = bytes([input_id, active])
    engine._registry.get(1).dispense_state = state
    fast = _fast_name(engine, 1, CanEvent.InputChanged, extra)
    engine._registry.get(1).dispense_state = state     # the slow path reads it before applying the input
    assert fast == _logged_name(engine, 1, CanEvent.InputChanged, extra)


def test_a_malformed_input_event_is_named_the_same_in_both_places(engine) -> None:
    assert _fast_name(engine, 1, CanEvent.InputChanged, b"\x09") == \
        _logged_name(engine, 1, CanEvent.InputChanged, b"\x09") == "Invalid Input Event"


# ---------------------------------------------------------------------------
# CanManager tap and connect
# ---------------------------------------------------------------------------

class _OneShotBus:
    def __init__(self, messages) -> None:
        self.messages = list(messages)

    def recv(self, timeout=None):
        return self.messages.pop(0) if self.messages else None


def _run_rx_loop(manager, count):
    manager._running = True
    thread = threading.Thread(target=manager._rx_loop, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2.0
    while manager._rx_queue.qsize() < count and time.monotonic() < deadline:
        time.sleep(0.001)
    manager._running = False
    thread.join(timeout=2.0)


def test_the_rx_tap_sees_each_frame_before_it_is_queued() -> None:
    manager = CanManager("vcan0")
    manager._bus = _OneShotBus(["a", "b"])
    order = []
    manager.set_rx_tap(lambda m: order.append(("tap", m, manager._rx_queue.qsize())))
    _run_rx_loop(manager, 2)
    assert [(k, m, q) for k, m, q in order] == [("tap", "a", 0), ("tap", "b", 1)]
    assert manager.poll_rx() == ["a", "b"]


def test_a_failing_rx_tap_never_costs_a_frame() -> None:
    manager = CanManager("vcan0")
    manager._bus = _OneShotBus(["a", "b"])
    manager.set_rx_tap(lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
    _run_rx_loop(manager, 2)
    assert manager.poll_rx() == ["a", "b"]


def test_connect_installs_the_tap_and_arms_the_fast_path(monkeypatch, tmp_path) -> None:
    engine = BaseStationEngine(argparse.Namespace(interface="can0", bitrate=1, nodes=2, log_dir=str(tmp_path)))
    taps = []
    fake = SimpleNamespace(start=lambda: None, set_rx_tap=taps.append, poll_rx=lambda: [])
    monkeypatch.setattr("base_station.engine.CanManager", lambda **_: fake)
    monkeypatch.setattr(engine._io, "begin", lambda: None)
    monkeypatch.setattr("base_station.engine.DiscoveryManager", lambda *a, **k: SimpleNamespace(
        on_node_discovered=lambda cb: None, on_complete=lambda cb: None, start=lambda start_id=1: None,
    ))
    engine._bnc_out_cfg.mapping = "node_event"
    engine._bnc_out_cfg.trigger = "Loaded"

    assert engine.connect("can0", 250000, 2, str(tmp_path), False) == ""

    assert taps == [engine._rx_tap]
    assert engine._fast_out is not None
    engine._log.close()
