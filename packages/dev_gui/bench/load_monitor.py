"""
load_monitor.py — measure CPU and memory of the Rig Map processes over time.

    python -m bench.load_monitor --group engine=unit:sfm-engine --group gateway=unit:sfm-gateway \
        --group browser=match:--user-data-dir=/home/pi/.config/sfm-kiosk --interval 5 --duration 600 --out load.csv

A group is ``name=unit:<systemd user unit>``, ``name=match:<text in the command line>`` or
``name=pid:<pid>``. Each sample records, per group, CPU as a share of the WHOLE machine
(100% = every core busy), the memory the group really uses (PSS, which counts shared pages
once) and its process count. A summary is printed at the end: mean and worst CPU, and memory
at the start and end with its trend in MB per hour. Leave it running for a day to look for leaks.

Reads /proc only. Linux.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

CLK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1


def _pids() -> Iterable[int]:
    return (int(n) for n in os.listdir("/proc") if n.isdigit())


def pids_for_unit(unit: str) -> List[int]:
    """Every process in a systemd user unit's control group."""
    out = subprocess.run(["systemctl", "--user", "show", "-p", "ControlGroup", "--value", unit],
                         capture_output=True, text=True).stdout.strip()
    if not out:
        return []
    try:
        return [int(x) for x in Path("/sys/fs/cgroup" + out, "cgroup.procs").read_text().split()]
    except OSError:
        return []


def pids_matching(text: str) -> List[int]:
    me = os.getpid()
    found = []
    for pid in _pids():
        if pid == me:
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if text in cmd and "load_monitor" not in cmd:
            found.append(pid)
    return found


def resolve(spec: str) -> List[int]:
    kind, _, value = spec.partition(":")
    if kind == "unit":
        return pids_for_unit(value)
    if kind == "match":
        return pids_matching(value)
    if kind == "pid":
        return [int(value)] if Path(f"/proc/{value}").exists() else []
    raise ValueError(f"unknown group kind: {kind}")


def cpu_ticks(pid: int) -> Optional[int]:
    """utime + stime in clock ticks, or None if the process is gone."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, ValueError, IndexError):
        return None


def pss_kb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1024
    except (OSError, ValueError, IndexError):
        return 0.0


def machine_busy_ticks() -> Tuple[int, int]:
    """(busy, total) clock ticks across every core."""
    vals = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals) - idle, sum(vals)


def slope_per_hour(points: Sequence[Tuple[float, float]]) -> float:
    """Least-squares slope of (seconds, value) points, in value units per hour."""
    n = len(points)
    if n < 2:
        return 0.0
    mx = sum(t for t, _ in points) / n
    my = sum(v for _, v in points) / n
    den = sum((t - mx) ** 2 for t, _ in points)
    return 0.0 if den == 0 else sum((t - mx) * (v - my) for t, v in points) / den * 3600.0


class Monitor:
    def __init__(self, groups: Dict[str, str]) -> None:
        self.groups = groups
        self.prev: Dict[str, Dict[int, int]] = {g: {} for g in groups}
        self.prev_time = time.monotonic()
        self.prev_busy = machine_busy_ticks()

    def sample(self) -> List[dict]:
        now = time.monotonic()
        dt = max(1e-6, now - self.prev_time)
        rows = []
        for name, spec in self.groups.items():
            ticks_now: Dict[int, int] = {}
            mem = 0.0
            for pid in resolve(spec):
                t = cpu_ticks(pid)
                if t is None:
                    continue
                ticks_now[pid] = t
                mem += pss_kb(pid)
            before = self.prev[name]
            # Only processes seen in both samples count, so one that just appeared does not add its whole life.
            used = sum(max(0, t - before[p]) for p, t in ticks_now.items() if p in before)
            self.prev[name] = ticks_now
            rows.append({"group": name, "cpu_pct_machine": 100.0 * used / CLK / dt / NCPU,
                         "mem_mb": mem / 1024, "procs": len(ticks_now)})
        busy, total = machine_busy_ticks()
        db, dtot = busy - self.prev_busy[0], total - self.prev_busy[1]
        rows.append({"group": "machine", "cpu_pct_machine": 100.0 * db / dtot if dtot else 0.0, "mem_mb": 0.0, "procs": 0})
        self.prev_busy, self.prev_time = (busy, total), now
        return rows


def summarize(samples: List[dict]) -> List[str]:
    lines = []
    for g in dict.fromkeys(s["group"] for s in samples):
        rows = [s for s in samples if s["group"] == g]
        cpu = [r["cpu_pct_machine"] for r in rows]
        line = f"{g:10} CPU of whole machine: mean {sum(cpu)/len(cpu):5.2f}%  worst {max(cpu):5.2f}%"
        if g != "machine":
            mem = [(r["t"], r["mem_mb"]) for r in rows]
            line += f"   memory: first {mem[0][1]:7.1f} MB  last {mem[-1][1]:7.1f} MB  peak {max(m for _, m in mem):7.1f} MB  trend {slope_per_hour(mem):+7.1f} MB/h"
        lines.append(line)
    return lines


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="load_monitor")
    p.add_argument("--group", action="append", required=True, metavar="NAME=SPEC")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--duration", type=float, default=60.0, help="seconds; 0 = until interrupted")
    p.add_argument("--out", help="write every sample to this CSV")
    a = p.parse_args(argv)
    groups = dict(g.split("=", 1) for g in a.group)
    mon = Monitor(groups)
    samples: List[dict] = []
    out = open(a.out, "w", newline="") if a.out else None
    writer = csv.DictWriter(out, ["t", "group", "cpu_pct_machine", "mem_mb", "procs"]) if out else None
    if writer:
        writer.writeheader()
    start = time.monotonic()
    time.sleep(a.interval)
    try:
        while True:
            t = time.monotonic() - start
            for row in mon.sample():
                row["t"] = round(t, 1)
                samples.append(row)
                if writer:
                    writer.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()})
            if out:
                out.flush()
            if a.duration and t >= a.duration:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()
    print("\n".join(summarize(samples)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
