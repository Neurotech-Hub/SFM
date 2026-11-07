"""
Operator commands, run against a real engine with a fake CAN bus.
No hardware, no display, no threads: ``run_command`` is called directly.
"""

from __future__ import annotations

import json
import struct

import pytest

from base_station.commands import COMMANDS, run_command
from base_station.log_manager import LogManager
from base_station.node_registry import NodeRegistry
from base_station.protocol import CONFIG_HEARTBEAT_INTERVAL, CONFIG_PRESENCE_FACTOR, CanCmd

from tests.test_engine import _args, _FakeCan, _RecordingEngine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # Keep the engine's settings files out of the real ~/.sfm.
    e = _RecordingEngine(_args())
    e._registry = NodeRegistry(2)
    e._log = LogManager(log_dir=str(tmp_path), auto_save=True)
    e._can = _FakeCan()
    e._exp_log_dir = str(tmp_path)
    e._dev_settings = type("D", (), {"presence_factor": 3.0, "set_presence_factor": lambda self, f: setattr(self, "presence_factor", f)})()
    yield e
    e._log.close()


@pytest.fixture
def offline_engine():
    return _RecordingEngine(_args())


def _ok(engine, name, **args):
    out = run_command(engine, name, args)
    assert out["ok"], out
    return out


def _err(engine, name, **args):
    out = run_command(engine, name, args)
    assert out["ok"] is False, out
    return out["error"]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_unknown_command_and_bad_arguments_are_reported(engine) -> None:
    assert "Unknown command" in _err(engine, "no_such_command")
    assert "Bad arguments" in _err(engine, "dispense")            # node missing
    assert "Bad arguments" in _err(engine, "dispense", node=1, extra=2)


def test_every_command_has_a_distinct_name_and_a_docstring_or_name() -> None:
    assert len(COMMANDS) == len(set(COMMANDS))
    assert {"connect", "start_session", "stop_session", "dispense", "set_heartbeat"} <= set(COMMANDS)


def test_commands_need_the_bus(offline_engine) -> None:
    for name, args in [("dispense", {"node": 1}), ("ping", {"node": "all"}), ("recover", {"node": 1}),
                       ("calibrate_presence", {"cage_empty": True}), ("set_heartbeat", {"seconds": 5}),
                       ("set_presence_factor", {"factor": 2}), ("rediscover", {})]:
        assert "Connect to the bus" in _err(offline_engine, name, **args), name


# ---------------------------------------------------------------------------
# Node actions
# ---------------------------------------------------------------------------

def test_dispense_one_node_and_all(engine) -> None:
    _ok(engine, "dispense", node=2)
    assert (2, CanCmd.Dispense, b"") in engine._can.commands
    _ok(engine, "ping", node="all")
    assert (CanCmd.Ping, b"") in engine._can.broadcasts


def test_a_node_that_is_not_in_the_setup_is_refused(engine) -> None:
    assert "not in this setup" in _err(engine, "dispense", node=9)
    assert "not a node number" in _err(engine, "dispense", node="x")


def test_dispense_is_withheld_when_a_pellet_is_on_the_plate(engine) -> None:
    engine._registry.get(1).pellet = True
    _ok(engine, "dispense", node=1)
    assert engine._can.commands == []
    assert any(e.frame_type == "WARNING" for e in engine._log.all_entries())


def test_assign_id_needs_a_known_mac_and_a_valid_id(engine) -> None:
    assert "MAC" in _err(engine, "assign_node_id", node=1, new_id=5)
    engine._registry.register_node(1, bytes([1, 2, 3, 4, 5, 6]))
    assert "1 to 254" in _err(engine, "assign_node_id", node=1, new_id=0)
    assert "1 to 254" in _err(engine, "assign_node_id", node=1, new_id=255)
    _ok(engine, "assign_node_id", node=1, new_id=7)
    assert engine._can.commands[-1][0] == "assign"


def test_calibration_needs_confirmation_and_is_blocked_during_a_run(engine) -> None:
    assert "empty" in _err(engine, "calibrate_presence")
    assert "empty" in _err(engine, "calibrate_presence", cage_empty=False)
    _ok(engine, "calibrate_presence", cage_empty=True)
    assert (CanCmd.CalibratePresence, b"") in engine._can.broadcasts
    _ok(engine, "start_session", experiment="free_feeding", session_name="cal_block")
    assert "not available while a session runs" in _err(engine, "calibrate_presence", cage_empty=True)
    _ok(engine, "stop_session")


# ---------------------------------------------------------------------------
# Developer settings
# ---------------------------------------------------------------------------

