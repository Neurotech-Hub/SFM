"""Tests for sfm_analysis.report.metrics."""

from report_fixtures import (
    can_event_row, exp_row, heartbeat_payload, heartbeat_row,
    input_changed_row, write_session,
)

from sfm_analysis.protocol import CanEvent, ServiceStatus
from sfm_analysis.logs import heartbeat_path_for
from sfm_analysis.report.loader import load_heartbeats, load_rows
from sfm_analysis.report.metrics import (
    activity_by_day, build_cycles, bouts, dome_bouts, fault_intervals,
    interaction_funnel, pellet_accounting, presence_bouts,
)
from sfm_analysis.report.session import split_runs
from sfm_analysis.report.stats import chi2_sf, wilson_ci


def _run(tmp_path, rows, heartbeat_rows=None, session="S"):
    path = write_session(tmp_path, rows, session=session, heartbeat_rows=heartbeat_rows)
    loaded, _, _ = load_rows(path)
    hb = load_heartbeats(heartbeat_path_for(path)) if heartbeat_rows else []
    return split_runs(loaded, hb, path)[0]


class TestBouts:
    def test_simple_pairs(self, tmp_path):
        rows = [
            input_changed_row(0, 1, 4, True, "MousePresence Detected"),
            input_changed_row(5, 1, 4, False, "MousePresence Cleared"),
            input_changed_row(10, 1, 4, True, "MousePresence Detected"),
            input_changed_row(20, 1, 4, False, "MousePresence Cleared"),
            input_changed_row(30, 1, 4, True, "MousePresence Detected"),
            input_changed_row(40, 1, 4, False, "MousePresence Cleared"),
        ]
        run = _run(tmp_path, rows)
        b, issues = presence_bouts(run)
        assert len(b) == 3
        assert issues.orphan_closes == 0
        assert issues.duplicate_opens == 0
        assert issues.censored == 0

    def test_duplicate_open_counted_and_first_kept(self, tmp_path):
        rows = [
            input_changed_row(0, 1, 4, True, "MousePresence Detected"),
            input_changed_row(5, 1, 4, True, "MousePresence Detected"),   # duplicate open
            input_changed_row(10, 1, 4, False, "MousePresence Cleared"),
        ]
        run = _run(tmp_path, rows)
        b, issues = presence_bouts(run)
        assert len(b) == 1
        assert b[0].t0 == 0.0   # first open kept
        assert issues.duplicate_opens == 1

    def test_orphan_close_counted(self, tmp_path):
        rows = [input_changed_row(0, 1, 4, False, "MousePresence Cleared")]
        run = _run(tmp_path, rows)
        b, issues = presence_bouts(run)
        assert len(b) == 0
        assert issues.orphan_closes == 1

    def test_trailing_open_is_censored(self, tmp_path):
        rows = [input_changed_row(0, 1, 4, True, "MousePresence Detected")]
        run = _run(tmp_path, rows)
        b, issues = presence_bouts(run)
        assert len(b) == 1
        assert b[0].censored is True
        assert issues.censored == 1


class TestDomeBouts:
    def test_milestone_and_edge_within_window_are_one_opening(self, tmp_path):
        rows = [
            can_event_row(0, 1, CanEvent.DomeOpened, bytes([0x01, 0x00, 0x01])),
            input_changed_row(100, 1, 3, True, "Dome Opened"),   # within 0.25s -> same opening
            input_changed_row(500, 1, 3, False, "Dome closed"),
        ]
        run = _run(tmp_path, rows)
        b, issues = dome_bouts(run)
        assert len(b) == 1
        assert b[0].t0 == 0.0

    def test_openings_farther_apart_are_two_bouts(self, tmp_path):
        rows = [
            can_event_row(0, 1, CanEvent.DomeOpened, bytes([0x01, 0x00, 0x01])),
            input_changed_row(500, 1, 3, False, "Dome closed"),
            can_event_row(1000, 1, CanEvent.DomeOpened, bytes([0x02, 0x00, 0x01])),
            input_changed_row(1500, 1, 3, False, "Dome closed"),
        ]
        run = _run(tmp_path, rows)
        b, issues = dome_bouts(run)
        assert len(b) == 2


