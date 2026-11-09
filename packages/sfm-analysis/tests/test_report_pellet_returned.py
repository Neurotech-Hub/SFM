"""Pellet Returned (firmware 1.7): a take the node later retracts is not a take."""

from report_fixtures import can_event_row, exp_row, session_open_row, write_session

from sfm_analysis.protocol import (
    CAN_EVENT_DISPLAY_NAME,
    CanEvent,
    EventPayload,
    parse_event_context,
)
from sfm_analysis.report import load_runs
from sfm_analysis.report.session import RETURNED_TAKE_NAME


def _load(tmp_path, rows):
    path = write_session(tmp_path, rows)
    (run,) = load_runs(path)
    return run


def test_returned_take_is_renamed_and_real_take_kept(tmp_path) -> None:
    run = _load(tmp_path, [
        session_open_row(1000, "S", 1),
        exp_row(1001, "session_start", {"experiment": "x", "nodes": [1, 2],
                                         "macs": {"1": "AA:BB:CC:DD:EE:01", "2": "unknown"}}),
        can_event_row(2000, 1, CanEvent.Loaded, b"\x01\x00"),
        can_event_row(3000, 1, CanEvent.PelletTaken, b"\x01\x00\x01"),
        can_event_row(4000, 1, CanEvent.PelletReturned, b"\x01\x00"),
        can_event_row(9000, 1, CanEvent.PelletTaken, b"\x01\x00\x01"),
        # node 2: an ordinary take, then a new cycle — nothing to retract
        can_event_row(2000, 2, CanEvent.Loaded, b"\x01\x00"),
        can_event_row(2500, 2, CanEvent.PelletTaken, b"\x01\x00\x01"),
        exp_row(10000, "session_end", {}),
    ])
    takes = run.by_event("Pellet Taken")
    assert [(r.node_id, r.ts_ms) for r in takes] == [(2, 2500), (1, 9000)]
    assert [r.ts_ms for r in run.by_event(RETURNED_TAKE_NAME)] == [3000]
    assert any("Pellet Returned" in n for n in run.notes)
    assert run.macs == {1: "AA:BB:CC:DD:EE:01"}


def test_return_after_a_new_cycle_does_not_rename_an_old_take(tmp_path) -> None:
    run = _load(tmp_path, [
        session_open_row(1000, "S", 1),
        can_event_row(2000, 1, CanEvent.PelletTaken, b"\x01\x00\x01"),
        can_event_row(3000, 1, CanEvent.Lowering, b"\x01\x00"),
        can_event_row(4000, 1, CanEvent.PelletReturned, b"\x02\x00"),
    ])
    assert len(run.by_event("Pellet Taken")) == 1


def test_protocol_decodes_new_wire_values() -> None:
    assert CAN_EVENT_DISPLAY_NAME[CanEvent.PelletReturned] == "Pellet Returned"
    ctx = parse_event_context(EventPayload(event=CanEvent.FeedSkipped, raw_extra=b"\x05\x00\x01"))
    assert ctx == {"pellet_count": 5, "presented": True}
    legacy = parse_event_context(EventPayload(event=CanEvent.FeedSkipped, raw_extra=b"\x05\x00"))
    assert "presented" not in legacy
    ret = parse_event_context(EventPayload(event=CanEvent.PelletReturned, raw_extra=b"\x07\x00"))
    assert ret == {"pellet_count": 7}
