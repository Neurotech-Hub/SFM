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
