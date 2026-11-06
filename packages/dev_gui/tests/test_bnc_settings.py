"""
BNC settings that survive a restart, and the validated setters that change them.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from base_station.bnc_settings import BncSettings, apply_settings, atomic_write_json
from base_station.engine import BaseStationEngine
from base_station.io_manager import BNCInputConfig, BNCOutputConfig
from base_station.log_manager import LogManager
from base_station.node_registry import NodeRegistry


class _FakeCan:
    def poll_rx(self):
        return []

    def send_command(self, node_id, cmd, payload=b""):
        return True

    def send_broadcast(self, cmd, payload=b""):
        return True


class _FakeIo:
    def pulse_bnc_out(self, width_us, purpose=""):
        return True

    def read_bnc_in1(self):
        return False

    read_bnc_in2 = read_bnc_in1


def _new_engine(tmp_path):
    e = BaseStationEngine(argparse.Namespace(interface="can0", bitrate=1, nodes=2, log_dir=str(tmp_path)))
    e._can = _FakeCan()
    e._io = _FakeIo()
    e._registry = NodeRegistry(3)
    for n in (1, 2, 3):
        e._registry.get(n).online = True
    e._log = LogManager(log_dir=str(tmp_path), auto_save=True)
    e._exp_log_dir = str(tmp_path)
    return e


@pytest.fixture
def engine(tmp_path):
    e = _new_engine(tmp_path)
    yield e
    if e._exp.is_running:
        e.stop_experiment()
    e._log.close()


@pytest.fixture
def store(tmp_path):
    return tmp_path / "bnc_settings.json"


def _rows(engine, name):
    return [e for e in engine._log.all_entries() if e.event_name == name]


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

def test_settings_round_trip_through_the_file(store) -> None:
    in1 = BNCInputConfig(label="Start", enabled=True, mode="pulse", action="start_experiment",
                         active_high=False, min_width_ms=8.0)
    in2 = BNCInputConfig(label="Camera", enabled=True, mode="log_only")
    out = BNCOutputConfig(mapping="node_event", trigger="Dome Opened", pulse_width_us=20_000,
                          sync_width_us=250_000, periodic_interval_s=30.0, enabled=True)
    BncSettings(store).save(in1, in2, out)

    new1, new2, new_out = BNCInputConfig(), BNCInputConfig(), BNCOutputConfig()
    assert apply_settings(BncSettings(store).load(), new1, new2, new_out) == []

    assert (new1.label, new1.enabled, new1.mode, new1.action, new1.active_high, new1.min_width_ms) == \
        ("Start", True, "pulse", "start_experiment", False, 8.0)
    assert (new2.label, new2.mode) == ("Camera", "log_only")
    assert (new_out.mapping, new_out.trigger, new_out.pulse_width_us, new_out.sync_width_us,
            new_out.periodic_interval_s) == ("node_event", "Dome Opened", 20_000, 250_000, 30.0)


def test_a_missing_or_unreadable_file_means_defaults(store) -> None:
    assert BncSettings(store).load() == {}
    store.write_text("{ this is not json", encoding="utf-8")
    assert BncSettings(store).load() == {}
    store.write_text("[1, 2, 3]", encoding="utf-8")
    assert BncSettings(store).load() == {}
    store.write_bytes(b"\xff\xfe\x00bad")
    assert BncSettings(store).load() == {}


def test_a_partial_file_from_another_version_loads_what_it_can(store) -> None:
    store.write_text(json.dumps({
        "version": 99, "future_key": True,
        "inputs": {"IN1": {"mode": "pulse", "unknown_field": 1}},
    }), encoding="utf-8")
    in1, in2, out = BNCInputConfig(), BNCInputConfig(), BNCOutputConfig()
    assert apply_settings(BncSettings(store).load(), in1, in2, out) == []
    assert in1.mode == "pulse" and in2.mode == "edge" and out.mapping == "legacy"


@pytest.mark.parametrize("data, where", [
    ({"inputs": {"IN1": {"mode": "bogus"}}}, "inputs.IN1.mode"),
    ({"inputs": {"IN1": {"enabled": "yes"}}}, "inputs.IN1.enabled"),
    ({"inputs": {"IN1": {"min_width_ms": -3}}}, "inputs.IN1.min_width_ms"),
    ({"inputs": {"IN2": {"min_width_ms": True}}}, "inputs.IN2.min_width_ms"),
    ({"output": {"mapping": "bogus"}}, "output.mapping"),
    ({"output": {"pulse_width_us": 0}}, "output.pulse_width_us"),
    ({"output": {"pulse_width_us": 1.5}}, "output.pulse_width_us"),
    ({"output": {"periodic_interval_s": 0.01}}, "output.periodic_interval_s"),
])
def test_an_unusable_value_is_skipped_and_reported_and_the_default_kept(data, where) -> None:
    in1, in2, out = BNCInputConfig(), BNCInputConfig(), BNCOutputConfig()
    skipped = apply_settings(data, in1, in2, out)
    assert any(where in s for s in skipped), skipped
    assert in1.mode in ("edge", "pulse", "level", "log_only")
    assert in1.min_width_ms > 0 and in2.min_width_ms > 0
    assert out.mapping == "legacy" and out.pulse_width_us >= 1 and out.periodic_interval_s >= 0.5


def test_one_bad_value_does_not_cost_the_others() -> None:
    in1, in2, out = BNCInputConfig(), BNCInputConfig(), BNCOutputConfig()
    skipped = apply_settings({"inputs": {"IN1": {"mode": "pulse", "enabled": "yes", "label": "ok"}}},
                             in1, in2, out)
    assert len(skipped) == 1
    assert (in1.mode, in1.label, in1.enabled) == ("pulse", "ok", False)


# ---------------------------------------------------------------------------
# Writing safely
# ---------------------------------------------------------------------------

def test_a_crash_during_a_save_leaves_the_old_file_intact(store, monkeypatch) -> None:
    store.write_text(json.dumps({"version": 1, "marker": "old"}), encoding="utf-8")

    def crash(*a, **k):
        raise OSError("power cut")

    monkeypatch.setattr("base_station.bnc_settings.os.replace", crash)
    with pytest.raises(OSError):
        atomic_write_json(store, {"version": 1, "marker": "new"})

    assert json.loads(store.read_text(encoding="utf-8"))["marker"] == "old"
    assert not store.with_suffix(".json.tmp").exists()      # no stray temporary file


def test_saving_creates_the_folder_and_replaces_in_one_step(tmp_path) -> None:
    path = tmp_path / "a" / "b" / "bnc.json"
    atomic_write_json(path, {"n": 1})
    atomic_write_json(path, {"n": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 2}
    assert sorted(p.name for p in path.parent.iterdir()) == ["bnc.json"]


# ---------------------------------------------------------------------------
# In the engine
# ---------------------------------------------------------------------------

def test_nothing_is_saved_unless_persistence_was_asked_for(engine, store) -> None:
    assert engine.set_bnc_input("IN1", mode="pulse", enabled=True) == ""
    assert not store.exists()


def test_changes_are_saved_and_come_back_after_a_restart(engine, tmp_path, store) -> None:
    engine.enable_bnc_persistence(str(store))
    assert engine.set_bnc_input("IN1", enabled=True, mode="pulse", action="start_experiment",
                                min_width_ms=8.0, label="Start") == ""
    assert engine.set_bnc_input("IN2", enabled=True, mode="log_only") == ""
    assert engine.set_bnc_out_mapping("node_event", trigger="Dome Opened", width_us=20_000) == ""

    restarted = _new_engine(tmp_path)
    try:
        assert restarted.enable_bnc_persistence(str(store)) == []
        cfg = restarted._bnc_in1_cfg
        assert (cfg.enabled, cfg.mode, cfg.action, cfg.min_width_ms, cfg.label) == \
            (True, "pulse", "start_experiment", 8.0, "Start")
        assert restarted._bnc_in2_cfg.mode == "log_only"
        assert (restarted._bnc_out_cfg.mapping, restarted._bnc_out_cfg.trigger) == ("node_event", "Dome Opened")
    finally:
        restarted._log.close()


def test_a_restart_is_never_armed_even_with_a_start_input_saved(engine, tmp_path, store) -> None:
    engine.enable_bnc_persistence(str(store))
    engine.set_bnc_input("IN1", enabled=True, mode="pulse", action="start_experiment")
    assert engine.arm_start(engine._default_experiment_def(), "armed_run", lambda: ({}, [1, 2])) == ""
    assert engine.start_armed

    restarted = _new_engine(tmp_path)
    try:
        restarted.enable_bnc_persistence(str(store))
        assert restarted._bnc_in1_cfg.action == "start_experiment"     # the setting came back...
        assert restarted.start_armed is False                          # ...the arming did not
    finally:
        restarted._log.close()


def test_loading_a_saved_fast_path_arms_it_once_the_tap_is_in(engine, tmp_path, store) -> None:
    engine.enable_bnc_persistence(str(store))
    engine._rx_tap_installed = True
    engine.set_bnc_out_mapping("node_event", trigger="Loaded")

    restarted = _new_engine(tmp_path)
    try:
        restarted._rx_tap_installed = True
        restarted.enable_bnc_persistence(str(store))
        assert restarted._fast_out is not None
    finally:
        restarted._log.close()


def test_unusable_saved_values_are_logged_on_load(engine, store) -> None:
    store.write_text(json.dumps({"inputs": {"IN1": {"mode": "bogus"}}}), encoding="utf-8")
    skipped = engine.enable_bnc_persistence(str(store))
    assert len(skipped) == 1
    row = _rows(engine, "BNC Settings Skipped")[0]
    assert row.fields["skipped"] == skipped and row.source == "BNC"


def test_a_failed_save_is_reported_and_does_not_stop_the_engine(engine, store, monkeypatch) -> None:
    engine.enable_bnc_persistence(str(store))
    monkeypatch.setattr("base_station.bnc_settings.os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert engine.set_bnc_input("IN1", enabled=True) == ""           # the change still applies
    assert engine._bnc_in1_cfg.enabled is True
    assert _rows(engine, "BNC Settings Not Saved")[0].details == "disk full"
    assert engine.last_notice[0] == "warning"


# ---------------------------------------------------------------------------
# set_bnc_input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, expected", [
    ({"which": "IN3"}, "Unknown BNC input"),
    ({"which": "IN1", "mode": "bogus"}, "Unknown input mode"),
    ({"which": "IN1", "action": "launch_rockets"}, "Unknown action for action"),
    ({"which": "IN1", "release_action": "nope"}, "Unknown action for release_action"),
    ({"which": "IN1", "min_width_ms": 0}, "minimum pulse width"),
    ({"which": "IN1", "min_width_ms": 100_000}, "minimum pulse width"),
])
def test_invalid_input_settings_are_refused_and_change_nothing(engine, kwargs, expected) -> None:
    before = (engine._bnc_in1_cfg.mode, engine._bnc_in1_cfg.action, engine._bnc_in1_cfg.min_width_ms)
    assert expected in engine.set_bnc_input(**kwargs, label="should not stick")
    assert (engine._bnc_in1_cfg.mode, engine._bnc_in1_cfg.action, engine._bnc_in1_cfg.min_width_ms) == before
    assert engine._bnc_in1_cfg.label != "should not stick"


def test_inputs_are_locked_while_a_session_runs(engine) -> None:
    started, _ = engine.start_experiment(engine._default_experiment_def(), "locked", lambda: ({}, [1, 2]))
    assert started
    assert engine.set_bnc_input("IN1", mode="pulse") == "BNC inputs are locked while a session runs."
    assert engine._bnc_in1_cfg.mode == "edge"


def test_changing_an_input_withdraws_an_arming(engine) -> None:
    engine.set_bnc_input("IN1", enabled=True, mode="pulse", action="start_experiment")
    assert engine.arm_start(engine._default_experiment_def(), "armed_run", lambda: ({}, [1, 2])) == ""
    engine.set_bnc_input("IN1", min_width_ms=20.0)
    assert engine.start_armed is False
    assert len(_rows(engine, "BNC Disarmed")) == 1


def test_an_input_can_be_switched_off_by_setting_enabled_false(engine) -> None:
    engine.set_bnc_input("IN1", enabled=True)
    assert engine.set_bnc_input("IN1", enabled=False) == ""
    assert engine._bnc_in1_cfg.enabled is False


def test_changing_an_input_forgets_a_half_seen_pulse(engine) -> None:
    engine._bnc_active_since["IN1"] = 5.0
    engine.set_bnc_input("IN1", min_width_ms=20.0)
    assert "IN1" not in engine._bnc_active_since
