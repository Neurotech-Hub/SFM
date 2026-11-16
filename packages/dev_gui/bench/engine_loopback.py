"""
engine_loopback.py — measure the REAL engine's BNC timing, end to end.

``bnc_loopback.py`` times the BNC code on its own. This times the whole engine the way an
experiment uses it: a node event reaches the base station, the engine's CAN receive thread asks
the pulse thread for a BNC OUT pulse, and the engine's own BNC input sees the line rise.

    python -m bench.engine_loopback --count 1000
    python -m bench.engine_loopback --realtime 50 --gc freeze --load-procs 4

It starts its own engine (``python -m base_station.service``) with scratch settings, so the
operator's saved BNC settings, node map and logs are never touched, connects it to a virtual CAN
bus, sets BNC OUT to follow "Dome Opened" and BNC IN 0 to log every edge, then sends node events
from this process. The delay it reports comes out of the engine's own log:

    frame_sent_to_edge_us   from this process sending the frame (CLOCK_MONOTONIC) to the kernel's
                            stamp on BNC OUT rising, as logged by the engine

BNC OUT must be cabled to BNC IN 0, and nothing else may be using the BNC lines: stop the
engine service first (``systemctl --user stop sfm-engine``). Use ``vcan0``, never ``can0``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bench.bnc_loopback import summarize, tail_counts, verdict, worst, start_load_processes, format_summary

TARGETS_US = {"frame_sent_to_edge_us": 1000.0, "csv_ms_diff_us": 1000.0, "logged_minus_edge_us": 1000.0}


# ---------------------------------------------------------------------------
# Pure analysis
# ---------------------------------------------------------------------------

def pair_sends(
    sends: Sequence[float], rising: Sequence[float], falling: Sequence[float], window_s: float = 0.5
) -> Tuple[List[Dict[str, float]], int]:
    """
    Match each send time (monotonic seconds) with the first unused rising edge after it, and the
    falling edge after that. Returns the measurements in microseconds and the number of sends with
    no pulse inside ``window_s``.
    """
    rising = sorted(rising)
    falling = sorted(falling)
    out: List[Dict[str, float]] = []
    missed = 0
    used = set()
    for sent in sorted(sends):
        rise = next((i for i, t in enumerate(rising) if i not in used and sent <= t <= sent + window_s), None)
        fall = next((t for t in falling if rise is not None and t > rising[rise]), None)
        if rise is None or fall is None:
            missed += 1
            continue
        used.add(rise)
        out.append({
            "frame_sent_to_edge_us": (rising[rise] - sent) * 1e6,
            "observed_width_us": (fall - rising[rise]) * 1e6,
        })
    return out, missed


def read_bnc_rows(csv_path: Path, channel_name: str = "IN1") -> Dict[str, Any]:
    """Pull the BNC edges and pulse records out of the engine's CSV log."""
    rising: List[float] = []
    falling: List[float] = []
    pulses: List[Dict[str, Any]] = []
    out_rows: List[Dict[str, float]] = []       # BNC OUT pulse rows as written: CSV ms + engine fields
    in_rows: List[Dict[str, float]] = []        # IN rising rows as written: CSV ms + kernel t_mono
    warnings: List[str] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["frame_type"] == "WARNING" and "BNC" in row["event_name"]:
                warnings.append(f"{row['event_name']}: {row['details']}")
            if row["frame_type"] != "BNC":
                continue
            try:
                fields = json.loads(row["fields_json"] or "{}")
            except ValueError:
                continue
            name = row["event_name"]
            if name == f"{channel_name} rising" and "t_mono" in fields:
                rising.append(float(fields["t_mono"]))
                in_rows.append({"ms": int(row.get("timestamp_ms") or 0), "t_mono": float(fields["t_mono"])})
            elif name == f"{channel_name} falling" and "t_mono" in fields:
                falling.append(float(fields["t_mono"]))
            elif name == "BNC OUT pulse":
                pulses.append(fields)
                if "t_mono_write_start" in fields:
                    out_rows.append({"ms": int(row.get("timestamp_ms") or 0), **{k: float(fields[k]) for k in (
                        "t_mono_write_start", "t_mono_rise", "rise_uncertainty_us")}})
            elif name == "BNC OUT pulse refused":
                warnings.append(f"pulse refused: {row['details']}")
    return {"rising": rising, "falling": falling, "pulses": pulses, "warnings": warnings,
            "out_rows": out_rows, "in_rows": in_rows}


