"""
bnc_loopback.py — measure how accurate the BNC timing really is, on a Pi.

Cable BNC OUT to BNC IN 0 (any BNC cable, or a short jumper at the connectors),
then run this on the base station. Both ends are timed by the kernel, so the
numbers describe the wire, not what Python believes happened.

    python -m bench.bnc_loopback pulses                # BNC OUT pulse accuracy
    python -m bench.bnc_loopback event --can vcan0     # node event -> BNC OUT pulse

``pulses`` asks for N pulses of a given width and compares what was asked for
with what the input saw on the wire:

    request_to_edge_us   from asking for the pulse to the line rising
    width_error_us       measured width minus requested width

``event`` measures the whole path that matters for syncing with other
equipment: a frame reaches the base station, the CAN receive thread pulses BNC
OUT, and the input sees the line rise.

    event_to_pulse_us    from the kernel's arrival stamp on the CAN frame to
                         the kernel's stamp on the rising edge

It does not include the time the node takes to send an event, or the CAN bus
time (about 0.5 ms a frame at 250 kbit/s). Those are measured separately.

This needs libgpiod, because the RPi.GPIO fallback gives no edge timestamps,
and it needs the real BNC hardware. Run it with the browser open, closed and
the CPU busy (``--load``) to see whether anything else disturbs the timing.
The targets are proposals to check against, not guarantees; the tool reports
the measured numbers and says plainly whether they meet them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Pure analysis: no hardware, tested without it
# ---------------------------------------------------------------------------


def summarize(values: Sequence[float]) -> Dict[str, Any]:
    """Count, mean and the percentiles that matter for timing. All in the unit given."""
    data = sorted(float(v) for v in values)
    n = len(data)
    if n == 0:
        return {"n": 0}

    def pct(p: float) -> float:
        # Nearest-rank percentile: the value at least p% of samples are at or below.
        rank = max(1, math.ceil(p / 100.0 * n))
        return data[rank - 1]

    mean = sum(data) / n
    var = sum((x - mean) ** 2 for x in data) / n
    return {
        "n": n, "min": data[0], "p50": pct(50), "p95": pct(95), "p99": pct(99),
        "max": data[-1], "mean": mean, "stdev": math.sqrt(var),
    }


@dataclass(frozen=True)
class Edge:
    """A logical edge seen on the loopback input: rising means the BNC went high."""
    rising: bool
    t_mono: float
    t_wall: float


@dataclass(frozen=True)
class Pulse:
    """One pulse the output made: what was asked for and the pulse thread's own stamps."""
    requested_us: int
    t_request: float
    t_rise: float
    t_fall: float


def pair_pulses(
    pulses: Sequence[Pulse], edges: Sequence[Edge], window_s: float = 0.5
) -> Tuple[List[Dict[str, float]], int]:
    """
    Match each pulse with the rising and falling edges the input saw for it.

    A pulse is matched with the first rising edge at or after its request (and
    within ``window_s``), and the first falling edge after that. Pulses with no
    matching pair are counted as missed, not guessed at. Returns the matched
    measurements and the number missed.
    """
    rising = sorted((e for e in edges if e.rising), key=lambda e: e.t_mono)
    falling = sorted((e for e in edges if not e.rising), key=lambda e: e.t_mono)
    used_rising: set = set()
    out: List[Dict[str, float]] = []
    missed = 0
    for pulse in sorted(pulses, key=lambda p: p.t_request):
        rise = next((e for e in rising
                     if id(e) not in used_rising and pulse.t_request <= e.t_mono <= pulse.t_request + window_s),
                    None)
        fall = None
        if rise is not None:
            fall = next((e for e in falling if e.t_mono > rise.t_mono), None)
        if rise is None or fall is None:
            missed += 1
            continue
        used_rising.add(id(rise))
        observed_us = (fall.t_mono - rise.t_mono) * 1e6
        out.append({
            "request_to_edge_us": (rise.t_mono - pulse.t_request) * 1e6,
            "software_rise_to_edge_us": (rise.t_mono - pulse.t_rise) * 1e6,
            "observed_width_us": observed_us,
            "width_error_us": observed_us - pulse.requested_us,
            "software_width_error_us": (pulse.t_fall - pulse.t_rise) * 1e6 - pulse.requested_us,
        })
    return out, missed


