"""
The base-station engine must work with no GUI at all.

Everything here runs without dearpygui, so these tests run in CI. They cover
the seam between the engine and its front-ends: the entry points a front-end
calls, and the hooks it overrides to see what changed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from base_station.engine import BaseStationEngine
from base_station.io_manager import BncEdgeEvent, PulseRecord
from base_station.log_manager import LogManager
from base_station.node_registry import NodeRegistry
from base_station.protocol import (
    CanCmd,
    CanEvent,
    build_event_frame,
)

DEV_GUI_DIR = Path(__file__).resolve().parents[1]


def _args(nodes: int = 2) -> argparse.Namespace:
    return argparse.Namespace(
        interface="can0", bitrate=250000, nodes=nodes, log_dir="~/sfm_logs"
    )


def _msg(arb_id: int, data: bytes):
    return SimpleNamespace(arbitration_id=arb_id, data=bytes(data))


def _loaded_frame(node: int = 1):
    arb, data = build_event_frame(node, CanEvent.Loaded, bytes([0x67, 0x01]))
    return _msg(arb, data)


class _FakeCan:
    """CanManager stand-in: hands out queued frames and records what is sent."""

    def __init__(self, frames=()) -> None:
        self.frames = list(frames)
        self.commands: list = []
        self.broadcasts: list = []

    def poll_rx(self):
        out, self.frames = self.frames, []
        return out

    def send_command(self, node_id, cmd, payload=b""):
        self.commands.append((node_id, cmd, payload))
        return True

    def send_broadcast(self, cmd, payload=b""):
        self.broadcasts.append((cmd, payload))
        return True

    def send_assign(self, mac, new_id):
        self.commands.append(("assign", mac, new_id))
        return True


class _RecordingEngine(BaseStationEngine):
    """Engine whose front-end hooks just remember that they were called."""

    def __init__(self, args) -> None:
        super().__init__(args)
        self.seen: list = []

    def _refresh_tile(self, node_id):
        self.seen.append(("tile", node_id))

    def _refresh_all_tiles(self):
        self.seen.append(("all_tiles",))

    def _refresh_log_table(self, *_):
        self.seen.append(("log_table",))

    def _refresh_pellet_counters(self):
        self.seen.append(("pellets",))

    def _ui_bnc_edge(self, which, edge, ts):
        self.seen.append(("bnc_edge", which, edge))

    def _ui_bnc_out_pulsed(self):
        self.seen.append(("bnc_out",))


@pytest.fixture
def engine():
    e = _RecordingEngine(_args())
    e._registry = NodeRegistry(2)
    e._log = LogManager(auto_save=False)
    return e


# ---------------------------------------------------------------------------
# No GUI toolkit anywhere
# ---------------------------------------------------------------------------

def test_engine_imports_without_dearpygui() -> None:
    """A headless host must be able to import the engine on a machine with no dearpygui."""
    code = (
        "import sys\n"
        "sys.modules['dearpygui'] = None\n"
        "sys.modules['dearpygui.dearpygui'] = None\n"
        "import base_station.engine as engine\n"
        "assert 'base_station.app' not in sys.modules\n"
        "engine.BaseStationEngine\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=DEV_GUI_DIR, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_engine_source_does_not_mention_the_gui_toolkit() -> None:
    source = (DEV_GUI_DIR / "base_station" / "engine.py").read_text(encoding="utf-8")
    assert "dearpygui" not in source
    assert "dpg." not in source


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def test_tick_without_can_does_nothing(engine) -> None:
    engine.tick()
    assert engine.seen == []


def test_tick_drains_can_logs_and_tells_the_front_end(engine) -> None:
    engine._can = _FakeCan([_loaded_frame(1)])
    engine.tick()

    rows = [e for e in engine._log.all_entries() if e.event_name == "Loaded"]
    assert len(rows) == 1
    assert rows[0].fields["session_pellets"] == 1
    # A new non-heartbeat row reached the log, so the front-end is told.
    assert ("log_table",) in engine.seen
    # The first tick also runs the periodic sweeps.
    assert ("all_tiles",) in engine.seen
    assert ("pellets",) in engine.seen


def test_tick_tells_the_log_table_only_when_something_changed(engine) -> None:
    engine._can = _FakeCan([_loaded_frame(1)])
    engine.tick()
    engine.seen.clear()
    engine.tick()  # nothing new arrived
    assert ("log_table",) not in engine.seen


def test_dispatch_drives_the_tile_hook(engine) -> None:
    engine._dispatch_rx(_loaded_frame(1))
    assert ("tile", 1) in engine.seen


# ---------------------------------------------------------------------------
# Starting and stopping an experiment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["", "   ", "***", "!!! ??? ---"])
def test_start_rejects_a_missing_session_name_without_reading_the_form(engine, name) -> None:
    engine._can = _FakeCan()
    exp_def = engine._default_experiment_def()
    collected = []

    started, message = engine.start_experiment(
        exp_def, name, lambda: collected.append("read") or ({}, [1, 2])
    )

    assert (started, message) == (False, "Enter a session name before starting.")
    assert collected == []
    errors = [e for e in engine._log.all_entries() if e.frame_type == "ERROR"]
    assert [e.event_name for e in errors] == ["ExperimentStartRejected"]
    assert not engine._exp.is_running


def test_start_with_no_nodes_opens_no_session(engine, tmp_path) -> None:
    engine._can = _FakeCan()
    engine._exp_log_dir = str(tmp_path)
    started, message = engine.start_experiment(
        engine._default_experiment_def(), "unit_test", lambda: ({}, [])
    )
    assert (started, message) == (False, "Enable at least one node to run.")
    assert not engine._log.is_named_session
    assert list(tmp_path.iterdir()) == []


def test_start_is_ignored_without_a_bus(engine) -> None:
    assert engine.start_experiment(
        engine._default_experiment_def(), "unit_test", lambda: ({}, [1])
    ) == (False, "")
    engine._can = _FakeCan()
    engine._registry = None
    assert engine.start_experiment(
        engine._default_experiment_def(), "unit_test", lambda: ({}, [1])
    ) == (False, "")


def test_start_then_stop_a_session(engine, tmp_path) -> None:
    engine._can = _FakeCan()
    engine._log = LogManager(log_dir=str(tmp_path), auto_save=True)
    engine._exp_log_dir = str(tmp_path)
    exp_def = engine._default_experiment_def()

    started, message = engine.start_experiment(exp_def, "unit_test", lambda: ({}, [1, 2]))
    try:
        assert (started, message) == (True, "")
        assert engine._exp.is_running
        assert engine._pellet_run_nodes == [1, 2]
        assert engine._log.is_named_session
        # Starting the heartbeat policy and the sync marker both go out on the bus.
        assert any(cmd == CanCmd.SetConfig for cmd, _ in engine._can.broadcasts)

        # A second request while running is ignored.
        assert engine.start_experiment(exp_def, "unit_test", lambda: ({}, [1, 2])) == (False, "")
    finally:
        engine.stop_experiment()
        engine._log.close()

    assert not engine._exp.is_running
    assert engine._pellet_run_nodes == []
    assert ("pellets",) in engine.seen  # live read before the runner goes away
    assert not engine._log.is_named_session


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------

def test_connect_rejects_an_empty_interface(engine) -> None:
    assert engine.connect("  ", 250000, 2, "", True) == "Interface name cannot be empty."
    assert engine._can is None


def test_connect_rejects_an_unusable_log_directory(engine, tmp_path) -> None:
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("x")
    error = engine.connect("can0", 250000, 2, str(not_a_directory), True)
    assert error.startswith("Log directory issue:")
    assert engine._can is None


def test_connect_reports_a_can_failure_and_leaves_nothing_open(monkeypatch, tmp_path) -> None:
    engine = _RecordingEngine(_args())

    class _BrokenCan:
        def __init__(self, **_):
            raise OSError("no such device")

    monkeypatch.setattr("base_station.engine.CanManager", _BrokenCan)
    began = []
    monkeypatch.setattr(engine._io, "begin", lambda: began.append(True))

    error = engine.connect("can9", 250000, 2, str(tmp_path), True)

    assert error == "CAN error: no such device"
    assert began == []
    assert engine._registry is None
    assert engine._log is None


def test_connect_builds_the_subsystems(monkeypatch, tmp_path) -> None:
    engine = _RecordingEngine(_args())
    fake = _FakeCan()
    fake.start = lambda: None
    monkeypatch.setattr("base_station.engine.CanManager", lambda **_: fake)
    monkeypatch.setattr(engine._io, "begin", lambda: None)
    monkeypatch.setattr(engine._io, "shutdown", lambda: None)
    monkeypatch.setattr("base_station.engine.DiscoveryManager", lambda *a, **k: SimpleNamespace(
        on_node_discovered=lambda cb: None, on_complete=lambda cb: None,
        start=lambda start_id=1: None, stop=lambda: None, tick=lambda: None,
    ))

    assert engine.connect("can0", 250000, 3, str(tmp_path), False) == ""

    assert engine._can is fake
    assert engine._registry.num_nodes() == 3
    assert engine._log is not None
    assert engine._exp_log_dir == str(tmp_path)
    engine._log.close()


# ---------------------------------------------------------------------------
# BNC
# ---------------------------------------------------------------------------

class _FakeIo:
    def __init__(self, high: bool) -> None:
        self.high = high
        self.pulses: list = []
        self.purposes: list = []
        self.accept = True

    def read_bnc_in1(self):
        return self.high

    def read_bnc_in2(self):
        return self.high

    def pulse_bnc_out(self, width_us, purpose=""):
        self.pulses.append(width_us)
        self.purposes.append(purpose)
        return self.accept


def test_bnc_edge_is_logged_and_shown(engine) -> None:
    engine._io = _FakeIo(high=True)
    engine._handle_bnc_edge("IN1", 1234.5)

    rows = [e for e in engine._log.all_entries() if e.frame_type == "BNC"]
    assert [(r.event_name, r.timestamp) for r in rows] == [("IN1 rising", 1234.5)]
    assert ("bnc_edge", "IN1", "rising") in engine.seen

    engine._io.high = False
    engine._handle_bnc_edge("IN2", 1235.0)
    assert ("bnc_edge", "IN2", "falling") in engine.seen


def test_bnc_input_action_runs_a_broadcast(engine) -> None:
    engine._can = _FakeCan()
    engine._io = _FakeIo(high=True)
    engine._bnc_in1_cfg.enabled = True
    engine._bnc_in1_cfg.rising_action = "ping_all"
    engine._handle_bnc_edge("IN1", 1.0)
    assert engine._can.broadcasts == [(CanCmd.Ping, b"")]


def test_bnc_out_fires_on_a_matching_event_only(engine) -> None:
    engine._io = _FakeIo(high=False)
    engine._bnc_out_cfg.enabled = True
    engine._bnc_out_cfg.trigger = "Pellet Taken"
    engine._bnc_out_cfg.pulse_width_us = 5000

    engine._maybe_fire_bnc_out("Loaded")
    assert engine._io.pulses == []

    engine._maybe_fire_bnc_out("Pellet Taken")
    assert engine._io.pulses == [5000]
    assert engine._io.purposes == ["event:Pellet Taken"]
    assert ("bnc_out",) in engine.seen


def test_manual_pulse_ignores_enabled(engine) -> None:
    engine._io = _FakeIo(high=False)
    engine._bnc_out_cfg.enabled = False
    engine._bnc_out_cfg.pulse_width_us = 7000
    engine.manual_bnc_pulse()
    assert engine._io.pulses == [7000]
    assert engine._io.purposes == ["manual"]
    assert ("bnc_out",) in engine.seen


def _record(purpose="event:Dome Opened", requested_us=10_000, width_us=10_050.0, latency_us=120.0):
    return PulseRecord(
        purpose=purpose, requested_us=requested_us, t_request=100.0,
        t_rise=100.0 + latency_us / 1e6, t_fall=100.0 + (latency_us + width_us) / 1e6,
        wall_rise=1_760_000_000.5,
    )


def test_a_finished_pulse_is_logged_once_with_what_it_actually_did(engine) -> None:
    engine._pulse_records.put(_record())
    engine._log_finished_pulses()
    rows = [e for e in engine._log.all_entries() if e.event_name == "BNC OUT pulse"]
    assert len(rows) == 1
    row = rows[0]
    assert row.timestamp == 1_760_000_000.5          # when the line went high
    assert row.source == "BNC"
    assert row.fields["purpose"] == "event:Dome Opened"
    assert row.fields["width_us"] == pytest.approx(10_050.0, abs=1)
    assert row.fields["latency_us"] == pytest.approx(120.0, abs=1)
    assert "measured=10.050ms" in row.details
    engine._log_finished_pulses()                    # nothing new to write
    assert len([e for e in engine._log.all_entries() if e.event_name == "BNC OUT pulse"]) == 1


def test_a_refused_pulse_is_logged_and_not_shown_as_fired(engine) -> None:
    engine._io = _FakeIo(high=False)
    engine._io.accept = False
    assert engine._pulse_out(5000, "event:Loaded") is False
    rows = [e for e in engine._log.all_entries() if e.event_name == "BNC OUT pulse refused"]
    assert len(rows) == 1 and rows[0].source == "BNC"
    assert rows[0].fields == {"purpose": "event:Loaded", "requested_us": 5000}
    assert ("bnc_out",) not in engine.seen


# ---------------------------------------------------------------------------
# BNC input edges carry their own direction and time
# ---------------------------------------------------------------------------

def _edge_event(rising: bool, t: float, **kw) -> BncEdgeEvent:
    return BncEdgeEvent(
        channel=kw.pop("channel", 0), which=kw.pop("which", "IN1"), rising=rising,
        t_mono=t, t_wall=1_760_000_000.0 + t, **kw,
    )


def test_edges_of_a_ten_ms_pulse_keep_their_direction_and_time(engine) -> None:
    """The pin reads 'low' by the time the engine looks. The edges still say what happened."""
    engine._io = _FakeIo(high=False)
    engine._can = _FakeCan()
    engine._on_bnc_edge_event(_edge_event(True, 5.000, seqno=1))
    engine._on_bnc_edge_event(_edge_event(False, 5.010, seqno=2))
    engine.tick()

    rows = [e for e in engine._log.all_entries() if e.frame_type == "BNC"]
    assert [r.event_name for r in rows] == ["IN1 rising", "IN1 falling"]
    assert rows[1].timestamp - rows[0].timestamp == pytest.approx(0.010, abs=1e-6)
    assert all(r.source == "BNC" for r in rows)
    assert rows[0].fields == {"channel": 0, "level": 1, "capture": "kernel", "t_mono": 5.0, "seqno": 1}
    assert [e[:3] for e in engine.seen if e[0] == "bnc_edge"] == [
        ("bnc_edge", "IN1", "rising"), ("bnc_edge", "IN1", "falling")
    ]


def test_missed_edges_are_reported_on_the_row(engine) -> None:
    engine._handle_bnc_event(_edge_event(True, 1.0, seqno=9, missed=3))
    row = [e for e in engine._log.all_entries() if e.frame_type == "BNC"][0]
    assert row.fields["missed_edges"] == 3
    assert "missed_edges=3" in row.details


def test_the_edge_queue_never_blocks_and_counts_what_it_drops(engine) -> None:
    engine._can = _FakeCan()
    for i in range(300):
        engine._on_bnc_edge_event(_edge_event(i % 2 == 0, float(i)))   # returns at once
    assert engine._bnc_edge_dropped == 300 - 256
    engine.tick()
    warnings = [e for e in engine._log.all_entries() if e.event_name == "BNC Edges Dropped"]
    assert len(warnings) == 1 and warnings[0].fields == {"dropped_total": 44}
    engine.tick()
    assert len([e for e in engine._log.all_entries() if e.event_name == "BNC Edges Dropped"]) == 1


def test_older_callers_can_still_queue_a_tuple(engine) -> None:
    engine._io = _FakeIo(high=True)
    engine._bnc_edge_queue.put(("IN2", 1234.5))
    engine._can = _FakeCan()
    engine.tick()
    row = [e for e in engine._log.all_entries() if e.frame_type == "BNC"][0]
    assert (row.event_name, row.timestamp, row.source) == ("IN2 rising", 1234.5, "BNC")


# ---------------------------------------------------------------------------
# One fault must not stop the engine
# ---------------------------------------------------------------------------

def test_a_bad_frame_does_not_cost_the_rest_of_the_batch(engine) -> None:
    engine._can = _FakeCan([_loaded_frame(1), _loaded_frame(2)])
    real = engine._dispatch_rx
    calls = []

    def flaky(msg):
        calls.append(msg)
        if len(calls) == 1:
            raise ValueError("bad frame")
        real(msg)

    engine._dispatch_rx = flaky
    engine.tick()

    assert len(calls) == 2
    assert [e.event_name for e in engine._log.all_entries() if e.event_name == "Loaded"] == ["Loaded"]
    errors = [e for e in engine._log.all_entries() if e.event_name == "EngineError"]
    assert len(errors) == 1
    assert errors[0].fields["phase"] == "rx frame" and errors[0].fields["error"] == "ValueError"
    assert "bad frame" in errors[0].fields["message"]


def test_a_repeating_fault_is_logged_once_with_a_count(engine) -> None:
    engine._can = _FakeCan()
    engine._refresh_experiment_status = lambda: (_ for _ in ()).throw(RuntimeError("display gone"))
    for _ in range(5):
        engine._last_panel_paint = 0.0     # make the status hook run on every tick
        engine.tick()
    errors = [e for e in engine._log.all_entries() if e.event_name == "EngineError"]
    assert len(errors) == 1
    assert engine._error_counts[("status display", "RuntimeError")] == 5


def test_other_steps_still_run_after_one_fails(engine) -> None:
    engine._can = _FakeCan([_loaded_frame(1)])
    engine._refresh_bnc_dots = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    engine.tick()
    assert ("log_table",) in engine.seen        # a later step still ran


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def _stamped(stamp):
    arb, data = build_event_frame(1, CanEvent.Loaded, bytes([5, 0]))
    return SimpleNamespace(arbitration_id=arb, data=bytes(data), timestamp=stamp)


def test_a_can_row_carries_the_frames_arrival_time(engine) -> None:
    arrived = time.time() - 120.0       # the engine was busy for two minutes
    engine._dispatch_rx(_stamped(arrived))
    row = [e for e in engine._log.all_entries() if e.event_name == "Loaded"][0]
    assert row.timestamp == arrived


@pytest.mark.parametrize("stamp", [None, 0, 0.0, -1.0, "junk", time.time() - 7 * 3600, time.time() + 3600])
def test_an_unusable_arrival_stamp_falls_back_to_now(engine, stamp) -> None:
    before = time.time()
    engine._dispatch_rx(_stamped(stamp))
    row = [e for e in engine._log.all_entries() if e.event_name == "Loaded"][0]
    assert before <= row.timestamp <= time.time()


def test_a_wall_clock_step_is_logged_once(engine, monkeypatch) -> None:
    real = time.time
    engine._clock_offset = real() - time.monotonic()
    monkeypatch.setattr("base_station.engine.time.time", lambda: real() + 30.0)
    engine._check_clock()
    engine._check_clock()               # the new offset is the baseline now
    steps = [e for e in engine._log.all_entries() if e.event_name == "ClockStep"]
    assert len(steps) == 1
    assert steps[0].fields["step_s"] == pytest.approx(30.0, abs=0.5)


def test_a_slewed_clock_is_not_a_step(engine) -> None:
    engine._clock_offset = time.time() - time.monotonic() - 0.005   # 5 ms of drift
    engine._check_clock()
    assert [e for e in engine._log.all_entries() if e.event_name == "ClockStep"] == []


def test_the_clock_check_can_be_switched_off(engine, monkeypatch) -> None:
    engine._clock_check_enabled = False
    engine._clock_offset = 0.0
    engine._check_clock()
    assert [e for e in engine._log.all_entries() if e.event_name == "ClockStep"] == []


def test_session_start_says_how_the_timestamps_were_taken(engine, tmp_path) -> None:
    engine._can = _FakeCan()
    engine._io = _FakeIo(high=False)
    engine._log = LogManager(log_dir=str(tmp_path), auto_save=True)
    engine._exp_log_dir = str(tmp_path)
    started, _ = engine.start_experiment(engine._default_experiment_def(), "unit_test", lambda: ({}, [1, 2]))
    try:
        assert started
        row = [e for e in engine._log.all_entries() if e.event_name == "session_start"][0]
        assert row.fields["timebase"] == "capture-v1"
        assert row.fields["bnc_backend"] == "none"
        assert row.fields["bnc_edge_capture"] == "none"
        assert row.fields["clock_offset_s"] == pytest.approx(time.time() - time.monotonic(), abs=1.0)
        # The runner's own keys are untouched.
        assert row.fields["experiment"] == "free_feeding" and row.fields["nodes"] == [1, 2]
    finally:
        engine.stop_experiment()
        engine._log.close()


def test_bnc_start_and_stop_actions_reach_the_front_end(engine) -> None:
    calls = []
    engine._on_experiment_start = lambda *a: calls.append("start")
    engine._on_experiment_stop = lambda *a: calls.append("stop")
    engine._dispatch_bnc_action("start_experiment")
    engine._dispatch_bnc_action("stop_experiment")
    engine._dispatch_bnc_action("(none)")
    assert calls == ["start", "stop"]


# ---------------------------------------------------------------------------
# Node identity
# ---------------------------------------------------------------------------

def test_assign_id_outside_the_valid_range_does_nothing(engine) -> None:
    engine._can = _FakeCan()
    engine.assign_node_id(1, 0)
    engine.assign_node_id(1, 255)
    assert engine._can.commands == []
    assert engine._log.all_entries() == []


def test_assign_id_without_a_known_mac_is_logged(engine) -> None:
    engine._can = _FakeCan()
    engine.assign_node_id(1, 5)
    rows = engine._log.all_entries()
    assert [(r.frame_type, r.event_name, r.details) for r in rows] == [
        ("ERROR", "AssignId", "MAC unknown — run discovery first")
    ]
    assert engine._can.commands == []


# ---------------------------------------------------------------------------
# Hooks are optional
# ---------------------------------------------------------------------------

def test_a_bare_engine_needs_no_front_end(tmp_path) -> None:
    """The default hooks do nothing, so an engine with no front-end still works."""
    e = BaseStationEngine(_args())
    e._registry = NodeRegistry(2)
    e._log = LogManager(auto_save=False)
    e._can = _FakeCan([_loaded_frame(1)])
    e.tick()
    names = [r.event_name for r in e._log.all_entries()]
    # A node with no known MAC is pinged to resolve it, then its event is logged.
    assert names == ["Ping", "Loaded"]
    assert e._read_hb_interval_input_s() == 60
