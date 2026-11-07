"""
EngineHost runs the engine on its own thread and is the only thing that touches it.

These tests use a real engine with a fake CAN bus, so they need no hardware and
no display.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from base_station.host import EngineHost, HostNotRunning
from base_station.log_manager import LogEntry, LogManager
from base_station.node_registry import NodeRegistry

from tests.test_engine import _args, _FakeCan, _RecordingEngine


@pytest.fixture
def engine(tmp_path):
    e = _RecordingEngine(_args())
    e._registry = NodeRegistry(2)
    e._log = LogManager(log_dir=str(tmp_path), auto_save=True)
    e._can = _FakeCan()
    e._exp_log_dir = str(tmp_path)
    yield e
    e._log.close()


@pytest.fixture
def host(engine):
    h = EngineHost(engine, tick_s=0.002, snapshot_hz=50)
    h.start()
    yield h
    h.stop()


def _row(text: str, node: int = 1, frame_type: str = "EVENT") -> LogEntry:
    return LogEntry(
        timestamp=time.time(), direction="RX", node_id=node, frame_type=frame_type,
        event_name=text, raw_id=0x301, raw_data=b"",
    )


# ---------------------------------------------------------------------------
# Threading contract
# ---------------------------------------------------------------------------

def test_commands_run_on_the_engine_thread(host, engine) -> None:
    name = host.call(lambda e: threading.current_thread().name)
    assert name == "sfm-engine"
    assert name != threading.current_thread().name


def test_commands_return_values_and_run_in_order(host) -> None:
    seen = []
    futures = [host.submit(lambda e, i=i: seen.append(i) or i) for i in range(20)]
    assert [f.result(2) for f in futures] == list(range(20))
    assert seen == list(range(20))


def test_a_failing_command_reports_to_its_caller_and_the_loop_survives(host) -> None:
    def boom(engine):
        raise ValueError("bad request")

    with pytest.raises(ValueError, match="bad request"):
        host.call(boom)
    assert host.running
    assert host.call(lambda e: "still alive") == "still alive"


def test_a_failing_tick_does_not_stop_the_loop(host, engine) -> None:
    calls = {"n": 0}

    def bad_tick():
        calls["n"] += 1
        raise RuntimeError("tick blew up")

    engine.tick = bad_tick
    deadline = time.monotonic() + 2
    while calls["n"] < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls["n"] >= 5, "the loop kept ticking after exceptions"
    assert host.error_count >= 5
    assert "tick blew up" in (host.last_error or "")
    assert host.call(lambda e: 1) == 1


def test_call_from_the_engine_thread_does_not_deadlock(host) -> None:
    assert host.call(lambda e: host.call(lambda e2: "inner")) == "inner"


def test_ticks_keep_running_while_idle(host, engine) -> None:
    calls = {"n": 0}
    real = engine.tick

    def counting():
        calls["n"] += 1
        real()

    engine.tick = counting
    time.sleep(0.2)
    assert calls["n"] > 20, "a 2 ms tick should run many times in 200 ms"


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------

def test_submit_after_stop_fails_cleanly(engine) -> None:
    h = EngineHost(engine)
    h.start()
    h.stop()
    assert not h.running
    with pytest.raises(HostNotRunning):
        h.call(lambda e: 1)


def test_stop_fails_commands_still_queued(engine) -> None:
    h = EngineHost(engine, tick_s=0.002)
    h.start()
    release = threading.Event()
    blocker = h.submit(lambda e: release.wait(5))
    waiting = h.submit(lambda e: "never runs")
    threading.Timer(0.1, release.set).start()
    h.stop()
    blocker.result(1)
    # Either it ran just before the stop, or it was failed. It must not hang.
    try:
        assert waiting.result(1) == "never runs"
    except HostNotRunning:
        pass


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def test_snapshot_is_json_safe_and_describes_the_nodes(host) -> None:
    v = host.wait_for_snapshot(0, timeout=2)
    assert v > 0
    snap = host.snapshot()
    json.dumps(snap)  # must not raise
    assert snap["connected"] is True
    assert [n["id"] for n in snap["nodes"]] == [1, 2]
    assert snap["nodes_online"] == 0
    assert snap["session"]["running"] is False
    assert snap["session"]["armed"] is False
    assert [i["which"] for i in snap["bnc"]["inputs"]] == ["IN1", "IN2"]
    assert snap["bnc"]["out"]["mapping"] == "legacy"


def test_snapshot_version_advances(host) -> None:
    v1 = host.wait_for_snapshot(0, timeout=2)
    v2 = host.wait_for_snapshot(v1, timeout=2)
    assert v2 > v1


def test_snapshot_rate_is_limited(engine) -> None:
    h = EngineHost(engine, tick_s=0.001, snapshot_hz=10)
    h.start()
    try:
        time.sleep(1.0)
        # ~10 Hz for one second, plus the first; never one per tick.
        assert 5 <= h.snapshot_version <= 14
    finally:
        h.stop()


def test_snapshot_shows_a_running_session(host, engine) -> None:
    exp = next(d for d in engine._exp_defs if d.name == "free_feeding")
    started, msg = host.call(
        lambda e: e.start_experiment(exp, "snap_test", lambda: (exp.defaults(), [1, 2]))
    )
    assert started, msg
    v = host.snapshot_version
    host.wait_for_snapshot(v, timeout=2)
    snap = host.snapshot()
    assert snap["session"]["running"] is True
    assert snap["session"]["name"] == "snap_test"
    assert snap["session"]["experiment"] == "free_feeding"
    assert isinstance(snap["session"]["elapsed_s"], float)
    json.dumps(snap)
    host.call(lambda e: e.stop_experiment())
    host.wait_for_snapshot(host.snapshot_version, timeout=2)
    assert host.snapshot()["session"]["running"] is False


def test_snapshot_builds_with_no_registry_and_no_can(host, engine) -> None:
    engine._registry = None
    engine._can = None
    host.wait_for_snapshot(host.snapshot_version, timeout=2)
    snap = host.snapshot()
    assert host.running
    assert snap["nodes"] == []
    assert snap["connected"] is False


# ---------------------------------------------------------------------------
# Log rows
# ---------------------------------------------------------------------------

def test_rows_after_walks_forward_without_skipping(host, engine) -> None:
    host.call(lambda e: [e._log.add(_row(f"r{i}")) for i in range(5)])
    first = host.rows_after(0, limit=3)
    assert [r["event"] for r in first] == ["r0", "r1", "r2"]
    second = host.rows_after(first[-1]["seq"], limit=3)
    assert [r["event"] for r in second] == ["r3", "r4"]
    assert host.rows_after(second[-1]["seq"]) == []


def test_rows_after_filters_by_node_and_type(host) -> None:
    def add(e):
        e._log.add(_row("a", node=1, frame_type="EVENT"))
        e._log.add(_row("b", node=2, frame_type="COMMAND"))
        e._log.add(_row("c", node=2, frame_type="EVENT"))
    host.call(add)
    assert [r["event"] for r in host.rows_after(0, node_id=2)] == ["b", "c"]
    assert [r["event"] for r in host.rows_after(0, frame_type="EVENT")] == ["a", "c"]


def test_row_numbers_survive_a_clear(host) -> None:
    host.call(lambda e: e._log.add(_row("old")))
    seq = host.rows_after(0)[0]["seq"]
    host.call(lambda e: (e._log.clear(), e._log.add(_row("new"))))
    rows = host.rows_after(seq)
    assert [r["event"] for r in rows] == ["new"]
    assert rows[0]["seq"] > seq