class TestBuildCycles:
    def test_full_chain_yields_exact_latencies(self, tmp_path):
        # A COMMAND row is built directly (frame_type=COMMAND, source=EXP) —
        # exp_row() always writes frame_type=EXPERIMENT, which wouldn't match.
        from report_fixtures import row as row_
        rows = [
            row_(ts_ms=0, node_id=1, frame_type="COMMAND", source="EXP", direction="TX",
                 event_name="Dispense", fields={"cmd": "Dispense", "payload_hex": ""}),
            can_event_row(100, 1, CanEvent.Lowering),
            can_event_row(200, 1, CanEvent.Loading),
            can_event_row(300, 1, CanEvent.OnPlate),
            can_event_row(400, 1, CanEvent.Raising),
            can_event_row(500, 1, CanEvent.Loaded),
            input_changed_row(600, 1, 4, True, "MousePresence Detected"),
            can_event_row(700, 1, CanEvent.DomeOpened, bytes([0x01, 0x00, 0x01])),
            can_event_row(900, 1, CanEvent.PelletTaken, bytes([0x01, 0x00, 0x01])),
        ]
        run = _run(tmp_path, rows)
        cycles = build_cycles(run)
        assert len(cycles) == 1
        c = cycles[0]
        assert c.cmd_t == 0.0
        assert c.ready_t == 0.5
        assert abs(c.cycle_duration - 0.5) < 1e-9
        assert abs(c.approach_latency - 0.1) < 1e-9
        assert abs(c.dome_latency - 0.2) < 1e-9
        assert abs(c.retrieval_latency - 0.4) < 1e-9
        assert abs(c.handling_time - 0.2) < 1e-9
        assert c.censored is False

    def test_no_take_leaves_retrieval_latency_none_and_dome_without_take(self, tmp_path):
        from report_fixtures import row as row_
        rows = [
            row_(ts_ms=0, node_id=1, frame_type="COMMAND", source="EXP", direction="TX",
                 event_name="Dispense", fields={"cmd": "Dispense"}),
            can_event_row(500, 1, CanEvent.Loaded),
            can_event_row(700, 1, CanEvent.DomeOpened, bytes([0x01, 0x00, 0x01])),
        ]
        run = _run(tmp_path, rows)
        cycles = build_cycles(run)
        assert len(cycles) == 1
        c = cycles[0]
        assert c.retrieval_latency is None
        assert c.dome_without_take is True
        assert c.censored is True   # never closed by a Take or a next command

    def test_manual_dispense_with_no_command_row_still_opens_cycle(self, tmp_path):
        rows = [
            can_event_row(0, 1, CanEvent.Loaded),
            can_event_row(300, 1, CanEvent.PelletTaken, bytes([0x01, 0x00, 0x01])),
        ]
        run = _run(tmp_path, rows)
        cycles = build_cycles(run)
        assert len(cycles) == 1
        assert cycles[0].cmd_t is None
        assert cycles[0].cycle_duration is None
        assert abs(cycles[0].retrieval_latency - 0.3) < 1e-9


class TestPelletAccounting:
    def test_counts_and_take_rate(self, tmp_path):
        rows = [
            can_event_row(0, 1, CanEvent.Loaded),
            can_event_row(100, 1, CanEvent.Loaded),
            can_event_row(200, 1, CanEvent.PelletTaken, bytes([0x01, 0x00, 0x01])),
        ]
        run = _run(tmp_path, rows)
        acct = pellet_accounting(run)
        a = acct[1]
        assert a.presented == 2
        assert a.taken == 1
        assert a.take_rate == 0.5

    def test_zero_presented_gives_none_take_rate_not_error(self, tmp_path):
        run = _run(tmp_path, [can_event_row(0, 1, CanEvent.PelletTaken, bytes([0, 0, 1]))])
        acct = pellet_accounting(run)
        assert acct[1].take_rate is None

    def test_bus_loss_from_heartbeat_cross_check(self, tmp_path):
        rows = [can_event_row(0, 1, CanEvent.Loaded)]  # only 1 counted, but firmware saw 2
        hb_rows = [
            heartbeat_row(0, 1, heartbeat_payload(presented=5)),
            heartbeat_row(1000, 1, heartbeat_payload(presented=7)),
        ]
        run = _run(tmp_path, rows, heartbeat_rows=hb_rows)
        acct = pellet_accounting(run)
        assert acct[1].hb_presented_delta == 2
        assert acct[1].bus_loss_presented == 1   # firmware saw 2, we counted 1 EVENT row


class TestFaultIntervals:
    def test_pairs_fault_and_recovered_with_exact_downtime(self, tmp_path):
        rows = [
            can_event_row(0, 1, CanEvent.Fault, bytes([ServiceStatus.Jam.value])),
            exp_row(500, "recovered", {"node": 1}, node=1),
        ]
        run = _run(tmp_path, rows)
        faults = fault_intervals(run)
        assert len(faults) == 1
        assert faults[0].code == ServiceStatus.Jam
        assert abs(faults[0].dur - 0.5) < 1e-9
        assert faults[0].censored is False

    def test_unrecovered_fault_is_censored_at_run_end(self, tmp_path):
        rows = [can_event_row(0, 1, CanEvent.Fault, bytes([ServiceStatus.FeedTimeout.value]))]
        run = _run(tmp_path, rows)
        faults = fault_intervals(run)
        assert len(faults) == 1
        assert faults[0].censored is True