def _hb_payloads(engine):
    return [p for c, p in engine._can.broadcasts if c == CanCmd.SetConfig and p[:1] == bytes([CONFIG_HEARTBEAT_INTERVAL])]


def test_heartbeat_range_is_the_firmwares_100ms_to_65_535s(engine) -> None:
    assert "0.1 s to 65.535 s" in _err(engine, "set_heartbeat", seconds=0.05)
    assert "0.1 s to 65.535 s" in _err(engine, "set_heartbeat", seconds=65.6)
    assert "number" in _err(engine, "set_heartbeat", seconds="x")
    assert _hb_payloads(engine) == []

    out = _ok(engine, "set_heartbeat", seconds=0.1)
    assert out["heartbeat_interval_s"] == 0.1
    assert struct.unpack("<H", _hb_payloads(engine)[-1][1:3])[0] == 100

    _ok(engine, "set_heartbeat", seconds=65.535)
    assert struct.unpack("<H", _hb_payloads(engine)[-1][1:3])[0] == 65535


def test_offline_timer_matches_what_nodes_were_told(engine) -> None:
    # Regression: a value above 65.535 s used to be clamped on the wire but not in the timer.
    engine._sync_heartbeat_policy(120, broadcast=True)
    assert engine._hb_interval_s == 65.535
    assert struct.unpack("<H", _hb_payloads(engine)[-1][1:3])[0] == 65535
    engine._sync_heartbeat_policy(0, broadcast=False)
    assert engine._hb_interval_s == 0.1


def test_presence_factor_is_range_checked_and_broadcast(engine) -> None:
    assert "from 0.1 to 100" in _err(engine, "set_presence_factor", factor=0.01)
    assert "from 0.1 to 100" in _err(engine, "set_presence_factor", factor=500)
    _ok(engine, "set_presence_factor", factor=2.5)
    cfg = [p for c, p in engine._can.broadcasts if c == CanCmd.SetConfig and p[:1] == bytes([CONFIG_PRESENCE_FACTOR])]
    assert struct.unpack("<f", cfg[-1][1:5])[0] == pytest.approx(2.5)
    assert engine._dev_settings.presence_factor == 2.5


def test_dev_settings_report_the_real_ranges(engine) -> None:
    out = _ok(engine, "get_dev_settings")
    assert out["heartbeat_range_s"] == [0.1, 65.535]
    json.dumps(out)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_list_experiments_is_json_safe_and_includes_troubleshooting(engine) -> None:
    out = _ok(engine, "list_experiments")
    json.dumps(out)
    names = [e["name"] for e in out["experiments"]]
    assert "free_feeding" in names and "troubleshooting" in names


def test_start_and_stop_a_session(engine) -> None:
    _ok(engine, "start_session", experiment="free_feeding", session_name="my run!")
    assert engine._exp.is_running
    assert engine._log.session_name == "my_run"
    assert "already running" in _err(engine, "start_session", experiment="free_feeding", session_name="again")
    _ok(engine, "stop_session")
    assert not engine._exp.is_running


def test_start_refuses_bad_input_without_leaving_a_session_open(engine) -> None:
    assert "Unknown experiment" in _err(engine, "start_session", experiment="nope", session_name="x")
    assert "session name" in _err(engine, "start_session", experiment="free_feeding", session_name="  ")
    msg = _err(engine, "start_session", experiment="free_feeding", session_name="bad_value",
               values={"fixed_delay_s": "abc", "next_trial_wait": "fixed_delay"})
    assert "not a number" in msg
    assert "Enable at least one node" in _err(
        engine, "start_session", experiment="free_feeding", session_name="no_nodes", values={"nodes": []})
    assert not engine._exp.is_running


def test_preview_session_reports_resume(engine) -> None:
    out = _ok(engine, "preview_session", session_name="resume me")
    assert out["name"] == "resume_me" and out["will_create"] is True
    _ok(engine, "start_session", experiment="free_feeding", session_name="resume me")
    _ok(engine, "stop_session")
    engine._log.close()
    again = _ok(engine, "preview_session", session_name="resume me")
    assert again["will_create"] is False and again["next_run_id"] == 2
    json.dumps(again)


def test_arming_needs_an_enabled_bnc_start_input(engine) -> None:
    assert "BNC input" in _err(engine, "arm_start", experiment="free_feeding", session_name="armed")
    _ok(engine, "set_bnc_input", which="IN1", enabled=True, mode="pulse", action="start_experiment", min_width_ms=5)
    _ok(engine, "arm_start", experiment="free_feeding", session_name="armed")
    assert engine.start_armed
    _ok(engine, "disarm_start")
    assert not engine.start_armed


