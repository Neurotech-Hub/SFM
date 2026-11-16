"""
The analysis half of the BNC loopback bench tool. The measuring half needs the
Pi; the arithmetic that turns edge times into numbers is checked here.
"""

from __future__ import annotations

import pytest

from bench.bnc_loopback import (
    Edge,
    Pulse,
    pair_events,
    pair_pulses,
    summarize,
    verdict,
)


def _edge(rising, t, wall_offset=1_000_000.0):
    return Edge(rising, t, wall_offset + t)


def test_summary_percentiles_use_nearest_rank() -> None:
    s = summarize(range(1, 101))            # 1..100
    assert (s["n"], s["min"], s["p50"], s["p95"], s["p99"], s["max"]) == (100, 1, 50, 95, 99, 100)
    assert s["mean"] == pytest.approx(50.5)


def test_summary_of_nothing_and_of_one() -> None:
    assert summarize([]) == {"n": 0}
    one = summarize([7.0])
    assert one["p50"] == one["p99"] == one["max"] == 7.0 and one["stdev"] == 0.0


def test_a_clean_pulse_is_measured_from_its_edges() -> None:
    pulse = Pulse(requested_us=10_000, t_request=10.000, t_rise=10.00010, t_fall=10.01012)
    edges = [_edge(True, 10.00030), _edge(False, 10.01040)]
    measured, missed = pair_pulses([pulse], edges)

    assert missed == 0
    m = measured[0]
    assert m["request_to_edge_us"] == pytest.approx(300.0, abs=0.01)        # asked -> line rose
    assert m["software_rise_to_edge_us"] == pytest.approx(200.0, abs=0.01)  # thread stamp -> line rose
    assert m["observed_width_us"] == pytest.approx(10_100.0, abs=0.01)      # what the wire carried
    assert m["width_error_us"] == pytest.approx(100.0, abs=0.01)
    assert m["software_width_error_us"] == pytest.approx(20.0, abs=0.01)    # what Python believed


def test_each_pulse_gets_its_own_edges() -> None:
    pulses = [Pulse(5_000, 1.0, 1.0001, 1.0051), Pulse(5_000, 2.0, 2.0001, 2.0051)]
    edges = [_edge(True, 1.0002), _edge(False, 1.0052), _edge(True, 2.0003), _edge(False, 2.0053)]
    measured, missed = pair_pulses(pulses, edges)
    assert missed == 0 and len(measured) == 2
    assert [round(m["request_to_edge_us"]) for m in measured] == [200, 300]


def test_a_pulse_with_no_edge_is_counted_as_missed_not_guessed() -> None:
    pulses = [Pulse(5_000, 1.0, 1.0001, 1.0051), Pulse(5_000, 2.0, 2.0001, 2.0051)]
    edges = [_edge(True, 2.0003), _edge(False, 2.0053)]      # the first pulse never reached the input
    measured, missed = pair_pulses(pulses, edges)
    assert (len(measured), missed) == (1, 1)


def test_a_rising_edge_with_no_falling_edge_is_missed() -> None:
    measured, missed = pair_pulses([Pulse(5_000, 1.0, 1.0001, 1.0051)], [_edge(True, 1.0002)])
    assert (measured, missed) == ([], 1)


def test_an_edge_from_long_before_is_not_matched_to_a_later_pulse() -> None:
    measured, missed = pair_pulses(
        [Pulse(5_000, 5.0, 5.0001, 5.0051)],
        [_edge(True, 1.0), _edge(False, 1.005)],
    )
    assert (measured, missed) == ([], 1)


def test_event_delays_are_edge_minus_frame_arrival() -> None:
    arrivals = [2_000_000.0, 2_000_001.0]
    edges = [Edge(True, 0.0, 2_000_000.00042), Edge(False, 0.0, 2_000_000.0101),
             Edge(True, 0.0, 2_000_001.00031)]
    delays, missed = pair_events(arrivals, edges)
    assert missed == 0
    assert delays == pytest.approx([420.0, 310.0], abs=0.01)


def test_a_frame_with_no_pulse_is_counted_as_missed() -> None:
    delays, missed = pair_events([100.0, 200.0], [Edge(True, 0.0, 200.0004)])
    assert (len(delays), missed) == (1, 1)


def test_the_verdict_judges_the_p99_against_the_target() -> None:
    good = summarize([300.0] * 99 + [900.0])
    bad = summarize([300.0] * 90 + [4000.0] * 10)
    (_, met_good, _), = verdict({"request_to_edge_us": good}, {"request_to_edge_us": 1000.0})
    (_, met_bad, sentence), = verdict({"request_to_edge_us": bad}, {"request_to_edge_us": 1000.0})
    assert met_good is True
    assert met_bad is False and "NOT met" in sentence


