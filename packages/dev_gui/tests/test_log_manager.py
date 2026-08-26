"""Tests for base_station.log_manager."""

import sys
import os
import tempfile
import csv
import time
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest
from base_station.log_manager import LogManager, LogEntry, sanitize_session_name


def make_entry(**kwargs) -> LogEntry:
    defaults = dict(
        timestamp=time.time(),
        direction="RX",
        node_id=1,
        frame_type="EVENT",
        event_name="OnPlate",
        raw_id=0x301,
        raw_data=bytes([0x01]),
        details="",
    )
    defaults.update(kwargs)
    return LogEntry(**defaults)


class TestLogManager:
    def test_add_and_retrieve(self):
        lm = LogManager(auto_save=False)
        lm.add(make_entry(event_name="OnPlate"))
        assert lm.total_count == 1

    def test_ring_buffer_overflow(self):
        lm = LogManager(max_entries=5, auto_save=False)
        for i in range(10):
            lm.add(make_entry(event_name=f"Event{i}"))
        assert lm.total_count == 5
        # Only the last 5 entries remain
        entries = lm.get_filtered(show_heartbeats=True)
        names = [e.event_name for e in entries]
        assert "Event0" not in names
        assert "Event9" in names

    def test_filter_by_node(self):
        lm = LogManager(auto_save=False)
        lm.add(make_entry(node_id=1, event_name="A"))
        lm.add(make_entry(node_id=2, event_name="B"))
        lm.add(make_entry(node_id=1, event_name="C"))
        result = lm.get_filtered(node_id=1)
        assert all(e.node_id == 1 for e in result)
        assert len(result) == 2

    def test_filter_by_type(self):
        lm = LogManager(auto_save=False)
        lm.add(make_entry(frame_type="EVENT"))
        lm.add(make_entry(frame_type="COMMAND"))
        lm.add(make_entry(frame_type="HEARTBEAT"))
        result = lm.get_filtered(frame_type="COMMAND", show_heartbeats=True)
        assert all(e.frame_type == "COMMAND" for e in result)

    def test_heartbeats_hidden_by_default(self):
        lm = LogManager(auto_save=False)
        lm.add(make_entry(frame_type="HEARTBEAT"))
        lm.add(make_entry(frame_type="EVENT"))
        result = lm.get_filtered(show_heartbeats=False)
        assert all(e.frame_type != "HEARTBEAT" for e in result)

    def test_heartbeats_shown_when_requested(self):
        lm = LogManager(auto_save=False)
        lm.add(make_entry(frame_type="HEARTBEAT"))
        result = lm.get_filtered(show_heartbeats=True)
        assert len(result) == 1

    def test_newest_first(self):
        lm = LogManager(auto_save=False)
        t = time.time()
        lm.add(make_entry(timestamp=t,     event_name="First"))
        lm.add(make_entry(timestamp=t+1.0, event_name="Second"))
        entries = lm.get_filtered(show_heartbeats=True)
        assert entries[0].event_name == "Second"

    def test_clear_empties_buffer(self):
        lm = LogManager(auto_save=False)
        for _ in range(5):
            lm.add(make_entry())
        lm.clear()
        assert lm.total_count == 0

    def test_export_csv(self, tmp_path):
        lm = LogManager(auto_save=False)
        lm.add(make_entry(event_name="TestEvent", node_id=3))
        export_path = tmp_path / "export.csv"
        out = lm.export(str(export_path))
        assert out.exists()
        with open(out) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["event_name"] == "TestEvent"
        assert rows[0]["node_id"] == "3"

    def test_autosave_csv(self, tmp_path):
        lm = LogManager(max_entries=100, log_dir=str(tmp_path), auto_save=True)
        lm.add(make_entry(event_name="AutoSaved"))
        lm.close()
        today = datetime.now().strftime("%Y%m%d")
        main = tmp_path / f"session_{today}.csv"
        hb = tmp_path / f"session_{today}_heartbeats.csv"
        assert main.exists()
        with open(main) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["event_name"] == "AutoSaved"
        assert rows[0]["session"] == f"session_{today}"
        assert rows[0]["run_id"] == "0"
        assert hb.exists()

    def test_heartbeats_go_to_sibling_csv_not_main(self, tmp_path):
        lm = LogManager(log_dir=str(tmp_path), auto_save=True)
        lm.add(make_entry(frame_type="EVENT", event_name="OnPlate"))
        lm.add(make_entry(frame_type="HEARTBEAT", event_name="", node_id=2))
        lm.close()
        today = datetime.now().strftime("%Y%m%d")
        main = tmp_path / f"session_{today}.csv"
        hb = tmp_path / f"session_{today}_heartbeats.csv"
        with open(main) as f:
            main_rows = list(csv.DictReader(f))
        with open(hb) as f:
            hb_rows = list(csv.DictReader(f))
        assert [r["frame_type"] for r in main_rows] == ["EVENT"]
        assert [r["frame_type"] for r in hb_rows] == ["HEARTBEAT"]
        assert hb_rows[0]["node_id"] == "2"
        # Still in the ring buffer for the GUI
        assert any(e.frame_type == "HEARTBEAT" for e in lm.all_entries())

    def test_named_session_heartbeats_sibling(self, tmp_path):
        lm = LogManager(auto_save=False)
        lm.open_session("cohortA", str(tmp_path))
        lm.add(make_entry(frame_type="HEARTBEAT", node_id=1))
        lm.add(make_entry(frame_type="EVENT", event_name="DomeClosed"))
        lm.close()
        with open(tmp_path / "cohortA.csv") as f:
            main_rows = list(csv.DictReader(f))
        with open(tmp_path / "cohortA_heartbeats.csv") as f:
            hb_rows = list(csv.DictReader(f))
        assert all(r["frame_type"] != "HEARTBEAT" for r in main_rows)
        assert any(r["frame_type"] == "EVENT" for r in main_rows)
        assert len(hb_rows) == 1
        assert hb_rows[0]["frame_type"] == "HEARTBEAT"

    def test_timestamp_str_format(self):
        entry = make_entry(timestamp=0.0)  # epoch midnight
        # Just check it's HH:MM:SS.mmm format
        ts = entry.timestamp_str
        assert len(ts) == 12  # "HH:MM:SS.mmm"
        assert ts[2] == ":" and ts[5] == ":"

    def test_raw_id_hex(self):
        entry = make_entry(raw_id=0x301)
        assert entry.raw_id_hex == "0x301"

    def test_raw_data_hex(self):
        entry = make_entry(raw_data=bytes([0x01, 0xAB]))
        assert entry.raw_data_hex == "01 AB"

    def test_combined_filter(self):
        lm = LogManager(auto_save=False)
        lm.add(make_entry(node_id=1, frame_type="EVENT"))
        lm.add(make_entry(node_id=1, frame_type="COMMAND"))
        lm.add(make_entry(node_id=2, frame_type="EVENT"))
        result = lm.get_filtered(node_id=1, frame_type="EVENT")
        assert len(result) == 1
        assert result[0].node_id == 1
        assert result[0].frame_type == "EVENT"


