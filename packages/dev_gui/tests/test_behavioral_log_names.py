"""Behavioral event-log naming: one line per logical sensor/milestone event."""

from __future__ import annotations

from base_station.protocol import (
    DispenseState,
    InputId,
    behavioral_input_log_name,
    normalize_event_match_key,
)


def test_pellet_and_load_position_input_edges_are_suppressed() -> None:
    assert behavioral_input_log_name(InputId.Pellet, True) is None
    assert behavioral_input_log_name(InputId.Pellet, False) is None
    assert behavioral_input_log_name(InputId.LoadPosition, True) is None
    assert behavioral_input_log_name(InputId.LoadPosition, False) is None


def test_dome_open_while_loaded_defers_to_dome_opened_milestone() -> None:
    assert behavioral_input_log_name(
        InputId.Dome, True, DispenseState.Loaded
    ) is None


def test_dome_edges_outside_loaded_use_behavioral_names() -> None:
    assert behavioral_input_log_name(
        InputId.Dome, True, DispenseState.Idle
    ) == "Dome Opened"
    assert behavioral_input_log_name(
        InputId.Dome, False, DispenseState.Loaded
    ) == "Dome closed"
    assert behavioral_input_log_name(
        InputId.Dome, False, DispenseState.Idle
    ) == "Dome closed"


def test_mouse_presence_unchanged() -> None:
    assert behavioral_input_log_name(
        InputId.MousePresence, True
    ) == "MousePresence Detected"
    assert behavioral_input_log_name(
        InputId.MousePresence, False
    ) == "MousePresence Cleared"


def test_bnc_trigger_matches_spaced_and_legacy_camel_case() -> None:
    assert normalize_event_match_key("Dome Opened") == normalize_event_match_key(
        "DomeOpened"
    )
    assert normalize_event_match_key("Pellet Taken") == normalize_event_match_key(
        "PelletTaken"
    )
    assert normalize_event_match_key("Pellet OnPlate") == normalize_event_match_key(
        "PelletOnPlate"
    )
    assert normalize_event_match_key("any_event") == "anyevent"