def pair_events(
    arrivals_wall: Sequence[float], edges: Sequence[Edge], window_s: float = 0.5
) -> Tuple[List[float], int]:
    """
    For each CAN frame (kernel arrival stamp, wall clock) find the first rising
    edge after it, and return the delays in microseconds and the count with no
    edge inside ``window_s``.
    """
    rising = sorted((e for e in edges if e.rising), key=lambda e: e.t_wall)
    delays: List[float] = []
    missed = 0
    used: set = set()
    for arrived in sorted(arrivals_wall):
        edge = next((e for e in rising
                     if id(e) not in used and arrived <= e.t_wall <= arrived + window_s), None)
        if edge is None:
            missed += 1
            continue
        used.add(id(edge))
        delays.append((edge.t_wall - arrived) * 1e6)
    return delays, missed


def verdict(
    summaries: Dict[str, Dict[str, Any]], targets_us: Dict[str, float]
) -> List[Tuple[str, bool, str]]:
    """Compare p99 of each measurement with its target. Returns (name, met, sentence)."""
    results = []
    for name, target in targets_us.items():
        s = summaries.get(name)
        if not s or s.get("n", 0) == 0:
            results.append((name, False, f"{name}: no measurements"))
            continue
        worst = max(abs(s["p99"]), abs(s["min"]))
        met = worst <= target
        results.append((
            name, met,
            f"{name}: p99 {s['p99']:.0f} us, max {s['max']:.0f} us, target {target:.0f} us -> "
            + ("met" if met else "NOT met"),
        ))
    return results


def format_summary(name: str, s: Dict[str, Any]) -> str:
    if s.get("n", 0) == 0:
        return f"{name}: no data"
    return (
        f"{name:28s} n={s['n']:<5d} min={s['min']:9.1f}  p50={s['p50']:9.1f}  p95={s['p95']:9.1f}  "
        f"p99={s['p99']:9.1f}  max={s['max']:9.1f}  (us)"
    )


# ---------------------------------------------------------------------------
# The measurements (hardware)
# ---------------------------------------------------------------------------


def _open_io(channel: int):
    from base_station.io_manager import IOManager

    io = IOManager()
    io.begin()
    if io.backend != "gpiod":
        io.shutdown()
        raise SystemExit(
            f"GPIO backend is {io.backend!r}. This test needs libgpiod, because only it gives "
            "kernel edge timestamps. Install gpiod and run on the Pi."
        )
    edges: List[Edge] = []
    io.on_bnc_edge(lambda e: edges.append(Edge(e.rising, e.t_mono, e.t_wall)) if e.channel == channel else None)
    return io, edges


def _busy_load(stop: threading.Event) -> None:
    x = 0
    while not stop.is_set():
        x = (x * 31 + 7) % 1000003


def run_pulses(args: argparse.Namespace) -> Dict[str, Any]:
    io, edges = _open_io(args.in_channel)
    pulses: List[Pulse] = []
    done = threading.Event()

    def on_pulse(rec) -> None:
        pulses.append(Pulse(rec.requested_us, rec.t_request, rec.t_rise, rec.t_fall))
        done.set()

    io.on_bnc_pulse(on_pulse)
    width_us = int(args.width_ms * 1000)
    try:
        for _ in range(args.count):
            done.clear()
            if not io.pulse_bnc_out(width_us, purpose="bench"):
                raise SystemExit("pulse refused: the output was busy")
            if not done.wait(5.0):
                raise SystemExit("a pulse never completed")
            time.sleep(args.gap_ms / 1000.0)
        time.sleep(0.2)     # let the last edge arrive
    finally:
        io.shutdown()

    measured, missed = pair_pulses(pulses, edges)
    keys = ("request_to_edge_us", "software_rise_to_edge_us", "observed_width_us",
            "width_error_us", "software_width_error_us")
    summaries = {k: summarize([m[k] for m in measured]) for k in keys}
    return {
        "mode": "pulses", "requested_us": width_us, "pulses": len(pulses),
        "matched": len(measured), "missed": missed, "edges_seen": len(edges),
        "summaries": summaries,
        "targets_us": {"request_to_edge_us": args.target_latency_us,
                       "width_error_us": args.target_width_error_us},
    }


