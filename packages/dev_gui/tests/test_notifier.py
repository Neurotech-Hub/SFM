"""Phone alerts for node faults: settings, filtering, and the Fault-frame hook."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from base_station.notifier import (
    DEDUPE_WINDOW_S,
    KNOWN_FAULTS,
    Notifier,
    NotifySettings,
    generate_topic,
)
from base_station.protocol import (
    CanEvent,
    DispenseState,
    HeartbeatPayload,
    ServiceStatus,
    build_event_frame,
    build_heartbeat_frame,
)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"ok"


def _settings(tmp_path, **overrides) -> NotifySettings:
    settings = NotifySettings(tmp_path / "notify.json")
    settings.topic = "labtopic123"
    settings.station_name = "Lab A"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _notifier(tmp_path, monkeypatch, **overrides):
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append((req, timeout))
        return _Response()

    monkeypatch.setattr("base_station.notifier.urllib.request.urlopen", fake_urlopen)
    notifier = Notifier(_settings(tmp_path, **overrides))
    return notifier, captured


def _headers(req) -> dict:
    return {name.lower(): value for name, value in req.header_items()}


def test_missing_or_corrupt_file_uses_defaults(tmp_path) -> None:
    missing = NotifySettings(tmp_path / "missing.json")
    assert missing.enabled is True
    assert missing.server == "https://ntfy.sh"
    assert missing.topic == ""
    assert missing.faults == list(KNOWN_FAULTS)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    loaded = NotifySettings(corrupt)
    assert loaded.topic == ""
    assert loaded.faults == list(KNOWN_FAULTS)


def test_save_load_round_trip_ignores_unknown_keys(tmp_path) -> None:
    path = tmp_path / "notify.json"
    settings = NotifySettings(path)
    settings.enabled = False
    settings.server = "https://alerts.example"
    settings.topic = "lab-topic"
    settings.token = "secret"
    settings.station_name = "Rig 1"
    settings.faults = ["Jam", "FeedTimeout"]
    settings.save()

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["not_a_real_key"] = "ignored"
    raw["faults"] = ["Jam", "NotAFault", "FeedTimeout"]
    raw["enabled"] = "yes"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = NotifySettings(path)
    assert loaded.enabled is True  # non-bool is ignored, so the default stands
    assert loaded.server == "https://alerts.example"
    assert loaded.topic == "lab-topic"
    assert loaded.token == "secret"
    assert loaded.station_name == "Rig 1"
    assert loaded.faults == ["Jam", "FeedTimeout"]
    assert not hasattr(loaded, "not_a_real_key")


def test_generate_topic_is_20_unguessable_characters() -> None:
    topic = generate_topic()
    assert len(topic) == 20
    assert topic.isalnum()
    assert generate_topic() != topic


def test_disabled_code_or_empty_topic_sends_nothing(tmp_path, monkeypatch) -> None:
    notifier, captured = _notifier(tmp_path, monkeypatch, faults=["Jam"])
    notifier.on_fault(1, ServiceStatus.PelletLost, "Node 1", "")
    notifier.on_fault(1, None, "Node 1", "")
    notifier.settings.topic = ""
    notifier.on_fault(1, ServiceStatus.Jam, "Node 1", "")
    notifier.flush()
    assert captured == []


def test_alerts_off_sends_nothing(tmp_path, monkeypatch) -> None:
    notifier, captured = _notifier(tmp_path, monkeypatch, enabled=False)
    notifier.on_fault(1, ServiceStatus.Jam)
    notifier.flush()
    assert captured == []


@pytest.mark.parametrize("code", list(ServiceStatus))
def test_each_enabled_code_sends_once(tmp_path, monkeypatch, code) -> None:
    notifier, captured = _notifier(tmp_path, monkeypatch)
    notifier.on_fault(code.value + 1, code, f"Node {code.value + 1}", "cohort")
    notifier.flush()
    if code.name in KNOWN_FAULTS:
        assert len(captured) == 1
        assert captured[0][0].full_url.endswith("/labtopic123")
    else:
        assert captured == []


def test_duplicate_window(tmp_path, monkeypatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr("base_station.notifier.time.monotonic", lambda: clock["now"])
    notifier, captured = _notifier(tmp_path, monkeypatch)

    notifier.on_fault(2, ServiceStatus.Jam, "Node 2", "")
    notifier.on_fault(2, ServiceStatus.Jam, "Node 2", "")
    notifier.on_fault(3, ServiceStatus.Jam, "Node 3", "")
    notifier.flush()
    assert len(captured) == 2

    clock["now"] += DEDUPE_WINDOW_S
    notifier.on_fault(2, ServiceStatus.Jam, "Node 2", "")
    notifier.flush()
    assert len(captured) == 3


def test_request_headers_include_bearer_only_with_a_token(tmp_path, monkeypatch) -> None:
    notifier, captured = _notifier(tmp_path, monkeypatch, token="abc123")
    notifier.on_fault(2, ServiceStatus.Jam, "Left feeder", "cohortA")
    notifier.flush()

    req, timeout = captured[0]
    headers = _headers(req)
    assert req.full_url == "https://ntfy.sh/labtopic123"
    assert headers["title"].startswith("SFM Lab A: Node 2 (Left feeder) Jam")
    assert headers["priority"] == "high"
    assert headers["tags"] == "warning"
    assert headers["authorization"] == "Bearer abc123"
    assert timeout == 10.0
    body = req.data.decode("utf-8")
    assert "Jam" in body
    assert "Session: cohortA" in body

    bare, _captured = _notifier(tmp_path, monkeypatch, token="")
    bare.on_fault(1, ServiceStatus.FeedTimeout, "Node 1", "")
    bare.flush()
    assert "authorization" not in _headers(_captured[0][0])


def test_failed_send_is_retried_once_and_does_not_raise(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise URLError("network down")

    monkeypatch.setattr("base_station.notifier.urllib.request.urlopen", fake_urlopen)
    notifier = Notifier(_settings(tmp_path))
    notifier.on_fault(1, ServiceStatus.ActuatorTimeout)
    notifier.flush()
    assert calls["n"] == 2
    assert notifier.last_result is not None
    assert notifier.last_result.ok is False
    assert "network down" in notifier.last_result.detail


def test_send_test_without_a_topic_records_an_error(tmp_path) -> None:
    notifier = Notifier(_settings(tmp_path, topic=""))
    notifier.send_test()
    assert notifier.last_result is not None
    assert notifier.last_result.ok is False
    assert "topic" in notifier.last_result.detail.lower()


def _msg(arb_id: int, data: bytes):
    return SimpleNamespace(arbitration_id=arb_id, data=bytes(data))


def _make_app():
    from base_station.app import SFMApp
    from base_station.log_manager import LogManager
    from base_station.node_registry import NodeRegistry

    app = SFMApp(
        argparse.Namespace(interface="can0", bitrate=250000, nodes=2, log_dir="~/sfm_logs")
    )
    app._registry = NodeRegistry(2)
    app._log = LogManager(auto_save=False)
    app._ensure_node_heartbeat_config = lambda node_id: None
    return app


def test_fault_frame_notifies_once_and_a_heartbeat_does_not() -> None:
    try:
        app = _make_app()
    except ModuleNotFoundError as exc:
        if "dearpygui" in str(exc).lower():
            pytest.skip("dearpygui not installed")
        raise

    calls = []
    app._notifier.on_fault = lambda *args, **kwargs: calls.append(args)
    app._registry.set_label(2, "Left feeder")
    app._log.set_context(session="cohortA")

    arb, data = build_event_frame(2, CanEvent.Fault, bytes([ServiceStatus.Jam]))
    app._dispatch_rx(_msg(arb, data))

    heartbeat = HeartbeatPayload(
        dispense_state=DispenseState.Fault,
        mouse_presence=False,
        pellet=False,
        load_position=True,
        dome_open=False,
        fault_code=ServiceStatus.Jam,
    )
    hb_arb, hb_data = build_heartbeat_frame(2, heartbeat)
    app._dispatch_rx(_msg(hb_arb, hb_data))

    assert calls == [(2, ServiceStatus.Jam, "Left feeder", "cohortA")]
    fault_rows = [e for e in app._log.all_entries() if e.event_name == "Fault: Jam"]
    assert len(fault_rows) == 1


def test_dispatch_survives_a_notifier_failure() -> None:
    try:
        app = _make_app()
    except ModuleNotFoundError as exc:
        if "dearpygui" in str(exc).lower():
            pytest.skip("dearpygui not installed")
        raise

    def boom(*_args, **_kwargs):
        raise RuntimeError("notifier down")

    app._notifier.on_fault = boom
    arb, data = build_event_frame(1, CanEvent.Fault, bytes([ServiceStatus.FeedTimeout]))
    app._dispatch_rx(_msg(arb, data))
    rows = [e for e in app._log.all_entries() if e.event_name == "Fault: FeedTimeout"]
    assert len(rows) == 1
