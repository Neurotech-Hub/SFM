"""
IOManager: timed BNC edges and the single-owner BNC OUT pulse worker.

No GPIO hardware is needed. libgpiod and RPi.GPIO are replaced with small
fakes that behave the way the real libraries report events, so the parts that
matter (edge direction, capture time, sequence gaps, pulse ownership) are
checked on any machine.
"""

from __future__ import annotations

import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from base_station.io_manager import (
    PIN_BNC_IN1,
    PIN_BNC_IN2,
    PIN_BTN,
    BncEdgeEvent,
    IOManager,
    PulseRecord,
)


# ---------------------------------------------------------------------------
# libgpiod stand-ins
# ---------------------------------------------------------------------------

def _edge(pin: int, kind: str, stamp_ns: int, seq: int = 0):
    """What libgpiod's read_edge_events() yields: an enum-like type, a kernel stamp, a per-line seqno."""
    return SimpleNamespace(
        line_offset=pin,
        event_type=SimpleNamespace(name=f"{kind}_EDGE"),
        timestamp_ns=stamp_ns,
        line_seqno=seq,
    )


class _Request:
    """Hands over one batch of events, then asks the loop to stop."""

    def __init__(self, io: IOManager, events) -> None:
        self.io = io
        self.events = list(events)

    def wait_edge_events(self, timeout=None):
        if self.events:
            return True
        self.io._edge_thread_stop.set()
        return False

    def read_edge_events(self):
        out, self.events = self.events, []
        return out


def _run_edges(io: IOManager, events) -> list:
    seen: list = []
    io.on_bnc_edge(seen.append)
    io._edge_thread_stop.clear()
    io._gpiod_edge_loop(_Request(io, events))
    return seen


# ---------------------------------------------------------------------------
# Edge direction and capture time
# ---------------------------------------------------------------------------

def test_raw_rising_pin_edge_is_a_logical_falling_edge() -> None:
    """The HAT inverts the signal: the pin rising means the BNC went low."""
    seen = _run_edges(IOManager(), [
        _edge(PIN_BNC_IN1, "RISING", 1_000_000_000),
        _edge(PIN_BNC_IN1, "FALLING", 1_010_000_000),
    ])
    assert [e.rising for e in seen] == [False, True]
    assert all(isinstance(e, BncEdgeEvent) for e in seen)


def test_the_two_inputs_are_told_apart() -> None:
    seen = _run_edges(IOManager(), [
        _edge(PIN_BNC_IN1, "FALLING", 1_000_000_000),
        _edge(PIN_BNC_IN2, "FALLING", 2_000_000_000),
    ])
    assert [(e.channel, e.which) for e in seen] == [(0, "IN1"), (1, "IN2")]


def test_a_ten_ms_pulse_read_in_one_batch_keeps_its_width() -> None:
    """
    Both edges of a short pulse arrive in the same read. The old code re-read
    the pin level for each one and called both 'falling'. The kernel stamps
    keep them apart and 10 ms wide.
    """
    start = time.monotonic_ns()
    seen = _run_edges(IOManager(), [
        _edge(PIN_BNC_IN1, "FALLING", start, seq=1),             # BNC goes HIGH
        _edge(PIN_BNC_IN1, "RISING", start + 10_000_000, seq=2),  # BNC goes LOW
    ])
    assert [e.rising for e in seen] == [True, False]
    assert (seen[1].t_mono - seen[0].t_mono) == pytest.approx(0.010, abs=1e-9)
    assert seen[0].source == "kernel"


def test_capture_time_is_the_kernel_stamp_not_the_time_it_was_read() -> None:
    stamp = time.monotonic_ns() - 2_000_000_000   # the edge happened 2 s ago
    seen = _run_edges(IOManager(), [_edge(PIN_BNC_IN1, "FALLING", stamp)])
    event = seen[0]
    assert event.t_mono == pytest.approx(stamp / 1e9)
    # The wall time is the same instant: about two seconds in the past.
    assert time.time() - event.t_wall == pytest.approx(2.0, abs=0.5)


def test_sequence_gaps_are_reported_as_missed_edges() -> None:
    seen = _run_edges(IOManager(), [
        _edge(PIN_BNC_IN1, "FALLING", 1_000_000_000, seq=1),
        _edge(PIN_BNC_IN1, "RISING", 1_001_000_000, seq=2),
        _edge(PIN_BNC_IN1, "FALLING", 1_005_000_000, seq=5),   # 3 and 4 were lost
        _edge(PIN_BNC_IN2, "FALLING", 1_006_000_000, seq=1),   # other line, own sequence
    ])
    assert [e.missed for e in seen] == [0, 0, 2, 0]


def test_legacy_zero_argument_callbacks_still_fire() -> None:
    io = IOManager()
    hits: list = []
    io.on_bnc_in1_edge(lambda: hits.append("in1"))
    io.on_bnc_in2_edge(lambda: hits.append("in2"))
    _run_edges(io, [
        _edge(PIN_BNC_IN1, "FALLING", 1_000_000_000),
        _edge(PIN_BNC_IN2, "FALLING", 1_001_000_000),
    ])
    assert hits == ["in1", "in2"]


def test_a_failing_listener_does_not_stop_the_others() -> None:
    io = IOManager()
    got: list = []

    def broken(_event):
        raise RuntimeError("boom")

    io.on_bnc_edge(broken)
    seen = _run_edges(io, [_edge(PIN_BNC_IN1, "FALLING", 1_000_000_000)])
    assert len(seen) == 1 and got == []