def run_event(args: argparse.Namespace) -> Dict[str, Any]:
    import can

    from base_station.can_manager import CanManager
    from base_station.protocol import CanEvent, build_event_frame, classify_frame

    io, edges = _open_io(args.in_channel)
    arrivals: List[float] = []
    width_us = int(args.width_ms * 1000)

    def tap(msg) -> None:
        if classify_frame(msg.arbitration_id) == "EVENT":
            arrivals.append(float(msg.timestamp))
            io.pulse_bnc_out(width_us, purpose="bench-event")

    manager = CanManager(interface=args.can)
    manager.start()
    manager.set_rx_tap(tap)
    sender = can.interface.Bus(channel=args.can, interface="socketcan")
    arb, data = build_event_frame(1, CanEvent.DomeOpened, bytes([1, 0, 1]))
    try:
        for _ in range(args.count):
            sender.send(can.Message(arbitration_id=arb, data=data, is_extended_id=False))
            time.sleep(max(args.gap_ms, args.width_ms * 2) / 1000.0)
        time.sleep(0.3)
    finally:
        sender.shutdown()
        manager.stop()
        io.shutdown()

    delays, missed = pair_events(arrivals, edges)
    return {
        "mode": "event", "frames": len(arrivals), "matched": len(delays), "missed": missed,
        "edges_seen": len(edges),
        "summaries": {"event_to_pulse_us": summarize(delays)},
        "targets_us": {"event_to_pulse_us": args.target_latency_us},
    }


def report(result: Dict[str, Any]) -> bool:
    """Print the result. Returns True if every target was met."""
    print()
    print(f"mode={result['mode']}  matched={result['matched']}  missed={result['missed']}  "
          f"edges seen={result['edges_seen']}")
    for name, summary in result["summaries"].items():
        print(format_summary(name, summary))
    print()
    ok = result["missed"] == 0
    if not ok:
        print(f"{result['missed']} pulse(s) had no matching edge: check the cable and the input channel.")
    for _name, met, sentence in verdict(result["summaries"], result["targets_us"]):
        print(("  met    " if met else "  NOT    ") + sentence)
        ok = ok and met
    print()
    print("Python on Linux gives no hard guarantee: judge the p99 and the max over a long run,")
    print("with the browser open and closed and the CPU busy, not one quiet run.")
    return ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Measure BNC timing on the Pi (BNC OUT cabled to a BNC IN).")
    parser.add_argument("mode", choices=("pulses", "event"))
    parser.add_argument("--count", type=int, default=1000, help="pulses or frames to send (default 1000)")
    parser.add_argument("--width-ms", type=float, default=10.0, help="pulse width in ms (default 10)")
    parser.add_argument("--gap-ms", type=float, default=50.0, help="quiet time between pulses (default 50)")
    parser.add_argument("--in-channel", type=int, default=0, choices=(0, 1), help="BNC input used for the loopback")
    parser.add_argument("--can", default="vcan0", help="CAN interface for the event test (default vcan0)")
    parser.add_argument("--target-latency-us", type=float, default=1000.0)
    parser.add_argument("--target-width-error-us", type=float, default=200.0)
    parser.add_argument("--load", action="store_true", help="keep a CPU core busy during the run")
    parser.add_argument("--out", help="also write the full result as JSON to this file")
    args = parser.parse_args(argv)

    stop = threading.Event()
    if args.load:
        threading.Thread(target=_busy_load, args=(stop,), daemon=True).start()
    try:
        result = run_pulses(args) if args.mode == "pulses" else run_event(args)
    finally:
        stop.set()
    result["load"] = bool(args.load)
    ok = report(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"wrote {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