class TestSanitizeSessionName:
    def test_basic_passthrough(self):
        assert sanitize_session_name("cohortA_day3") == "cohortA_day3"

    def test_spaces_and_punctuation_collapse(self):
        assert sanitize_session_name("cohort A / day 3!") == "cohort_A_day_3"

    def test_blank_and_punctuation_only_are_empty(self):
        assert sanitize_session_name("   ") == ""
        assert sanitize_session_name("***") == ""

    def test_length_capped(self):
        assert len(sanitize_session_name("x" * 200)) == 64


class TestNamedSession:
    def test_fresh_session_creates_file_with_run_id_1(self, tmp_path):
        lm = LogManager(auto_save=False)
        run_id = lm.open_session("cohortA", str(tmp_path))
        assert run_id == 1
        assert lm.csv_path == tmp_path / "cohortA.csv"
        with open(lm.csv_path) as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["frame_type"] == "SESSION_OPEN"
        lm.close()

    def test_reopening_same_session_appends_and_increments_run_id(self, tmp_path):
        lm = LogManager(auto_save=False)
        run_id_1 = lm.open_session("cohortA", str(tmp_path))
        lm.add(make_entry(event_name="Row1"))
        lm.close()

        lm2 = LogManager(auto_save=False)
        run_id_2 = lm2.open_session("cohortA", str(tmp_path))
        assert run_id_2 == run_id_1 + 1
        lm2.add(make_entry(event_name="Row2"))
        lm2.close()

        with open(tmp_path / "cohortA.csv") as f:
            rows = list(csv.DictReader(f))
        # SESSION_OPEN (run1) + Row1 + SESSION_OPEN (run2) + Row2
        assert len(rows) == 4
        assert rows[-1]["event_name"] == "Row2"
        assert rows[-1]["run_id"] == "2"

    def test_stale_header_diverts_to_timestamped_file(self, tmp_path):
        stale = tmp_path / "cohortA.csv"
        with open(stale, "w", newline="") as f:
            csv.writer(f).writerow(["some", "old", "header"])

        lm = LogManager(auto_save=False)
        run_id = lm.open_session("cohortA", str(tmp_path))
        assert run_id == 1
        assert lm.csv_path != stale
        assert lm.csv_path.name.startswith("cohortA_")
        lm.close()
        # The stale file must survive untouched.
        with open(stale) as f:
            assert f.readline().strip() == "some,old,header"

    def test_open_session_rejects_empty_name(self, tmp_path):
        lm = LogManager(auto_save=False)
        with pytest.raises(ValueError):
            lm.open_session("   ***   ", str(tmp_path))

    def test_fields_json_round_trips_spaces_and_equals(self, tmp_path):
        lm = LogManager(auto_save=False)
        lm.open_session("cohortA", str(tmp_path))
        lm.add(make_entry(
            frame_type="EXPERIMENT",
            fields={"note": "a=b has spaces", "count": 3},
        ))
        lm.close()
        with open(tmp_path / "cohortA.csv") as f:
            rows = list(csv.DictReader(f))
        payload = json.loads(rows[-1]["fields_json"])
        assert payload == {"note": "a=b has spaces", "count": 3}

    def test_run_id_and_trial_stamped_from_context(self, tmp_path):
        lm = LogManager(auto_save=False)
        run_id = lm.open_session("cohortA", str(tmp_path))
        lm.set_context(trial=5)
        lm.add(make_entry())
        entries = lm.get_filtered(show_heartbeats=True)
        assert entries[0].run_id == run_id
        assert entries[0].trial == 5
        lm.close()