def test_the_verdict_treats_an_error_in_either_direction_as_an_error() -> None:
    early = summarize([-5000.0] * 100)         # e.g. a width 5 ms too short
    (_, met, _), = verdict({"width_error_us": early}, {"width_error_us": 200.0})
    assert met is False


def test_the_verdict_fails_when_there_is_nothing_to_judge() -> None:
    (_, met, sentence), = verdict({"x": {"n": 0}}, {"x": 1000.0})
    assert met is False and "no measurements" in sentence


def test_tail_counts_count_samples_above_each_threshold() -> None:
    from bench.bnc_loopback import tail_counts
    t = tail_counts([100, 300, 600, 1500, 6000], thresholds=(250, 500, 1000, 5000))
    assert t == {"over_250": 4, "over_500": 3, "over_1000": 2, "over_5000": 1}
    assert tail_counts([], thresholds=(250,)) == {"over_250": 0}
    assert tail_counts([250], thresholds=(250,)) == {"over_250": 0}, "above, not at"


def test_worst_reports_where_in_the_run_the_slowest_samples_were() -> None:
    from bench.bnc_loopback import worst
    assert worst([5, 90, 7, 40, 90.5], k=3) == [(4, 90.5), (1, 90), (3, 40)]
    assert worst([], k=3) == []


def test_scheduling_switches_apply_and_are_reported() -> None:
    import subprocess, sys, json
    code = ("import json; from bench.bnc_loopback import apply_scheduling;"
            "print(json.dumps(apply_scheduling(500, 0, None)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    got = json.loads(out)
    assert got["switch_interval_us"] == 500 and got["cpu_affinity"] == [0] and got["scheduler"] == "SCHED_OTHER"


def test_real_time_priority_without_privileges_is_a_plain_message_not_a_traceback() -> None:
    import os, subprocess, sys
    if os.geteuid() == 0:
        pytest.skip("running as root")
    code = "from bench.bnc_loopback import apply_scheduling; apply_scheduling(None, None, 50)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 1 and "privileges" in r.stderr and "Traceback" not in r.stderr.split("privileges")[0].splitlines()[-1]


def test_load_processes_do_not_inherit_the_pinning_applied_afterwards() -> None:
    """A busy child pinned (or made real-time) with the benchmark would starve it rather than imitate other programs."""
    import json, subprocess, sys
    code = """
import json, os
from bench.bnc_loopback import start_load_processes, apply_scheduling
procs, stop = start_load_processes(2)
apply_scheduling(None, 0, None)                 # pin THIS process to core 0, after the load started
child = sorted(os.sched_getaffinity(procs[0].pid))
own = sorted(os.sched_getaffinity(0))
stop.set()
for p in procs:
    p.join(3)
print(json.dumps({"child": child, "own": own, "cores": os.cpu_count()}))
"""
    out = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout)
    assert out["own"] == [0]
    assert len(out["child"]) == out["cores"], "the load processes keep every core"


def test_the_gc_switch_changes_the_collector_and_says_what_it_did() -> None:
    import json, subprocess, sys
    code = """
import gc, json
from bench.bnc_loopback import apply_gc
out = {}
for mode in ("default", "off", "freeze"):
    gc.enable()
    out[mode] = [apply_gc(mode), gc.isenabled(), gc.get_freeze_count() > 0]
print(json.dumps(out))
"""
    got = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout)
    assert got["default"] == ["default", True, False]
    assert got["off"] == ["off", False, False]
    assert got["freeze"][0] == "freeze" and got["freeze"][1] is True and got["freeze"][2] is True


# ---- the engine-level harness: its analysis and its safety rails ----

def test_sends_are_paired_with_the_edges_they_caused() -> None:
    from bench.engine_loopback import pair_sends
    sends = [10.000, 10.100, 10.200]
    rising = [10.00040, 10.10030, 10.20050]
    falling = [10.01045, 10.11030, 10.21050]
    out, missed = pair_sends(sends, rising, falling)
    assert missed == 0 and len(out) == 3
    assert out[0]["frame_sent_to_edge_us"] == pytest.approx(400, abs=1)
    assert out[0]["observed_width_us"] == pytest.approx(10050, abs=1)


def test_a_send_with_no_pulse_is_counted_as_missed_not_guessed() -> None:
    from bench.engine_loopback import pair_sends
    out, missed = pair_sends([1.0, 2.0], [1.0003], [1.0103])
    assert len(out) == 1 and missed == 1
    out, missed = pair_sends([1.0], [1.0003], [])
    assert out == [] and missed == 1, "a rising edge with no falling edge is not a pulse"


