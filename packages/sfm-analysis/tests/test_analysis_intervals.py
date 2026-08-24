"""Tests for sfm_analysis.analysis.intervals."""

from dataclasses import dataclass
from typing import Optional

import pytest

from sfm_analysis.analysis import intervals as iv


@dataclass
class _Bout:
    t0: float
    t1: Optional[float]


class TestAsIntervals:
    def test_converts_closed_bouts(self):
        bouts = [_Bout(0, 5), _Bout(10, 12)]
        assert iv.as_intervals(bouts) == [(0.0, 5.0), (10.0, 12.0)]

    def test_censored_without_run_end_raises(self):
        with pytest.raises(ValueError, match="censored"):
            iv.as_intervals([_Bout(0, None)])

    def test_censored_with_run_end_resolves_to_it(self):
        assert iv.as_intervals([_Bout(5, None)], run_end=20.0) == [(5.0, 20.0)]


class TestMerge:
    def test_empty_input(self):
        assert iv.merge([]) == []

    def test_non_overlapping_stay_separate(self):
        assert iv.merge([(0, 5), (10, 15)]) == [(0, 5), (10, 15)]

    def test_overlapping_intervals_combine(self):
        assert iv.merge([(0, 5), (3, 8)]) == [(0, 8)]

    def test_adjacent_intervals_combine(self):
        assert iv.merge([(0, 5), (5, 8)]) == [(0, 8)]

    def test_gap_parameter_bridges_near_intervals(self):
        assert iv.merge([(0, 5), (6, 8)], gap=2.0) == [(0, 8)]
        assert iv.merge([(0, 5), (6, 8)], gap=0.5) == [(0, 5), (6, 8)]

    def test_sorts_out_of_order_input(self):
        assert iv.merge([(10, 15), (0, 5)]) == [(0, 5), (10, 15)]

    def test_one_interval_swallows_another(self):
        assert iv.merge([(0, 20), (5, 8)]) == [(0, 20)]


class TestOverlap:
    def test_no_overlap_is_empty(self):
        assert iv.overlap([(0, 5)], [(10, 15)]) == []

    def test_partial_overlap(self):
        assert iv.overlap([(0, 10)], [(5, 15)]) == [(5.0, 10.0)]

    def test_containment(self):
        assert iv.overlap([(0, 20)], [(5, 8)]) == [(5.0, 8.0)]

    def test_touching_but_not_overlapping_is_empty(self):
        # Half-open semantics: [0,5) and [5,10) share only the single
        # point t=5, which has zero duration and is not "overlap".
        assert iv.overlap([(0, 5)], [(5, 10)]) == []

    def test_multiple_overlaps_all_found(self):
        a = [(0, 10), (20, 30)]
        b = [(5, 8), (25, 35)]
        assert iv.overlap(a, b) == [(5.0, 8.0), (25.0, 30.0)]


class TestSubtract:
    def test_no_overlap_returns_a_unchanged(self):
        assert iv.subtract([(0, 10)], [(20, 30)]) == [(0, 10)]

    def test_full_cover_removes_everything(self):
        assert iv.subtract([(0, 10)], [(0, 10)]) == []
        assert iv.subtract([(0, 10)], [(-5, 15)]) == []

    def test_bite_out_of_the_middle(self):
        assert iv.subtract([(0, 10)], [(3, 6)]) == [(0.0, 3.0), (6.0, 10.0)]

    def test_bite_out_of_the_start(self):
        assert iv.subtract([(0, 10)], [(-5, 3)]) == [(3.0, 10.0)]

    def test_bite_out_of_the_end(self):
        assert iv.subtract([(0, 10)], [(7, 15)]) == [(0.0, 7.0)]

    def test_multiple_b_intervals_bite_multiple_holes(self):
        assert iv.subtract([(0, 10)], [(2, 3), (6, 7)]) == [
            (0.0, 2.0), (3.0, 6.0), (7.0, 10.0),
        ]


class TestAround:
    def test_symmetric_window(self):
        assert iv.around([10.0], (-2, 2)) == [(8.0, 12.0)]

    def test_asymmetric_window_matches_readme_example(self):
        # "takes within 30s after a fault" -> window (0, 30)
        assert iv.around([100.0], (0, 30)) == [(100.0, 130.0)]

    def test_multiple_events(self):
        assert iv.around([0.0, 10.0], (0, 5)) == [(0.0, 5.0), (10.0, 15.0)]


class TestCountIn:
    def test_counts_events_inside_intervals(self):
        assert iv.count_in([1, 5, 12], [(0, 10)]) == 2

    def test_boundary_is_inclusive(self):
        assert iv.count_in([5.0], [(0, 5)]) == 1

    def test_no_events_in_empty_intervals(self):
        assert iv.count_in([1, 2, 3], []) == 0

    def test_overlapping_intervals_do_not_double_count(self):
        assert iv.count_in([5.0], [(0, 10), (3, 8)]) == 1


class TestRateIn:
    def test_basic_rate(self):
        assert iv.rate_in([1, 2, 3, 4], [(0, 10)]) == pytest.approx(0.4)

    def test_zero_duration_returns_none(self):
        assert iv.rate_in([1, 2], []) is None

    def test_merges_overlapping_intervals_before_dividing(self):
        # merge([(0,10),(5,15)]) -> [(0,15)], total duration 15, not the
        # double-counted 10+10=20 an un-merged sum would give.
        assert iv.rate_in([5.0], [(0, 10), (5, 15)]) == pytest.approx(1 / 15)