class TestInteractionFunnel:
    def test_conversion_counts(self, tmp_path):
        from report_fixtures import row as row_
        rows = [
            row_(ts_ms=0, node_id=1, frame_type="COMMAND", source="EXP", direction="TX",
                 event_name="Dispense", fields={"cmd": "Dispense"}),
            can_event_row(100, 1, CanEvent.Loaded),
            input_changed_row(200, 1, 4, True, "MousePresence Detected"),
            can_event_row(300, 1, CanEvent.DomeOpened, bytes([1, 0, 1])),
            can_event_row(400, 1, CanEvent.PelletTaken, bytes([1, 0, 1])),
        ]
        run = _run(tmp_path, rows)
        cycles = build_cycles(run)
        funnel = interaction_funnel(cycles)
        f = funnel[1]
        assert f.presented == 1
        assert f.approached == 1
        assert f.dome_opened == 1
        assert f.taken == 1
        assert f.approach_without_dome == 0
        assert f.dome_without_take == 0


class TestActivityByDay:
    def test_groups_events_by_calendar_day(self, tmp_path):
        day_ms = 24 * 3600 * 1000
        # 3 days apart guarantees distinct calendar dates regardless of
        # the test machine's own timezone.
        rows = [
            input_changed_row(1_700_000_000_000, 1, 4, True, "MousePresence Detected"),
            input_changed_row(1_700_000_000_000 + 3 * day_ms, 1, 4, True, "MousePresence Detected"),
        ]
        run = _run(tmp_path, rows)
        days = activity_by_day(run)
        assert len(days) == 2
        assert days[0].date < days[1].date

    def test_empty_when_no_matching_events(self, tmp_path):
        run = _run(tmp_path, [can_event_row(0, 1, CanEvent.Loaded)])
        assert activity_by_day(run) == []

    def test_only_detected_not_cleared_counts_by_default(self, tmp_path):
        rows = [
            input_changed_row(0, 1, 4, True, "MousePresence Detected"),
            input_changed_row(5000, 1, 4, False, "MousePresence Cleared"),
        ]
        run = _run(tmp_path, rows)
        days = activity_by_day(run)
        assert len(days) == 1
        assert len(days[0].times) == 1

    def test_event_names_override_selects_a_different_proxy(self, tmp_path):
        rows = [
            input_changed_row(0, 1, 4, True, "MousePresence Detected"),
            can_event_row(1000, 1, CanEvent.PelletTaken, bytes([1, 0, 1])),
        ]
        run = _run(tmp_path, rows)
        default_days = activity_by_day(run)
        taken_days = activity_by_day(run, event_names=("Pellet Taken",))
        assert len(default_days[0].times) == 1
        assert len(taken_days[0].times) == 1
        # Different events, same day -- but a different single timestamp.
        assert default_days[0].times != taken_days[0].times

    def test_times_within_a_day_are_sorted(self, tmp_path):
        rows = [
            input_changed_row(5000, 1, 4, True, "MousePresence Detected"),
            input_changed_row(1000, 1, 4, True, "MousePresence Detected"),
            input_changed_row(3000, 1, 4, True, "MousePresence Detected"),
        ]
        run = _run(tmp_path, rows)
        days = activity_by_day(run)
        assert len(days) == 1
        assert days[0].times == sorted(days[0].times)

    def test_pools_across_nodes(self, tmp_path):
        rows = [
            input_changed_row(0, 1, 4, True, "MousePresence Detected"),
            input_changed_row(1000, 2, 4, True, "MousePresence Detected"),
        ]
        run = _run(tmp_path, rows)
        days = activity_by_day(run)
        assert len(days) == 1
        assert len(days[0].times) == 2


class TestStatsSanityCheckedHere:
    """Cross-file sanity: metrics rely on these being correct at the boundaries."""

    def test_wilson_ci_finite_and_bounded(self):
        eps = 1e-9
        for k, n in [(0, 0), (0, 10), (10, 10), (3, 7)]:
            p, lo, hi = wilson_ci(k, n)
            assert -eps <= lo <= p + eps and p - eps <= hi <= 1.0 + eps

    def test_chi2_sf_matches_published_critical_values(self):
        assert abs(chi2_sf(3.841, 1) - 0.05) < 0.001
        assert abs(chi2_sf(5.991, 2) - 0.05) < 0.001
        assert chi2_sf(0, 3) == 1.0