def test_one_edge_is_never_used_for_two_sends() -> None:
    from bench.engine_loopback import pair_sends
    out, missed = pair_sends([1.000, 1.001], [1.0005], [1.0105])
    assert len(out) == 1 and missed == 1


def test_the_edges_are_read_from_the_engines_csv(tmp_path) -> None:
    import csv, json
    from bench.engine_loopback import read_bnc_rows
    from sfm_analysis.logs import CSV_HEADER
    path = tmp_path / "log.csv"
    rows = [
        ("BNC", "IN1 rising", {"t_mono": 5.000123}), ("BNC", "IN1 falling", {"t_mono": 5.010234}),
        ("BNC", "IN2 rising", {"t_mono": 9.0}), ("BNC", "BNC OUT pulse", {"latency_us": 70.5, "width_us": 10020.0}),
        ("EVENT", "Dome Opened", {}), ("WARNING", "BNC Edges Dropped", {}),
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for ftype, name, fields in rows:
            row = [""] * len(CSV_HEADER)
            row[CSV_HEADER.index("frame_type")] = ftype
            row[CSV_HEADER.index("event_name")] = name
            row[CSV_HEADER.index("fields_json")] = json.dumps(fields)
            row[CSV_HEADER.index("details")] = "d"
            w.writerow(row)
    got = read_bnc_rows(path)
    assert got["rising"] == [5.000123] and got["falling"] == [5.010234], "only the loopback channel"
    assert got["pulses"] == [{"latency_us": 70.5, "width_us": 10020.0}]
    assert got["warnings"] and "BNC Edges Dropped" in got["warnings"][0]


def test_the_harness_refuses_a_real_can_interface() -> None:
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "bench.engine_loopback", "--can", "can0", "--count", "1"], capture_output=True, text=True)
    assert r.returncode != 0 and "Refusing" in r.stderr


def test_the_engine_command_carries_the_options_and_a_scratch_home() -> None:
    from bench.engine_loopback import engine_command
    plain = engine_command("/tmp/x/e.sock", "/tmp/h", 0, "default", False)
    assert plain[0] == "env" and "HOME=/tmp/h" in plain and "--realtime" not in plain
    rt = engine_command("/tmp/x/e.sock", "/tmp/h", 50, "freeze", True)
    assert rt[:4] == ["sudo", "-n", "prlimit", "--rtprio=99:99"] and "--realtime" in rt and "50" in rt and "freeze" in rt


def test_pair_csv_rows_reports_the_logged_time_against_the_kernel_edge() -> None:
    from bench.engine_loopback import pair_csv_rows
    out_rows = [{"ms": 1000, "t_mono_write_start": 50.000000, "t_mono_rise": 50.000040, "rise_uncertainty_us": 20.0}]
    in_rows = [{"ms": 1000, "t_mono": 50.000030}]
    paired, missed = pair_csv_rows(out_rows, in_rows)
    assert missed == 0
    assert paired[0]["logged_minus_edge_us"] == pytest.approx(-10.0, abs=0.01)   # middle of write is 10 us before the edge
    assert paired[0]["edge_outside_us"] == 0.0
    assert paired[0]["csv_ms_diff_us"] == 0.0


def test_pair_csv_rows_counts_pulses_with_no_edge_and_flags_an_edge_outside_the_write() -> None:
    from bench.engine_loopback import pair_csv_rows
    out_rows = [
        {"ms": 1, "t_mono_write_start": 10.0, "t_mono_rise": 10.00004, "rise_uncertainty_us": 20.0},
        {"ms": 2, "t_mono_write_start": 20.0, "t_mono_rise": 20.00004, "rise_uncertainty_us": 20.0},
    ]
    in_rows = [{"ms": 2, "t_mono": 20.0001}]        # 60 us after the write returned
    paired, missed = pair_csv_rows(out_rows, in_rows)
    assert missed == 1 and len(paired) == 1
    assert paired[0]["edge_outside_us"] == pytest.approx(60.0, abs=0.01)


def test_pair_csv_rows_uses_the_nearest_edge_when_pulses_are_close_together() -> None:
    from bench.engine_loopback import pair_csv_rows
    out_rows = [{"ms": 2, "t_mono_write_start": 20.0, "t_mono_rise": 20.00004, "rise_uncertainty_us": 20.0}]
    in_rows = [{"ms": 1, "t_mono": 19.95}, {"ms": 2, "t_mono": 20.00003}, {"ms": 3, "t_mono": 20.05}]
    paired, _ = pair_csv_rows(out_rows, in_rows)
    assert paired[0]["edge_outside_us"] == 0.0 and abs(paired[0]["logged_minus_edge_us"]) < 50