def pair_csv_rows(
    out_rows: Sequence[Dict[str, float]], in_rows: Sequence[Dict[str, float]], window_s: float = 0.01
) -> Tuple[List[Dict[str, float]], int]:
    """
    Compare what the CSV says about each BNC OUT pulse with the kernel's stamp of the same edge
    (the loopback input's rising row). Returns, in microseconds:

    ``csv_ms_diff_us``      OUT row ``timestamp_ms`` minus IN row ``timestamp_ms``: what someone
                            reading the CSV sees. Each is rounded to a millisecond, so this moves in
                            1000 us steps and is only meaningful as a bound.
    ``logged_minus_edge_us`` the time the OUT row records (middle of the pin write) minus the kernel
                            edge stamp, at full resolution, from the rows' own fields.
    ``edge_outside_us``     how far the kernel edge lies outside the write window (0 = inside).
    """
    ins = sorted(in_rows, key=lambda r: r["t_mono"])
    out: List[Dict[str, float]] = []
    missed = 0
    for o in out_rows:
        rise = o["t_mono_rise"]
        edge = min(ins, key=lambda i: abs(i["t_mono"] - rise), default=None)   # nearest, not first: pulses can be 50 ms apart
        if edge is None or abs(edge["t_mono"] - rise) > window_s:
            missed += 1
            continue
        mid = (o["t_mono_write_start"] + rise) / 2
        e = edge["t_mono"]
        out.append({
            "csv_ms_diff_us": (o["ms"] - edge["ms"]) * 1000.0,
            "logged_minus_edge_us": (mid - e) * 1e6,
            "edge_outside_us": max(0.0, (o["t_mono_write_start"] - e) * 1e6, (e - rise) * 1e6),
        })
    return out, missed


# ---------------------------------------------------------------------------
# Talking to the engine over its socket
# ---------------------------------------------------------------------------

class EngineClient:
    """A minimal client for the engine service's one-JSON-object-per-line protocol."""

    def __init__(self, path: str, timeout: float = 30.0) -> None:
        self._sock = socket.socket(socket.AF_UNIX)
        self._sock.settimeout(timeout)
        self._sock.connect(path)
        self._buf = b""
        self._next = 0
        self.state: Dict[str, Any] = {}

    def _lines(self):
        while True:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                yield json.loads(line)
            chunk = self._sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("the engine closed the connection")
            self._buf += chunk

    def call(self, cmd: str, **args: Any) -> Dict[str, Any]:
        self._next += 1
        rid = self._next
        self._sock.sendall(json.dumps({"id": rid, "cmd": cmd, "args": args}).encode() + b"\n")
        for msg in self._lines():
            if "state" in msg:
                self.state = msg["state"]
            elif msg.get("id") == rid:
                return msg["result"]
        raise ConnectionError("no reply")

    def refresh(self, seconds: float = 0.4) -> Dict[str, Any]:
        """Read pushed state for a moment and return the newest."""
        end = time.monotonic() + seconds
        self._sock.settimeout(0.2)
        try:
            while time.monotonic() < end:
                try:
                    for msg in self._lines():
                        if "state" in msg:
                            self.state = msg["state"]
                        break
                except socket.timeout:
                    pass
        finally:
            self._sock.settimeout(30.0)
        return self.state

    def close(self) -> None:
        self._sock.close()