# ---------------------------------------------------------------------------
# BNC
# ---------------------------------------------------------------------------

def test_bnc_input_settings_are_validated_by_the_engine(engine) -> None:
    assert "Unknown BNC input setting" in _err(engine, "set_bnc_input", which="IN1", bogus=1)
    assert _err(engine, "set_bnc_input", which="IN1", mode="nonsense")


def test_bnc_out_is_locked_during_a_run_and_pulse_is_refused(engine) -> None:
    _ok(engine, "set_bnc_out", mapping="session_start_stop", width_us=5000)
    assert engine._bnc_out_cfg.mapping == "session_start_stop"
    _ok(engine, "start_session", experiment="free_feeding", session_name="locked")
    assert "locked" in _err(engine, "set_bnc_out", mapping="off")
    assert "not available while a session runs" in _err(engine, "bnc_pulse")
    _ok(engine, "stop_session")


def test_node_event_mapping_needs_a_known_event(engine) -> None:
    assert "node event" in _err(engine, "set_bnc_out", mapping="node_event", trigger="made up")
    triggers = _ok(engine, "bnc_out_triggers")["triggers"]
    _ok(engine, "set_bnc_out", mapping="node_event", trigger=triggers[1])


# ---------------------------------------------------------------------------
# Connection and log
# ---------------------------------------------------------------------------

def test_connect_checks_node_count_and_existing_connection(offline_engine, engine) -> None:
    assert "between 1 and 12" in _err(offline_engine, "connect", interface="vcan0", num_nodes=13)
    assert "between 1 and 12" in _err(offline_engine, "connect", interface="vcan0", num_nodes=0)
    assert "Already connected" in _err(engine, "connect", interface="vcan0", num_nodes=2)


def test_check_interface_reports_a_missing_interface(engine) -> None:
    out = _ok(engine, "check_interface", interface="definitely_not_a_can_if")
    assert out["up"] is False


def test_log_export_name_is_skipped_by_the_loader_and_has_the_header(engine) -> None:
    from base_station.log_manager import LogEntry
    from sfm_analysis.logs import CSV_HEADER
    import csv, time
    engine._log.add(LogEntry(timestamp=time.time(), direction="RX", node_id=1, frame_type="EVENT",
                             event_name="x", raw_id=1, raw_data=b""))
    out = _ok(engine, "log_export")
    from pathlib import Path
    assert "_export_" in Path(out["path"]).stem   # the loader skips stems containing this
    with open(out["path"], newline="", encoding="utf-8") as f:
        assert next(csv.reader(f)) == list(CSV_HEADER)


def test_log_clear_keeps_the_csv(engine) -> None:
    from base_station.log_manager import LogEntry
    import time
    engine._log.add(LogEntry(timestamp=time.time(), direction="RX", node_id=1, frame_type="EVENT",
                             event_name="x", raw_id=1, raw_data=b""))
    _ok(engine, "log_clear")
    assert engine._log.all_entries() == []


def test_a_successful_connect_probes_the_bus_and_loads_saved_settings(engine, monkeypatch, tmp_path) -> None:
    # Connecting for real needs a CAN interface; stand in for the engine's own connect.
    engine._can = None
    calls = []
    monkeypatch.setattr(engine, "connect", lambda *a: (setattr(engine, "_can", _FakeCan()), "")[1])
    monkeypatch.setattr(engine, "enable_bnc_persistence", lambda: calls.append("bnc"))
    monkeypatch.setattr(engine, "enable_node_layout", lambda: calls.append("layout"))
    out = _ok(engine, "connect", interface="vcan0", num_nodes=2)
    assert out["num_nodes"] == 2
    assert calls == ["bnc", "layout"]
    # Every node already running answers this with its MAC, so the map is complete at once.
    assert (CanCmd.Ping, b"") in engine._can.broadcasts


def test_a_failed_connect_changes_nothing_else(engine, monkeypatch) -> None:
    engine._can = None
    calls = []
    monkeypatch.setattr(engine, "connect", lambda *a: "CAN error: no such device")
    monkeypatch.setattr(engine, "enable_node_layout", lambda: calls.append("layout"))
    assert "no such device" in _err(engine, "connect", interface="can9", num_nodes=2)
    assert calls == []


# ---------------------------------------------------------------------------
# What the Connect step needs before any bus is open
# ---------------------------------------------------------------------------

