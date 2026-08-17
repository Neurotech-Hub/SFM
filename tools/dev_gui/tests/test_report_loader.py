"""Tests for base_station.report.loader and .session."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from report_fixtures import (  # noqa: E402
    bandit_run, can_event_row, exp6_file, exp_row, heartbeat_payload,
    heartbeat_row, input_changed_row, legacy9_file, row, session_open_row,
    write_session,
)

from base_station.protocol import CanEvent  # noqa: E402
from base_station.report.loader import (  # noqa: E402
    LogSchema, discover_sessions, load_heartbeats, load_rows, sniff_schema,
)
from base_station.report.session import split_runs  # noqa: E402


class TestSniffSchema:
    def test_unified(self, tmp_path):
        path = write_session(tmp_path, [row(ts_ms=1000)])
        assert sniff_schema(path) == LogSchema.UNIFIED

    def test_legacy9(self, tmp_path):
        path = legacy9_file(tmp_path)
        assert sniff_schema(path) == LogSchema.LEGACY9

    def test_exp6(self, tmp_path):
        path = exp6_file(tmp_path)
        assert sniff_schema(path) == LogSchema.EXP6

    def test_unknown_for_missing_file(self, tmp_path):
        assert sniff_schema(tmp_path / "nope.csv") == LogSchema.UNKNOWN


class TestLoadRows:
    def test_round_trip(self, tmp_path):
        path = write_session(tmp_path, [
            can_event_row(1000, 1, CanEvent.Loaded),
            exp_row(1010, "trial", {"trial": 1}, trial=1),
        ])
        rows, schema, warnings = load_rows(path)
        assert schema == LogSchema.UNIFIED
        assert warnings == []
        assert len(rows) == 2
        assert rows[0].event_name == "Loaded"
        assert rows[0].fields == {}          # only EXP rows carry fields_json
        assert rows[1].fields == {"trial": 1}

    def test_legacy9_returns_no_rows_with_warning(self, tmp_path):
        path = legacy9_file(tmp_path)
        rows, schema, warnings = load_rows(path)
        assert rows == []
        assert schema == LogSchema.LEGACY9
        assert len(warnings) == 1
        assert "legacy9" in warnings[0]

    def test_malformed_fields_json_becomes_empty_dict(self, tmp_path):
        r = row(ts_ms=1000, source="EXP", frame_type="EXPERIMENT",
                event_name="trial", fields=None)
        # Corrupt the fields_json column directly (index matches HEADER order).
        from base_station.log_manager import LogManager
        idx = LogManager.CSV_HEADER.index("fields_json")
        r[idx] = "{not valid json"
        path = write_session(tmp_path, [r])
        rows, schema, warnings = load_rows(path)
        assert rows[0].fields == {}
        assert any("malformed fields_json" in w for w in warnings)

    def test_empty_file_returns_no_rows(self, tmp_path):
        path = write_session(tmp_path, [])
        rows, schema, warnings = load_rows(path)
        assert rows == []
        assert schema == LogSchema.UNIFIED
        assert warnings == []

    def test_can_event_disambiguates_dome_opened_milestone_vs_input_edge(self, tmp_path):
        # 03 0D 00 01 -> CanEvent.DomeOpened milestone (count=13, pellet_present=1)
        milestone = can_event_row(1000, 1, CanEvent.DomeOpened, bytes([0x0D, 0x00, 0x01]))
        # 06 03 01 -> InputChanged dome edge, renamed "Dome Opened" by the GUI
        edge = input_changed_row(2000, 1, 3, True, "Dome Opened")
        path = write_session(tmp_path, [milestone, edge])
        rows, _, _ = load_rows(path)
        assert rows[0].event_name == "Dome Opened"
        assert rows[0].can_event == CanEvent.DomeOpened
        assert rows[1].event_name == "Dome Opened"
        assert rows[1].can_event == CanEvent.InputChanged


class TestLoadHeartbeats:
    def test_decodes_real_payload_shape(self, tmp_path):
        # 00 0C 00 00 01 00 0C 00 -> presented=12, taken=12, pellet on plate
        payload = heartbeat_payload(state=0, presented=12, presence=False,
                                     pellet=True, taken=12)
        path = write_session(
            tmp_path, [row(ts_ms=1000)],
            heartbeat_rows=[heartbeat_row(1000, 2, payload)],
        )
        from base_station.log_manager import LogManager
        hb_path = LogManager.heartbeat_path_for(path)
        hbs = load_heartbeats(hb_path)
        assert len(hbs) == 1
        assert hbs[0].pellets_presented == 12
        assert hbs[0].pellets_taken == 12
        assert hbs[0].pellet is True
        assert hbs[0].presence is False

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_heartbeats(tmp_path / "nope_heartbeats.csv") == []


class TestDiscoverSessions:
    def test_finds_session_and_pairs_heartbeats(self, tmp_path):
        write_session(tmp_path, [row(ts_ms=1000)], session="A",
                       heartbeat_rows=[heartbeat_row(1000, 1, heartbeat_payload())])
        write_session(tmp_path, [row(ts_ms=2000)], session="B")
        refs = discover_sessions(tmp_path)
        by_name = {r.session: r for r in refs}
        assert set(by_name) == {"A", "B"}
        assert by_name["A"].heartbeats_path is not None
        assert by_name["B"].heartbeats_path is None

    def test_skips_heartbeat_files_as_sessions(self, tmp_path):
        write_session(tmp_path, [row(ts_ms=1000)], session="A",
                       heartbeat_rows=[heartbeat_row(1000, 1, heartbeat_payload())])
        refs = discover_sessions(tmp_path)
        assert [r.session for r in refs] == ["A"]

    def test_empty_dir_returns_empty(self, tmp_path):
        assert discover_sessions(tmp_path) == []


class TestSplitRuns:
    def test_two_runs_each_restart_at_zero(self, tmp_path):
        rows1 = [
            session_open_row(1_000_000, "S", 1),
            exp_row(1_000_010, "session_start", {"experiment": "free_feeding", "nodes": [1]}),
            can_event_row(1_000_020, 1, CanEvent.Loaded),
        ]
        rows2 = [
            session_open_row(2_000_000, "S", 2, mode="append"),
            exp_row(2_000_010, "session_start", {"experiment": "free_feeding", "nodes": [1]}, run_id=2),
            can_event_row(2_000_500, 1, CanEvent.Loaded, run_id=2),
        ]
        path = write_session(tmp_path, rows1 + rows2, session="S")
        loaded, schema, warnings = load_rows(path)
        runs = split_runs(loaded, [], path)
        assert len(runs) == 2
        run1 = next(r for r in runs if r.run_id == 1)
        run2 = next(r for r in runs if r.run_id == 2)
        assert run1.rows[0].t == 0.0
        assert run2.rows[0].t == 0.0
        assert abs(run2.rows[-1].t - 0.5) < 1e-6

    def test_out_of_order_rows_are_stable_sorted(self, tmp_path):
        r1 = exp_row(1_000_010, "trial", {"trial": 1}, trial=1)
        r2 = exp_row(1_000_005, "trial", {"trial": 0}, trial=0)  # earlier ts, written second
        path = write_session(tmp_path, [r1, r2])
        loaded, _, _ = load_rows(path)
        runs = split_runs(loaded, [], path)
        assert len(runs) == 1
        ordered_trials = [r.fields.get("trial") for r in runs[0].rows]
        assert ordered_trials == [0, 1]

    def test_elapsed_s_column_is_ignored(self, tmp_path):
        r = row(ts_ms=1_000_000, run_id=1)
        # Deliberately corrupt elapsed_s to a wrong value.
        from base_station.log_manager import LogManager
        idx = LogManager.CSV_HEADER.index("elapsed_s")
        r[idx] = "9999.999"
        path = write_session(tmp_path, [r])
        loaded, _, _ = load_rows(path)
        runs = split_runs(loaded, [], path)
        assert runs[0].rows[0].t == 0.0  # recomputed from timestamp_ms, not the bogus column

    def test_experiment_and_params_captured_from_start_rows(self, tmp_path):
        rows = bandit_run(n_trials=1)
        path = write_session(tmp_path, rows, session="Bandit01")
        loaded, _, _ = load_rows(path)
        runs = split_runs(loaded, [], path)
        assert len(runs) == 1
        run = runs[0]
        assert run.experiment == "two_armed_bandit"
        assert run.nodes == [1, 2]
        assert run.seed == 12345
        assert run.params["block_size"] == 3
        assert run.has_session_end is True
        assert run.end_fields["pellets"] == 1

    def test_rows_after_session_end_are_flagged(self, tmp_path):
        rows = [
            exp_row(1_000_000, "session_start", {"experiment": "free_feeding", "nodes": [1]}),
            exp_row(1_000_100, "session_end", {"elapsed_s": 0.1, "pellets": 0}),
            can_event_row(1_000_200, 1, CanEvent.Loaded),
        ]
        path = write_session(tmp_path, rows)
        loaded, _, _ = load_rows(path)
        runs = split_runs(loaded, [], path)
        run = runs[0]
        assert run.rows[-1].event_name == "Loaded"
        assert run.rows[-1].post_session is True
        assert run.rows[0].post_session is False

    def test_no_session_end_adds_note(self, tmp_path):
        rows = [exp_row(1_000_000, "session_start", {"experiment": "free_feeding", "nodes": [1]})]
        path = write_session(tmp_path, rows)
        loaded, _, _ = load_rows(path)
        runs = split_runs(loaded, [], path)
        assert runs[0].has_session_end is False
        assert any("session_end" in n for n in runs[0].notes)

    def test_heartbeats_scoped_to_their_run(self, tmp_path):
        rows1 = [session_open_row(1_000_000, "S", 1),
                 exp_row(1_000_010, "session_start", {"experiment": "free_feeding", "nodes": [1]})]
        rows2 = [session_open_row(2_000_000, "S", 2, mode="append"),
                 exp_row(2_000_010, "session_start", {"experiment": "free_feeding", "nodes": [1]}, run_id=2)]
        path = write_session(tmp_path, rows1 + rows2, session="S")
        hb1 = heartbeat_row(1_000_005, 1, heartbeat_payload())
        hb2 = heartbeat_row(2_000_005, 1, heartbeat_payload())
        from base_station.log_manager import LogManager
        from report_fixtures import write_csv
        write_csv(LogManager.heartbeat_path_for(path), [hb1, hb2])

        loaded, _, _ = load_rows(path)
        hb_loaded = load_heartbeats(LogManager.heartbeat_path_for(path))
        runs = split_runs(loaded, hb_loaded, path)
        run1 = next(r for r in runs if r.run_id == 1)
        run2 = next(r for r in runs if r.run_id == 2)
        assert len(run1.heartbeats) == 1
        assert len(run2.heartbeats) == 1
