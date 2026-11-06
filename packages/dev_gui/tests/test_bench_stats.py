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