def test_the_button_is_not_reported_as_a_bnc_edge() -> None:
    io = IOManager()
    pressed: list = []
    io.on_button_press(lambda: pressed.append(True))
    seen = _run_edges(io, [_edge(PIN_BTN, "FALLING", 1_000_000_000)])
    assert seen == [] and pressed == [True]


def test_rpi_gpio_fallback_reads_the_level_at_once() -> None:
    io = IOManager()
    io._gpio_backend = "RPi.GPIO"
    io._gpio = SimpleNamespace(input=lambda pin: 0, HIGH=1, LOW=0)  # pin low = BNC high
    seen: list = []
    io.on_bnc_edge(seen.append)
    io._on_rpi_bnc_edge(PIN_BNC_IN2)
    assert len(seen) == 1
    assert (seen[0].channel, seen[0].rising, seen[0].source) == (1, True, "software")


# ---------------------------------------------------------------------------
# Choosing the GPIO chip
# ---------------------------------------------------------------------------

def _fake_gpiod(labels: dict) -> ModuleType:
    module = ModuleType("gpiod")

    class Chip:
        def __init__(self, path):
            if path not in labels:
                raise OSError(path)
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_info(self):
            return SimpleNamespace(label=labels[self.path])

    module.Chip = Chip
    return module


def test_the_header_chip_is_picked_by_label(monkeypatch) -> None:
    labels = {"/dev/gpiochip0": "gpio-brcmstb", "/dev/gpiochip4": "pinctrl-rp1"}
    monkeypatch.setitem(sys.modules, "gpiod", _fake_gpiod(labels))
    monkeypatch.setattr("base_station.io_manager.os.listdir", lambda p: ["gpiochip0", "gpiochip4", "null"])
    assert IOManager._find_gpiochip() == "/dev/gpiochip4"


def test_the_chip_search_falls_back_to_the_usual_paths(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "gpiod", _fake_gpiod({}))
    monkeypatch.setattr("base_station.io_manager.os.listdir", lambda p: ["gpiochip4"])
    monkeypatch.setattr("base_station.io_manager.os.path.exists", lambda p: p == "/dev/gpiochip4")
    assert IOManager._find_gpiochip() == "/dev/gpiochip4"


# ---------------------------------------------------------------------------
# BNC OUT pulses
# ---------------------------------------------------------------------------

class _PulseProbe:
    """Collects PulseRecords and the order of writes to the pin."""

    def __init__(self, io: IOManager) -> None:
        self.records: list = []
        self.writes: list = []
        self.done = threading.Event()
        io.on_bnc_pulse(self._on_pulse)
        io._write_output = lambda pin, high: self.writes.append((pin, high))

    def _on_pulse(self, record: PulseRecord) -> None:
        self.records.append(record)
        self.done.set()


def test_a_pulse_goes_high_then_low_and_is_reported() -> None:
    io = IOManager()
    probe = _PulseProbe(io)
    assert io.pulse_bnc_out(30_000, purpose="sync") is True
    assert probe.done.wait(2.0)
    io.shutdown()

    record = probe.records[0]
    assert record.purpose == "sync" and record.requested_us == 30_000
    assert [high for _pin, high in probe.writes[:2]] == [True, False]
    assert 0.025 <= record.width_us / 1e6 < 0.2     # generous: CI machines are noisy
    assert record.t_request <= record.t_rise < record.t_fall
    assert record.latency_us < 200_000


def test_a_second_pulse_while_one_runs_is_refused_and_does_not_cut_it_short() -> None:
    io = IOManager()
    probe = _PulseProbe(io)
    assert io.pulse_bnc_out(80_000, purpose="long") is True
    assert io.bnc_out_busy
    assert io.pulse_bnc_out(1_000, purpose="short") is False
    assert probe.done.wait(2.0)
    io.shutdown()

    assert [r.purpose for r in probe.records] == ["long"]
    assert probe.records[0].width_us / 1e6 >= 0.07   # the refused pulse did not end it early
    assert [high for _pin, high in probe.writes[:2]] == [True, False]


def test_pulses_can_follow_one_another() -> None:
    io = IOManager()
    probe = _PulseProbe(io)
    for name in ("a", "b", "c"):
        probe.done.clear()
        assert io.pulse_bnc_out(5_000, purpose=name) is True
        assert probe.done.wait(2.0)
        deadline = time.monotonic() + 1.0
        while io.bnc_out_busy and time.monotonic() < deadline:
            time.sleep(0.001)
    io.shutdown()
    assert [r.purpose for r in probe.records] == ["a", "b", "c"]


def test_pulse_width_is_kept_in_range() -> None:
    io = IOManager()
    probe = _PulseProbe(io)
    assert io.pulse_bnc_out(0) is True
    assert probe.done.wait(2.0)
    io.shutdown()
    assert probe.records[0].requested_us == 1


def test_the_worker_restarts_after_shutdown() -> None:
    io = IOManager()
    probe = _PulseProbe(io)
    io.pulse_bnc_out(2_000, purpose="first")
    assert probe.done.wait(2.0)
    io.shutdown()
    probe.done.clear()
    assert io.pulse_bnc_out(2_000, purpose="second") is True
    assert probe.done.wait(2.0)
    io.shutdown()
    assert [r.purpose for r in probe.records] == ["first", "second"]


def test_a_failing_pulse_listener_does_not_kill_the_worker() -> None:
    io = IOManager()
    io._write_output = lambda pin, high: None
    io.on_bnc_pulse(lambda record: (_ for _ in ()).throw(RuntimeError("boom")))
    done = threading.Event()
    io.on_bnc_pulse(lambda record: done.set())
    io.pulse_bnc_out(2_000)
    assert done.wait(2.0)
    io.shutdown()
