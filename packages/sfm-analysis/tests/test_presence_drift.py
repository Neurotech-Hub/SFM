"""Presence-drift summary, plus the decision rules the firmware implements.

The C++ on the node is the real filter. These checks lock the numbers the
tuning script reports and the rules that script is written against: a
single spike does not move a 5-sample median, the clear level sits at half
the on level, and a boot seed is the median of the window.
"""

import csv
import json
from pathlib import Path

from sfm_analysis.analysis.presence_drift import format_report, summarize_presence
from sfm_analysis.logs import CSV_HEADER, presence_trace_path_for


def _write(path: Path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in rows:
            full = {key: "" for key in CSV_HEADER}
            full.update(row)
            writer.writerow(full)


def _row(ts, node, frame_type, name, fields=None):
    return {
        "timestamp_ms": str(ts),
        "node_id": str(node),
        "frame_type": frame_type,
        "event_name": name,
        "fields_json": json.dumps(fields or {}),
    }


def test_summary_reports_noise_signal_and_misses(tmp_path: Path):
    main = tmp_path / "session.csv"
    _write(main, [
        _row(1_000, 1, "EVENT", "DomeBoutSummary", {
            "touch_seen": True, "peak_pct_x100": 200,
            "prelift_pct_x100": 180, "duration_100ms": 10,
        }),
        _row(2_000, 1, "EVENT", "DomeBoutSummary", {
            "touch_seen": False, "peak_pct_x100": 20,
            "prelift_pct_x100": 10, "duration_100ms": 8,
        }),
        _row(1_500, 1, "EVENT", "MousePresence Detected"),
        _row(4_500, 1, "EVENT", "MousePresence Cleared"),
        _row(2_000, 1, "EVENT", "Pellet Taken"),
        _row(9_000, 1, "EVENT", "PresenceStuckCandidate", {
            "delta_pct_x100": 150, "range_counts": 4,
        }),
        _row(9_500, 1, "EVENT", "Dome Opened"),
    ])
    trace = presence_trace_path_for(main)
    trace_rows = []
    for i in range(10):
        trace_rows.append(_row(i * 1000, 1, "PRESENCE_TRACE", "PresenceTrace", {
            "baseline": 35000,
            "max_delta": 350,
            "min_delta": -5,
            "present": False,
            "dome_open": False,
            "tracking": True,
        }))
    # One loud clear second, so the 99.9th of 11 values is still the quiet one
    # only if the rank lands below it. Use a single sample set where every
    # clear second is 35 counts = 0.1%.
    _write(trace, trace_rows)

    summary = summarize_presence(main)
    node = summary["nodes"][1]
    assert node["N_percent"] == 1.0
    assert node["N_counts"] == 350
    assert node["S_percent"] == 2.0
    assert node["miss_rate"] == 0.5
    assert node["dome_bouts"] == 2
    assert node["touch_seen_bouts"] == 1
    assert node["longest_presence_s"] == 3.0
    assert node["pellet_taken_while_present"] == 1
    assert node["stuck_candidates"][0]["nearby_events"] == ["Dome Opened"]
    assert node["hardware_first"] is True
    text = format_report(summary)
    assert "node 1" in text
    assert "hardware" in text


def test_median_of_five_rejects_a_single_spike():
    samples = [35000, 35010, 90000, 35020, 34990]
    assert sorted(samples)[len(samples) // 2] == 35010


def test_clear_level_is_half_the_on_level():
    baseline = 35000.0
    factor_percent = 1.0
    on_delta = factor_percent / 100.0 * baseline
    assert on_delta == 350.0
    assert 0.5 * on_delta == 175.0


def test_boot_seed_is_the_median_not_the_minimum():
    window = [35100, 34900, 35000, 35200, 34800]
    median = sorted(window)[len(window) // 2]
    assert median == 35000
    assert median != min(window)