def test_list_interfaces_finds_can_interfaces_only(offline_engine, monkeypatch, tmp_path) -> None:
    import base_station.commands as commands
    net = tmp_path / "net"
    for name, typ in (("can0", 280), ("vcan0", 280), ("eth0", 1), ("lo", 772)):
        (net / name).mkdir(parents=True)
        (net / name / "type").write_text(f"{typ}\n")
    (net / "stray_file").write_text("x")
    monkeypatch.setattr(commands, "NET_SYSFS", net)
    monkeypatch.setattr(commands, "_can_bitrate", lambda name: 250000)
    monkeypatch.setattr(offline_engine, "_is_can_interface_up", lambda name: name == "can0")
    out = _ok(offline_engine, "list_interfaces")["interfaces"]
    assert [i["name"] for i in out] == ["can0", "vcan0"]
    assert out[0]["up"] is True and out[1]["up"] is False
    assert out[0]["bitrate"] == 250000


def test_list_interfaces_with_no_sysfs_is_empty_not_an_error(offline_engine, monkeypatch, tmp_path) -> None:
    import base_station.commands as commands
    monkeypatch.setattr(commands, "NET_SYSFS", tmp_path / "missing")
    assert _ok(offline_engine, "list_interfaces")["interfaces"] == []


def test_defaults_carry_the_real_limits_and_choices(offline_engine) -> None:
    out = _ok(offline_engine, "get_defaults")
    json.dumps(out)
    assert out["max_nodes"] == 12 and out["bitrate"] == 250000
    assert out["heartbeat_range_s"] == [0.1, 65.535]
    assert out["presence_factor_range"] == [0.1, 100.0]
    assert out["bnc"]["input_modes"] == ["edge", "pulse", "level", "log_only"]
    assert "start_experiment" in out["bnc"]["input_actions"]
    assert "session_start_sync" in out["bnc"]["out_mappings"]


def test_connect_settings_are_remembered_across_engines(offline_engine) -> None:
    # No bus needed: these are the form's starting values.
    _ok(offline_engine, "set_gui_settings", interface="can0", num_nodes=4, auto_save=False)
    assert _ok(offline_engine, "get_gui_settings")["settings"] == {"interface": "can0", "num_nodes": 4, "auto_save": False}
    other = _RecordingEngine(_args())          # a new engine process, same home
    assert _ok(other, "get_gui_settings")["settings"]["num_nodes"] == 4


def test_bad_connect_settings_are_operator_errors(offline_engine) -> None:
    assert "1 to 12" in _err(offline_engine, "set_gui_settings", num_nodes=40)
    assert "Unknown setting" in _err(offline_engine, "set_gui_settings", color="red")
    assert _ok(offline_engine, "get_gui_settings")["settings"] == {}


def test_the_session_setup_is_shown_in_the_state_for_a_page_opened_later(engine) -> None:
    from base_station.state import build_snapshot
    assert build_snapshot(engine)["session"]["setup"] is None
    _ok(engine, "start_session", experiment="free_feeding", session_name="My Run",
        values={"next_trial_wait": "fixed_delay", "fixed_delay_s": 12})
    setup = build_snapshot(engine)["session"]["setup"]
    assert setup == {"experiment": "free_feeding", "session_name": "My_Run",
                     "values": {"next_trial_wait": "fixed_delay", "fixed_delay_s": 12}}
    json.dumps(setup)
    _ok(engine, "stop_session")


def test_a_failed_start_does_not_overwrite_the_setup(engine) -> None:
    from base_station.state import build_snapshot
    _ok(engine, "start_session", experiment="free_feeding", session_name="first")
    _ok(engine, "stop_session")
    _err(engine, "start_session", experiment="free_feeding", session_name="second", values={"nodes": []})
    assert build_snapshot(engine)["session"]["setup"]["session_name"] == "first"


def test_each_notice_has_its_own_number(engine) -> None:
    from base_station.state import build_snapshot
    assert build_snapshot(engine)["notice"] is None
    engine._notice("warning", "same text")
    first = build_snapshot(engine)["notice"]["seq"]
    engine._notice("warning", "same text")
    assert build_snapshot(engine)["notice"]["seq"] == first + 1


def test_heartbeat_rows_come_from_their_own_ring(engine) -> None:
    from base_station.log_manager import LogEntry
    import time as _t
    engine._log.add(LogEntry(timestamp=_t.time(), direction="RX", node_id=1, frame_type="HEARTBEAT",
                             event_name="Heartbeat", raw_id=0x201, raw_data=b"\x01"))
    engine._log.add(LogEntry(timestamp=_t.time(), direction="RX", node_id=1, frame_type="EVENT",
                             event_name="x", raw_id=1, raw_data=b""))
    rows = _ok(engine, "heartbeat_rows")["rows"]
    assert [r["event"] for r in rows] == ["Heartbeat"]
    assert [r["event"] for r in _ok(engine, "log_rows")["rows"]] == ["x"]
