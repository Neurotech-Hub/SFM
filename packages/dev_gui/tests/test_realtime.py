"""Optional real-time priority for the BNC timing threads: off by default, per thread, and never fatal."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from base_station import realtime


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv(realtime.ENV, raising=False)
    realtime.reset_for_tests()
    yield
    realtime.reset_for_tests()


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(os, "sched_setscheduler", lambda pid, policy, param: seen.append((pid, policy, param.sched_priority)))
    return seen


@pytest.mark.parametrize("value,expected", [(None, 0), ("", 0), ("0", 0), ("50", 50), ("99", 99), ("100", 0), ("-3", 0), ("abc", 0), ("1", 1)])
def test_the_requested_priority_is_read_safely(monkeypatch, value, expected) -> None:
    if value is not None:
        monkeypatch.setenv(realtime.ENV, value)
    assert realtime.requested_priority() == expected


def test_nothing_changes_unless_it_was_asked_for(calls) -> None:
    assert realtime.elevate_current_thread("can-rx") is False
    assert calls == [] and realtime.status() == {"requested": 0, "threads": {}}


def test_only_the_calling_thread_is_raised_not_the_process(monkeypatch, calls) -> None:
    monkeypatch.setenv(realtime.ENV, "50")
    got = {}

    def worker():
        got["tid"] = threading.get_native_id()
        got["ok"] = realtime.elevate_current_thread("bnc-pulse")
    t = threading.Thread(target=worker)
    t.start(); t.join()
    assert got["ok"] is True
    (pid, policy, prio), = calls
    assert pid == got["tid"] and pid != os.getpid(), "the thread's own id: raising the process id would raise the main thread"
    assert policy == os.SCHED_FIFO and prio == 50
    assert realtime.status() == {"requested": 50, "threads": {"bnc-pulse": "FIFO 50"}}


def test_a_refusal_is_recorded_and_never_raises(monkeypatch) -> None:
    monkeypatch.setenv(realtime.ENV, "50")

    def refuse(pid, policy, param):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "sched_setscheduler", refuse)
    assert realtime.elevate_current_thread("can-rx") is False
    status = realtime.status()
    assert "LimitRTPRIO" in status["threads"]["can-rx"] and status["requested"] == 50
    json.dumps(status)


def test_a_system_without_the_call_is_not_an_error(monkeypatch) -> None:
    monkeypatch.setenv(realtime.ENV, "50")
    monkeypatch.delattr(os, "sched_setscheduler")
    assert realtime.elevate_current_thread("can-rx") is False
    assert "refused" in realtime.status()["threads"]["can-rx"]


def test_the_pulse_thread_asks_for_it(monkeypatch) -> None:
    from base_station.io_manager import IOManager
    labels = []
    monkeypatch.setattr(realtime, "elevate_current_thread", lambda label: labels.append(label) or False)
    io = IOManager()
    with io._pulse_cv:
        io._ensure_pulse_worker()
    deadline = time.monotonic() + 3
    while "bnc-pulse" not in labels and time.monotonic() < deadline:
        time.sleep(0.01)
    io._stop_pulse_worker()
    assert "bnc-pulse" in labels


def test_the_can_receive_thread_asks_for_it(monkeypatch) -> None:
    from base_station.can_manager import CanManager
    labels = []
    monkeypatch.setattr(realtime, "elevate_current_thread", lambda label: labels.append(label) or False)
    mgr = CanManager.__new__(CanManager)       # no bus: only the first line of the loop is of interest
    mgr._running = False
    mgr._bus = None
    mgr._rx_loop()
    assert labels == ["can-rx"]


def test_the_service_has_the_options_and_defaults_to_off() -> None:
    from base_station.service import parse_args
    a = parse_args([])
    assert a.realtime == 0 and a.gc == "freeze", "GC freeze is on by default; real-time priority is not"
    b = parse_args(["--realtime", "50", "--gc", "freeze"])
    assert b.realtime == 50 and b.gc == "freeze"


def test_what_the_timing_threads_got_is_in_the_state(monkeypatch) -> None:
    from base_station.state import build_snapshot
    from tests.test_engine import _args, _FakeCan, _RecordingEngine
    from base_station.log_manager import LogManager
    from base_station.node_registry import NodeRegistry
    e = _RecordingEngine(_args())
    e._registry = NodeRegistry(1); e._can = _FakeCan(); e._log = LogManager(auto_save=False)
    monkeypatch.setenv(realtime.ENV, "50")
    realtime._threads["can-rx"] = "FIFO 50"
    snap = build_snapshot(e)
    assert snap["timing"] == {"realtime": {"requested": 50, "threads": {"can-rx": "FIFO 50"}}, "gc": "default"}
    json.dumps(snap)
    e._log.close()


def test_the_other_engine_threads_run_a_little_below_the_timing_threads(monkeypatch, calls) -> None:
    monkeypatch.setenv(realtime.ENV, "50")
    assert realtime.elevate_current_thread("engine threads", below=realtime.OTHERS_BELOW) is True
    assert calls[-1][2] == 40
    assert realtime.status()["threads"]["engine threads"] == "FIFO 40"


def test_a_low_requested_priority_never_goes_below_one(monkeypatch, calls) -> None:
    monkeypatch.setenv(realtime.ENV, "5")
    realtime.elevate_current_thread("engine threads", below=realtime.OTHERS_BELOW)
    assert calls[-1][2] == 1


def test_threads_made_after_the_main_thread_is_raised_inherit_it() -> None:
    """The point of raising the main thread once: nothing created later needs its own call."""
    import subprocess, sys
    code = ("import os, threading\n"
            "os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(1))\n"
            "out = {}\n"
            "def w(): out['prio'] = os.sched_getparam(threading.get_native_id()).sched_priority\n"
            "t = threading.Thread(target=w); t.start(); t.join(); print(out['prio'])\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("real-time scheduling is not permitted here")      # needs RLIMIT_RTPRIO
    assert r.stdout.strip() == "1"
