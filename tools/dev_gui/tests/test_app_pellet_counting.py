"""
End-to-end check that the base station, not the node, owns the pellet numbers
that reach the log.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from base_station.protocol import (
    CanEvent,
    DispenseState,
    HeartbeatPayload,
    ServiceStatus,
    build_event_frame,
    build_heartbeat_frame,
)


def _msg(arb_id: int, data: bytes):
    return SimpleNamespace(arbitration_id=arb_id, data=bytes(data))


def _make_app(num_nodes: int = 2):
    from base_station.app import SFMApp
    from base_station.log_manager import LogManager
    from base_station.node_registry import NodeRegistry

    app = SFMApp(
        argparse.Namespace(
            interface="can0", bitrate=250000, nodes=num_nodes, log_dir="~/sfm_logs"
        )
    )
    app._registry = NodeRegistry(num_nodes)
    app._log = LogManager(auto_save=False)
    # Reads a dpg input widget; without a GUI context dearpygui hard-crashes
    # the interpreter rather than raising. Nothing here depends on it.
    app._ensure_node_heartbeat_config = lambda node_id: None
    return app


@pytest.fixture
def app():
    try:
        return _make_app()
    except ModuleNotFoundError as exc:
        if "dearpygui" in str(exc).lower():
            pytest.skip("dearpygui not installed")
        raise


def _feed(app, node: int, event: CanEvent, extra: bytes) -> None:
    arb, data = build_event_frame(node, event, extra)
    app._dispatch_rx(_msg(arb, data))


def _hb(app, node: int, presented: int, taken: int) -> None:
    payload = HeartbeatPayload(
        dispense_state=DispenseState.Idle,
        pellets_presented=presented,
        mouse_presence=False,
        pellet=False,
        load_position=False,
        dome_open=False,
        fault_code=ServiceStatus.Ok,
        pellets_taken=taken,
    )
    arb, data = build_heartbeat_frame(node, payload)
    app._dispatch_rx(_msg(arb, data))


def _rows(app, event_name: str):
    return [e for e in app._log.all_entries() if e.event_name == event_name]


def test_first_pellet_of_a_run_is_pellet_one(app) -> None:
    """Node has been powered all morning; the log still starts at 1."""
    _feed(app, 1, CanEvent.Loaded, bytes([0x67, 0x01]))  # node count 359
    row = _rows(app, "Loaded")[0]
    assert row.fields["session_pellets"] == 1
    assert row.fields["node_pellets"] == 359
    # The node's number must not be the headline in the details column.
    assert "session_pellets=1" in row.details


def test_reset_makes_the_next_run_start_over(app) -> None:
    _feed(app, 1, CanEvent.Loaded, bytes([10, 0]))
    _feed(app, 1, CanEvent.Loaded, bytes([11, 0]))
    assert app._pellets.presented(1) == 2

    app._pellets.reset()  # what _on_experiment_start does on open_session
    _feed(app, 1, CanEvent.Loaded, bytes([12, 0]))
    assert app._pellets.presented(1) == 1


def test_taken_is_counted_on_its_own_counter(app) -> None:
    _feed(app, 1, CanEvent.Loaded, bytes([40, 0]))
    _feed(app, 1, CanEvent.PelletTaken, bytes([7, 0, 1]))
    row = _rows(app, "Pellet Taken")[0]
    assert row.fields["session_taken"] == 1
    assert row.fields["node_taken"] == 7
    assert row.fields["dome_open"] is True
    assert app._pellets.presented(1) == 1


def test_dropped_frame_is_corrected_and_audited(app) -> None:
    _feed(app, 1, CanEvent.Loaded, bytes([20, 0]))
    # Pellet 21's Loaded never arrives; its DomeOpened does.
    _feed(app, 1, CanEvent.DomeOpened, bytes([21, 0, 1]))

    assert app._pellets.presented(1) == 2
    audit = _rows(app, "Pellet Count Gap")
    assert len(audit) == 1
    assert audit[0].fields["missed_frames"] == 1
    assert audit[0].fields["session_total"] == 2
    assert audit[0].node_id == 1


def test_heartbeat_recovers_pellets_missed_while_offline(app) -> None:
    _feed(app, 1, CanEvent.Loaded, bytes([5, 0]))          # pellet 1 of the run
    _feed(app, 1, CanEvent.PelletTaken, bytes([2, 0, 1]))  # take 1 of the run
    # Bus drops out; the next heartbeat shows three more presented and one more
    # taken than we ever saw event frames for.
    _hb(app, 1, presented=8, taken=3)

    assert app._pellets.presented(1) == 4
    assert app._pellets.taken(1) == 2
    hb_row = [e for e in app._log.all_entries() if e.frame_type == "HEARTBEAT"][0]
    assert hb_row.fields["session_pellets"] == 4
    assert hb_row.fields["node_pellets"] == 8
    gaps = _rows(app, "Pellet Count Gap")
    assert {g.fields["missed_frames"] for g in gaps} == {3, 1}


def test_counters_first_seen_in_a_heartbeat_are_pre_run_history(app) -> None:
    """
    A node that has been dispensing all morning joins a fresh run. Its first
    heartbeat is a baseline, not four pellets that belong to this session.
    """
    _hb(app, 1, presented=412, taken=400)
    assert app._pellets.presented(1) == 0
    assert app._pellets.taken(1) == 0
    assert _rows(app, "Pellet Count Gap") == []

    _feed(app, 1, CanEvent.Loaded, bytes([413 & 0xFF, 413 >> 8]))
    assert app._pellets.presented(1) == 1


def test_node_reboot_is_audited_not_counted_as_thousands(app) -> None:
    _feed(app, 1, CanEvent.Loaded, bytes([200, 0]))
    _feed(app, 1, CanEvent.Loaded, bytes([1, 0]))  # rebooted, counter restarted

    assert app._pellets.presented(1) == 2
    audit = _rows(app, "Pellet Counter Restart")
    assert len(audit) == 1
    assert audit[0].fields["node_counter_restarted"] is True


def test_nodes_do_not_share_a_counter(app) -> None:
    _feed(app, 1, CanEvent.Loaded, bytes([100, 0]))
    _feed(app, 2, CanEvent.Loaded, bytes([3, 0]))
    _feed(app, 2, CanEvent.Loaded, bytes([4, 0]))
    assert app._pellets.presented(1) == 1
    assert app._pellets.presented(2) == 2
