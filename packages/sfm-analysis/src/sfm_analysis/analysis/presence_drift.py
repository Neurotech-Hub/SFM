"""Summarize a presence-drift run from a session log and its trace sibling.

The node records the opt-in 1 Hz trace in ``{session}_presence_trace.csv``
and the bout / stuck events in the main session CSV. This module turns
those two files into the numbers the factor is tuned from:

  N  noise while the pad reads clear and the dome is closed
  S  the weakest dome bout that the touch pad also saw
  miss rate, hourly baseline range, stuck-candidate list

Both percent-of-baseline and raw counts are reported. Run it with::

    python -m sfm_analysis.analysis.presence_drift session.csv
"""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _percentile(values: List[float], fraction: float) -> Optional[float]:
    """Nearest-rank percentile. fraction 0.05 on 20 values is the minimum."""
    if not values:
        return None
    ordered = sorted(values)
    idx = math.ceil(fraction * len(ordered)) - 1
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


def _load_rows(path: Path) -> List[dict]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _fields(row: dict) -> dict:
    raw = row.get("fields_json") or ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int(row: dict, key: str, default: int = 0) -> int:
    try:
        return int(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def _ts(row: dict) -> int:
    return _int(row, "timestamp_ms")


def summarize_presence(main_csv: Path, trace_csv: Optional[Path] = None) -> Dict[str, Any]:
    """
    Build the per-node tuning summary.

    ``trace_csv`` defaults to the sibling ``*_presence_trace.csv``.
    """
    main_csv = Path(main_csv)
    if trace_csv is None:
        from ..logs import presence_trace_path_for
        trace_csv = presence_trace_path_for(main_csv)

    events = _load_rows(main_csv)
    traces = _load_rows(Path(trace_csv))
    nodes: Dict[int, Dict[str, Any]] = {}

    def node(node_id: int) -> Dict[str, Any]:
        bucket = nodes.get(node_id)
        if bucket is None:
            bucket = {
                "clear_max_counts": [],
                "clear_max_percent": [],
                "baselines": [],
                "hourly": {},
                "bouts": [],
                "stuck": [],
                "presence_edges": [],
                "pellet_taken": [],
                "other_events": [],
            }
            nodes[node_id] = bucket
        return bucket

    for row in traces:
        if row.get("frame_type") not in ("", "PRESENCE_TRACE"):
            continue
        fields = _fields(row)
        if "baseline" not in fields and "max_delta" not in fields:
            continue
        nid = _int(row, "node_id")
        if nid <= 0:
            continue
        bucket = node(nid)
        baseline = float(fields.get("baseline") or 0)
        max_delta = float(fields.get("max_delta") or 0)
        present = bool(fields.get("present"))
        dome = bool(fields.get("dome_open"))
        ts = _ts(row)
        if baseline > 0:
            bucket["baselines"].append(baseline)
        hour = ts // 3_600_000
        span = bucket["hourly"].setdefault(hour, [baseline, baseline])
        if baseline > 0:
            span[0] = min(span[0], baseline) if span[0] else baseline
            span[1] = max(span[1], baseline)
        if not present and not dome:
            bucket["clear_max_counts"].append(max_delta)
            if baseline > 0:
                bucket["clear_max_percent"].append(max_delta / baseline * 100.0)

    for row in events:
        nid = _int(row, "node_id")
        if nid <= 0:
            continue
        name = row.get("event_name") or ""
        fields = _fields(row)
        ts = _ts(row)
        bucket = node(nid)
        if name == "DomeBoutSummary":
            bucket["bouts"].append({
                "ts": ts,
                "touch_seen": bool(fields.get("touch_seen")),
                "peak_pct": float(fields.get("peak_pct_x100") or 0) / 100.0,
                "prelift_pct": float(fields.get("prelift_pct_x100") or 0) / 100.0,
                "duration_s": float(fields.get("duration_100ms") or 0) / 10.0,
            })
        elif name == "PresenceStuckCandidate":
            bucket["stuck"].append({
                "ts": ts,
                "delta_pct": float(fields.get("delta_pct_x100") or 0) / 100.0,
                "range_counts": float(fields.get("range_counts") or 0),
            })
        elif name in ("MousePresence Detected", "MousePresence Cleared"):
            bucket["presence_edges"].append((ts, name.endswith("Detected")))
        elif name == "Pellet Taken":
            bucket["pellet_taken"].append(ts)
        elif name:
            bucket["other_events"].append((ts, name))

    report_nodes = {}
    for nid, bucket in sorted(nodes.items()):
        report_nodes[nid] = _summarize_node(bucket)
    return {"nodes": report_nodes, "trace_csv": str(trace_csv), "main_csv": str(main_csv)}


def _presence_at(edges: List[tuple], ts: int) -> bool:
    state = False
    for edge_ts, present in edges:
        if edge_ts > ts:
            break
        state = present
    return state


def _longest_bout_s(edges: List[tuple], end_ts: Optional[int]) -> Optional[float]:
    start = None
    longest = 0
    saw = False
    for ts, present in edges:
        if present and start is None:
            start = ts
        elif not present and start is not None:
            longest = max(longest, ts - start)
            saw = True
            start = None
    if start is not None and end_ts is not None and end_ts >= start:
        longest = max(longest, end_ts - start)
        saw = True
    if not saw:
        return None
    return longest / 1000.0


def _summarize_node(bucket: Dict[str, Any]) -> Dict[str, Any]:
    baselines = bucket["baselines"]
    median_base = _percentile(baselines, 0.5)
    n_counts = _percentile(bucket["clear_max_counts"], 0.999)
    n_percent = _percentile(bucket["clear_max_percent"], 0.999)
    touch_peaks = [b["peak_pct"] for b in bucket["bouts"] if b["touch_seen"]]
    s_percent = _percentile(touch_peaks, 0.05)
    s_counts = None
    if s_percent is not None and median_base:
        s_counts = s_percent / 100.0 * median_base
    n_counts_from_pct = None
    if n_percent is not None and median_base:
        n_counts_from_pct = n_percent / 100.0 * median_base

    bouts = bucket["bouts"]
    misses = sum(1 for b in bouts if not b["touch_seen"])
    miss_rate = (misses / len(bouts)) if bouts else None

    edges = sorted(bucket["presence_edges"])
    taken = bucket["pellet_taken"]
    taken_while_present = sum(1 for ts in taken if _presence_at(edges, ts))
    end_ts = None
    if baselines or edges or bouts:
        candidates = [e[0] for e in edges]
        candidates += [b["ts"] for b in bouts]
        if candidates:
            end_ts = max(candidates)

    hourly = []
    for hour, (lo, hi) in sorted(bucket["hourly"].items()):
        if not lo and not hi:
            continue
        hourly.append({"hour_index": hour, "min": lo, "max": hi, "span": hi - lo})

    nearby = []
    for stuck in bucket["stuck"]:
        names = [
            name for ts, name in bucket["other_events"]
            if abs(ts - stuck["ts"]) <= 60_000
        ]
        nearby.append({**stuck, "nearby_events": names})

    suggestion = None
    hardware_first = False
    if n_percent is not None and s_percent is not None:
        suggestion = n_percent + 0.4 * (s_percent - n_percent)
        if s_percent < 3.0 * n_percent:
            hardware_first = True

    return {
        "samples_clear": len(bucket["clear_max_counts"]),
        "median_baseline": median_base,
        "N_counts": n_counts,
        "N_percent": n_percent,
        "N_counts_from_percent": n_counts_from_pct,
        "S_percent": s_percent,
        "S_counts": s_counts,
        "touch_seen_bouts": len(touch_peaks),
        "dome_bouts": len(bouts),
        "miss_rate": miss_rate,
        "longest_presence_s": _longest_bout_s(edges, end_ts),
        "pellet_taken": len(taken),
        "pellet_taken_while_present": taken_while_present,
        "hourly_baseline": hourly,
        "stuck_candidates": nearby,
        "suggested_factor_percent": suggestion,
        "hardware_first": hardware_first,
    }


def format_report(summary: Dict[str, Any]) -> str:
    """Plain-text report for the operator."""
    lines = [
        f"Presence drift summary",
        f"  session: {summary.get('main_csv')}",
        f"  trace:   {summary.get('trace_csv')}",
    ]
    nodes = summary.get("nodes") or {}
    if not nodes:
        lines.append("  no presence trace or bout rows found")
        return "\n".join(lines)
    for nid, row in nodes.items():
        lines.append(f"node {nid}")
        lines.append(
            f"  baseline median: {_fmt(row['median_baseline'])} counts"
            f"   clear seconds: {row['samples_clear']}"
        )
        lines.append(
            f"  N noise: {_fmt(row['N_percent'])}%   {_fmt(row['N_counts'])} counts"
        )
        lines.append(
            f"  S weakest touch-seen bout: {_fmt(row['S_percent'])}%"
            f"   {_fmt(row['S_counts'])} counts"
            f"   (n={row['touch_seen_bouts']})"
        )
        miss = row["miss_rate"]
        lines.append(
            f"  dome bouts: {row['dome_bouts']}"
            f"   miss rate: {_fmt(None if miss is None else miss * 100)}%"
        )
        lines.append(f"  longest presence: {_fmt(row['longest_presence_s'])} s")
        taken = row["pellet_taken"]
        lines.append(
            f"  Pellet Taken while present: {row['pellet_taken_while_present']}/{taken}"
        )
        if row["suggested_factor_percent"] is not None:
            lines.append(
                f"  suggested factor: {_fmt(row['suggested_factor_percent'])}%"
            )
        if row["hardware_first"]:
            lines.append("  S is under 3x N — fix the pad hardware before trusting the factor")
        if row["hourly_baseline"]:
            span = max(h["span"] for h in row["hourly_baseline"])
            lines.append(f"  widest hourly baseline span: {_fmt(span)} counts")
        for stuck in row["stuck_candidates"]:
            near = ", ".join(stuck["nearby_events"]) or "none"
            lines.append(
                f"  stuck candidate delta={_fmt(stuck['delta_pct'])}%"
                f" range={_fmt(stuck['range_counts'])} nearby: {near}"
            )
    return "\n".join(lines)


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if abs(value) >= 100:
        return f"{value:.0f}"
    return f"{value:.2f}"


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m sfm_analysis.analysis.presence_drift SESSION.csv", file=sys.stderr)
        return 2
    summary = summarize_presence(Path(args[0]))
    print(format_report(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