def engine_command(sock_path: str, home: str, realtime: int, gc_mode: str, grant_rt: bool, switch_interval_us: Optional[float] = None) -> List[str]:
    cmd = [sys.executable, "-m", "base_station.service", "--socket", sock_path, "--gc", gc_mode]
    if switch_interval_us:
        cmd += ["--switch-interval-us", str(switch_interval_us)]
    if realtime:
        cmd += ["--realtime", str(realtime)]
    env = ["env", f"HOME={home}", f"PYTHONUSERBASE={os.path.expanduser('~/.local')}", f"XDG_RUNTIME_DIR={os.path.dirname(sock_path)}"]
    if grant_rt:
        # Let the unprivileged engine raise its own timing threads, as a real-time limit in
        # LimitRTPRIO= or limits.d would. The engine itself stays an ordinary user.
        user = os.environ.get("USER") or os.getlogin()
        return ["sudo", "-n", "prlimit", "--rtprio=99:99", "sudo", "-n", "-u", user, *env, *cmd]
    return [*env, *cmd]


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> Dict[str, Any]:
    import can
    from base_station.protocol import CanEvent, build_event_frame

    work = Path(tempfile.mkdtemp(prefix="sfmeng-", dir=os.path.expanduser("~/.cache") if os.path.isdir(os.path.expanduser("~/.cache")) else None))
    home, logs = work / "home", work / "logs"
    home.mkdir()
    sock_path = str(work / "e.sock")
    procs, mp_stop = start_load_processes(args.load_procs) if args.load_procs else ([], None)   # before the engine: it must not inherit anything
    engine = None
    client = None
    try:
        engine = subprocess.Popen(engine_command(sock_path, str(home), args.realtime, args.gc, args.grant_rt, args.switch_interval_us),
                                  stdout=subprocess.DEVNULL, stderr=open(work / "engine.log", "w"), cwd=str(Path(__file__).resolve().parents[1]))
        for _ in range(100):
            if os.path.exists(sock_path):
                break
            if engine.poll() is not None:
                raise SystemExit("the engine did not start:\n" + (work / "engine.log").read_text()[-600:])
            time.sleep(0.1)
        client = EngineClient(sock_path)
        r = client.call("connect", interface=args.can, num_nodes=2, log_dir=str(logs), auto_save=True)
        if not r["ok"]:
            raise SystemExit(f"could not connect the engine: {r['error']}")
        state = client.refresh()
        if state.get("gpio_backend") != "gpiod":
            raise SystemExit(f"GPIO backend is {state.get('gpio_backend')!r}: this needs the real BNC hardware and libgpiod. "
                             "Is another process (the engine service?) holding the BNC lines?")
        for step in (client.call("set_bnc_input", which="IN1", enabled=True, mode="log_only"),):
            if not step["ok"]:
                raise SystemExit(step["error"])
        triggers = client.call("bnc_out_triggers")["triggers"]
        trigger = next((t for t in triggers if t.lower().replace(" ", "") == "domeopened"), None)
        if trigger is None:
            raise SystemExit("this engine has no 'Dome Opened' event to follow")
        r = client.call("set_bnc_out", mapping="node_event", trigger=trigger, width_us=int(args.width_ms * 1000))
        if not r["ok"]:
            raise SystemExit(r["error"])
        state = client.refresh()

        bus = can.interface.Bus(channel=args.can, interface="socketcan")
        arb, data = build_event_frame(1, CanEvent.DomeOpened, bytes([1, 0, 1]))
        msg = can.Message(arbitration_id=arb, data=data, is_extended_id=False)
        sends: List[float] = []
        if args.sender_rt:
            # The sender must not be held up by the busy programs between noting the time and sending,
            # or that delay would be blamed on the engine. Raised only now, after the engine was started,
            # so the engine does not inherit it.
            try:
                os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(args.sender_rt))
            except PermissionError:
                raise SystemExit("--sender-rt needs a real-time limit: run this under 'sudo prlimit --rtprio=99:99 ...'") from None
        gen = None
        if args.can_traffic_fps:
            gap_ms = max(0.1, 1000.0 / args.can_traffic_fps)
            gen = subprocess.Popen(["cangen", args.can, "-g", f"{gap_ms:g}", "-I", "201", "-L", "8", "-D", "r"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
        try:
            for _ in range(args.count):
                sends.append(time.monotonic())
                bus.send(msg)
                time.sleep(max(args.gap_ms, args.width_ms * 2) / 1000.0)
            time.sleep(0.5)
        finally:
            if gen is not None:
                gen.terminate()
                gen.wait(5)
            bus.shutdown()
        state = client.refresh()
        csv_path = Path(state["log"]["csv_path"])
        timing = state.get("timing", {})
        client.close(); client = None
        engine.send_signal(signal.SIGTERM)                  # a clean stop flushes the log
        try:
            engine.wait(30)
        except subprocess.TimeoutExpired:
            engine.kill()
        data_rows = read_bnc_rows(csv_path)
    finally:
        if client:
            client.close()
        if engine and engine.poll() is None:
            engine.kill()
        if procs:
            mp_stop.set()
            for p in procs:
                p.join(2)
                if p.is_alive():
                    p.kill()
        shutil.rmtree(work, ignore_errors=True) if not args.keep else print(f"kept: {work}")

    measured, missed = pair_sends(sends, data_rows["rising"], data_rows["falling"])
    keys = ("frame_sent_to_edge_us", "observed_width_us")
    pulse_lat = [p["latency_us"] for p in data_rows["pulses"] if "latency_us" in p]
    csv_pairs, csv_unpaired = pair_csv_rows(data_rows["out_rows"], data_rows["in_rows"])
    csv_keys = ("csv_ms_diff_us", "logged_minus_edge_us", "edge_outside_us")
    return {
        "mode": "engine", "frames": len(sends), "matched": len(measured), "missed": missed,
        "edges_seen": len(data_rows["rising"]) + len(data_rows["falling"]),
        "summaries": {**{k: summarize([m[k] for m in measured]) for k in keys},
                      "engine_request_to_rise_us": summarize(pulse_lat),
                      **{k: summarize([m[k] for m in csv_pairs]) for k in csv_keys}},
        "csv_rows_paired": len(csv_pairs), "csv_rows_unpaired": csv_unpaired,
        "samples": {**{k: [round(m[k], 1) for m in measured] for k in keys},
                    **{k: [round(m[k], 1) for m in csv_pairs] for k in csv_keys}},
        "targets_us": dict(TARGETS_US), "warnings": data_rows["warnings"],
        "conditions": {"realtime": args.realtime, "gc": args.gc, "busy_processes": args.load_procs,
                       "can_traffic_fps": args.can_traffic_fps, "switch_interval_us": args.switch_interval_us, "sender_rt": args.sender_rt,
                       "engine_reports": timing},
    }


def report(result: Dict[str, Any]) -> bool:
    print()
    print(f"mode=engine  matched={result['matched']}  missed={result['missed']}  edges seen={result['edges_seen']}")
    c = result["conditions"]
    print(f"conditions: realtime={c['realtime']}, gc={c['gc']}, busy_processes={c['busy_processes']}, "
          f"can_traffic_fps={c['can_traffic_fps']}, switch_interval_us={c['switch_interval_us']}, sender_rt={c['sender_rt']}, engine says: {c['engine_reports']}")
    for name, s in result["summaries"].items():
        print(format_summary(name, s))
    samples = result["samples"]["frame_sent_to_edge_us"]
    if samples:
        print("frame_sent_to_edge_us: slower than " + ", ".join(f"{int(k[5:]) / 1000:g} ms: {v}" for k, v in tail_counts(samples).items()) + f"  (of {len(samples)})")
        print("  slowest: " + ", ".join(f"#{i} = {v / 1000:.2f} ms" for i, v in worst(samples)))
    for w in result["warnings"][:5]:
        print("  engine warning:", w)
    ok = result["missed"] == 0
    for _name, met, sentence in verdict(result["summaries"], result["targets_us"]):
        print(("  met    " if met else "  NOT    ") + sentence)
        ok = ok and met
    return ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Measure the real engine's BNC timing (BNC OUT cabled to BNC IN 0).")
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--width-ms", type=float, default=10.0)
    p.add_argument("--gap-ms", type=float, default=50.0)
    p.add_argument("--can", default="vcan0", help="CAN interface to use. Always a virtual one; never can0 with nodes connected")
    p.add_argument("--realtime", type=int, default=0, metavar="PRIO", help="start the engine with --realtime PRIO")
    p.add_argument("--grant-rt", action="store_true", help="give the engine the real-time limit it needs (uses sudo -n prlimit)")
    p.add_argument("--gc", choices=("default", "freeze"), default="default", help="start the engine with --gc")
    p.add_argument("--load-procs", type=int, default=0, metavar="N", help="N busy processes on the machine during the run")
    p.add_argument("--can-traffic-fps", type=int, default=0, metavar="N",
                   help="N heartbeat-like CAN frames a second on the same bus during the run: keeps the engine's own threads busy")
    p.add_argument("--switch-interval-us", type=float, default=None, metavar="US", help="start the engine with --switch-interval-us")
    p.add_argument("--sender-rt", type=int, default=0, metavar="PRIO",
                   help="run the frame sender at this real-time priority, so busy programs can't delay it and be mistaken for engine delay")
    p.add_argument("--out", help="write the full result as JSON")
    p.add_argument("--keep", action="store_true", help="keep the engine's scratch folder")
    args = p.parse_args(argv)
    if args.can.startswith("can") and not args.can.startswith("vcan"):
        raise SystemExit("Refusing to send test frames on a real CAN interface. Use vcan0.")
    result = run(args)
    ok = report(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"wrote {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