class TestDailySink:
    def test_same_day_second_manager_appends(self, tmp_path):
        lm1 = LogManager(log_dir=str(tmp_path), auto_save=True)
        lm1.add(make_entry(event_name="First"))
        lm1.close()

        lm2 = LogManager(log_dir=str(tmp_path), auto_save=True)
        lm2.add(make_entry(event_name="Second"))
        lm2.close()

        today = datetime.now().strftime("%Y%m%d")
        mains = [p for p in tmp_path.glob("session_*.csv") if "_heartbeats" not in p.name]
        assert mains == [tmp_path / f"session_{today}.csv"]
        with open(mains[0]) as f:
            rows = list(csv.DictReader(f))
        assert [r["event_name"] for r in rows] == ["First", "Second"]
        assert all(r["run_id"] == "0" for r in rows)

    def test_stale_daily_header_diverts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(LogManager, "_today_stamp", lambda self: "20260824")
        stale = tmp_path / "session_20260824.csv"
        with open(stale, "w", newline="") as f:
            csv.writer(f).writerow(["old", "header"])
        lm = LogManager(log_dir=str(tmp_path), auto_save=True)
        assert lm.csv_path != stale
        assert lm.csv_path.name.startswith("session_20260824_")
        lm.close()
        with open(stale) as f:
            assert f.readline().strip() == "old,header"

    def test_midnight_rollover_opens_new_day_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(LogManager, "_today_stamp", lambda self: "20260824")
        lm = LogManager(log_dir=str(tmp_path), auto_save=True)
        lm.add(make_entry(event_name="Mon"))
        monkeypatch.setattr(LogManager, "_today_stamp", lambda self: "20260825")
        lm.add(make_entry(event_name="Tue"))
        lm.close()
        with open(tmp_path / "session_20260824.csv") as f:
            assert [r["event_name"] for r in csv.DictReader(f)] == ["Mon"]
        with open(tmp_path / "session_20260825.csv") as f:
            assert [r["event_name"] for r in csv.DictReader(f)] == ["Tue"]


