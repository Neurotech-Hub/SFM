import argparse

import pytest

from base_station.protocol import CanCmd


def test_command_purpose_covers_all_cmds() -> None:
    # Import COMMAND_PURPOSE without requiring a full DearPyGui session beyond
    # the module import (dearpygui must be installed for app.py).
    try:
        from base_station.app import COMMAND_PURPOSE
    except ModuleNotFoundError as exc:
        if "dearpygui" in str(exc).lower():
            pytest.skip("dearpygui not installed")
        raise
    for cmd in CanCmd:
        assert cmd in COMMAND_PURPOSE, f"missing COMMAND_PURPOSE for {cmd.name}"
        assert isinstance(COMMAND_PURPOSE[cmd], str) and COMMAND_PURPOSE[cmd]


def test_render_callback_wrapper_reschedules_itself(monkeypatch) -> None:
    try:
        from base_station.app import SFMApp
    except ModuleNotFoundError as exc:
        if "dearpygui" in str(exc).lower():
            pytest.skip("dearpygui not installed")
        raise

    app = SFMApp(
        argparse.Namespace(interface="can0", bitrate=250000, nodes=3, log_dir="~/sfm_logs")
    )

    calls = []
    monkeypatch.setattr(app, "_on_render", lambda: calls.append("render"))
    monkeypatch.setattr("base_station.app.dpg.get_frame_count", lambda: 7)
    scheduled = []
    monkeypatch.setattr("base_station.app.dpg.set_frame_callback", lambda frame, cb: scheduled.append((frame, cb)))

    callback = app._make_render_callback()
    callback()

    assert calls == ["render"]
    assert scheduled == [(8, callback)]


def test_default_experiment_is_free_feeding() -> None:
    try:
        from base_station.app import SFMApp
    except ModuleNotFoundError as exc:
        if "dearpygui" in str(exc).lower():
            pytest.skip("dearpygui not installed")
        raise

    app = SFMApp(
        argparse.Namespace(interface="can0", bitrate=250000, nodes=3, log_dir="~/sfm_logs")
    )
    default_def = app._default_experiment_def()
    assert default_def is not None
    assert default_def.name == "free_feeding"


class _FakeDpg:
    """
    Minimal in-memory stand-in for the dpg tag registry used by
    _on_experiment_start's session-name gate. Real dpg widget calls segfault
    without a live GUI context (create_context() was never called in these
    unit tests), so every dpg.* call the code path under test touches must be
    faked rather than invoked for real.
    """

    def __init__(self, values: dict) -> None:
        self._values = dict(values)

    def does_item_exist(self, tag):
        return tag in self._values

    def get_value(self, tag):
        return self._values.get(tag)

    def set_value(self, tag, value):
        self._values[tag] = value

    def configure_item(self, tag, **kwargs):
        pass


@pytest.mark.parametrize("session_name", ["", "   ", "***", "!!! ??? ---"])
def test_experiment_start_rejects_missing_session_name(monkeypatch, session_name) -> None:
    try:
        from base_station.app import SFMApp
    except ModuleNotFoundError as exc:
        if "dearpygui" in str(exc).lower():
            pytest.skip("dearpygui not installed")
        raise
    from base_station.log_manager import LogManager

    app = SFMApp(
        argparse.Namespace(interface="can0", bitrate=250000, nodes=3, log_dir="~/sfm_logs")
    )
    # Simulate a started GUI session (bypassing real CAN/GPIO hardware) so
    # _on_experiment_start gets past its can/registry guard and reaches the
    # session-name gate.
    app._can = object()
    app._registry = object()
    app._log = LogManager(auto_save=False)

    fake = _FakeDpg({"exp_session_name": session_name})
    monkeypatch.setattr("base_station.app.dpg.does_item_exist", fake.does_item_exist)
    monkeypatch.setattr("base_station.app.dpg.get_value", fake.get_value)
    monkeypatch.setattr("base_station.app.dpg.set_value", fake.set_value)
    monkeypatch.setattr("base_station.app.dpg.configure_item", fake.configure_item)

    app._on_experiment_start()

    assert not app._exp.is_running
    assert fake.get_value("exp_status_text") == "Enter a session name before starting."
    error_rows = [e for e in app._log.all_entries() if e.frame_type == "ERROR"]
    assert len(error_rows) == 1
    assert error_rows[0].event_name == "ExperimentStartRejected"