class TestResumeDaily:
    def test_post_session_rows_go_to_daily_not_experiment(self, tmp_path):
        lm = LogManager(auto_save=False)
        lm.open_session("Bandit_test_01", str(tmp_path))
        lm.add(make_entry(
            frame_type="EXPERIMENT", event_name="session_end", source="EXP",
        ))
        exp_path = tmp_path / "Bandit_test_01.csv"
        lm.resume_daily(str(tmp_path))
        assert lm.is_named_session is False
        lm.add(make_entry(event_name="PostStopCommand", frame_type="COMMAND"))
        lm.add(make_entry(frame_type="HEARTBEAT", node_id=1))
        lm.close()

        with open(exp_path) as f:
            exp_rows = list(csv.DictReader(f))
        assert exp_rows[-1]["event_name"] == "session_end"
        assert all(r["event_name"] != "PostStopCommand" for r in exp_rows)
        assert all(r["frame_type"] != "HEARTBEAT" for r in exp_rows)

        today = datetime.now().strftime("%Y%m%d")
        with open(tmp_path / f"session_{today}.csv") as f:
            daily_rows = list(csv.DictReader(f))
        assert [r["event_name"] for r in daily_rows] == ["PostStopCommand"]
        assert all(r["run_id"] == "0" for r in daily_rows)

        with open(tmp_path / f"session_{today}_heartbeats.csv") as f:
            hb_rows = list(csv.DictReader(f))
        assert len(hb_rows) == 1
        assert hb_rows[0]["frame_type"] == "HEARTBEAT"

        # Experiment heartbeat sibling must not receive post-stop heartbeats
        with open(tmp_path / "Bandit_test_01_heartbeats.csv") as f:
            exp_hb = list(csv.DictReader(f))
        assert exp_hb == []

    def test_resume_daily_is_noop_when_already_on_today(self, tmp_path):
        lm = LogManager(log_dir=str(tmp_path), auto_save=True)
        path = lm.csv_path
        lm.resume_daily(str(tmp_path))
        assert lm.csv_path == path
        lm.close()

    def test_reopen_named_after_resume_still_bumps_run_id(self, tmp_path):
        lm = LogManager(auto_save=False)
        assert lm.open_session("cohortA", str(tmp_path)) == 1
        lm.add(make_entry(event_name="Run1"))
        lm.resume_daily(str(tmp_path))
        assert lm.open_session("cohortA", str(tmp_path)) == 2
        lm.add(make_entry(event_name="Run2"))
        lm.close()
        with open(tmp_path / "cohortA.csv") as f:
            rows = list(csv.DictReader(f))
        names = [r["event_name"] for r in rows]
        assert "Run1" in names and "Run2" in names
        assert rows[-1]["run_id"] == "2"
        assert all(r["event_name"] != "PostStopCommand" for r in rows)
