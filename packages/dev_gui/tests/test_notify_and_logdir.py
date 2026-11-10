"""
Phone-alert settings and moving the log folder, as the web page uses them.
No test here touches the network: sending is replaced by a recorder.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import pytest

from base_station import notifier
from base_station.commands import run_command
from base_station.log_manager import LogEntry, LogManager
from base_station.node_registry import NodeRegistry
from base_station.notifier import clean_form
from base_station.state import build_snapshot

from tests.test_engine import _args, _FakeCan, _RecordingEngine

GOOD = dict(enabled=True, server="https://ntfy.sh", topic="sfm-lab-a-7Qx2", station_name="Lab A", faults=["PelletLost"])


@pytest.fixture
def engine(tmp_path):
    e = _RecordingEngine(_args())
    e._registry = NodeRegistry(2)
    e._exp_log_dir = str(tmp_path / "a")
    e._log = LogManager(log_dir=e._exp_log_dir, auto_save=True)
    e._can = _FakeCan()
    yield e
    e._log.close()


@pytest.fixture
def sent(monkeypatch):
    """Replace the network call; collect what would have been sent."""
    out = []
    monkeypatch.setattr(notifier, "_post", lambda item: out.append(item))
    return out


def ok(engine, name, **a):
    r = run_command(engine, name, a)
    assert r["ok"], r
    return r


def err(engine, name, **a):
    r = run_command(engine, name, a)
    assert r["ok"] is False, r
    return r["error"]


# ---------------------------------------------------------------------------
# The form rules
# ---------------------------------------------------------------------------

def test_a_good_form_is_accepted_and_normalised() -> None:
    clean, e = clean_form(True, "  ntfy.example.org ", " my-topic_1 ", "  Lab A ", ["ActuatorTimeout", "PelletLost"])
    assert e == "" and clean == {"enabled": True, "server": "https://ntfy.example.org", "topic": "my-topic_1",
                                 "station_name": "Lab A", "faults": ["PelletLost", "ActuatorTimeout"]}, "faults keep the checkbox order"


def test_an_empty_topic_and_server_are_allowed_and_mean_nothing_is_sent_to_the_default_server() -> None:
    clean, e = clean_form(False, "", "", "", [])
    assert e == "" and clean["server"] == "https://ntfy.sh" and clean["topic"] == "" and clean["faults"] == []


@pytest.mark.parametrize("args,fragment", [
    ((True, "https://", "t", "n", []), "http(s) URL"),
    ((True, "https://a b.c", "t", "n", []), "http(s) URL"),
    ((True, "https://" + "x" * 300, "t", "n", []), "http(s) URL"),
    ((True, "", "has space", "n", []), "Topic may only"),
    ((True, "", "umläut", "n", []), "Topic may only"),
    ((True, "", "a/b", "n", []), "Topic may only"),
    ((True, "", "x" * 65, "n", []), "Topic may only"),
    ((True, "", "t", "bad\nname", []), "Name must be plain"),
    ((True, "", "t", "x" * 65, []), "Name must be plain"),
    ((True, "", "t", "n", ["Nope"]), "Unknown fault"),
    ((True, "", "t", "n", "PelletLost"), "Choose which faults"),
    (("yes", "", "t", "n", []), "on or off"),
    ((True, "", 5, "n", []), "Topic must be text"),
])
def test_bad_form_values_are_refused_with_a_reason(args, fragment) -> None:
    clean, e = clean_form(*args)
    assert clean is None and fragment in e


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def test_settings_are_saved_and_a_new_engine_reads_them_back(engine) -> None:
    out = ok(engine, "set_notify_settings", **GOOD)
    assert out["saved"] and out["has_topic"]
    other = _RecordingEngine(_args())            # a fresh engine process, same home
    got = ok(other, "get_notify_settings")
    assert got["settings"] == {**GOOD, "faults": ["PelletLost"]} and got["known_faults"] == ["PelletLost", "FeedTimeout", "ActuatorTimeout"]
    assert json.loads(Path(engine._notifier.settings.path).read_text())["topic"] == "sfm-lab-a-7Qx2"


def test_a_refused_form_changes_nothing(engine) -> None:
    ok(engine, "set_notify_settings", **GOOD)
    before = Path(engine._notifier.settings.path).read_text()
    assert "Topic may only" in err(engine, "set_notify_settings", **{**GOOD, "topic": "bad topic"})
    assert Path(engine._notifier.settings.path).read_text() == before
    assert engine._notifier.settings.topic == "sfm-lab-a-7Qx2"


def test_a_failed_save_keeps_the_old_settings(engine, monkeypatch) -> None:
    ok(engine, "set_notify_settings", **GOOD)
    monkeypatch.setattr(notifier, "save_preserving", lambda path, values: (_ for _ in ()).throw(OSError("disk full")))
    assert "disk full" in err(engine, "set_notify_settings", **{**GOOD, "topic": "other"})
    assert engine._notifier.settings.topic == "sfm-lab-a-7Qx2"


# A settings file written by a newer version can hold things this one doesn't manage.
NEWER = {"version": 1, "enabled": True, "server": "https://ntfy.sh", "topic": "ABC-SFM", "token": "tk_secret",
         "station_name": "Base-station-02", "faults": ["Jam", "PelletLost", "FeedTimeout", "ActuatorTimeout"], "future_key": {"a": 1}}


def test_saving_keeps_keys_and_fault_names_this_version_does_not_know(engine) -> None:
    path = Path(engine._notifier.settings.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(NEWER))
    engine._notifier.reload()
    ok(engine, "set_notify_settings", **{**GOOD, "topic": "changed", "faults": ["PelletLost"]})
    raw = json.loads(path.read_text())
    assert raw["token"] == "tk_secret" and raw["future_key"] == {"a": 1}, "unknown keys must survive a save"
    assert "Jam" in raw["faults"] and "PelletLost" in raw["faults"] and "FeedTimeout" not in raw["faults"]
    assert raw["topic"] == "changed" and raw["station_name"] == "Lab A"
    assert engine._notifier.settings.topic == "changed", "and the engine uses the new values"
    assert engine._notifier.settings.faults == ["PelletLost"], "the engine only sends the faults it knows"


def test_the_page_is_told_what_is_being_kept(engine) -> None:
    path = Path(engine._notifier.settings.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(NEWER))
    kept = ok(engine, "get_notify_settings")["kept"]
    assert kept == {"keys": ["future_key", "token"], "faults": ["Jam"]}


def test_nothing_is_reported_as_kept_for_a_plain_or_missing_file(engine, tmp_path) -> None:
    assert notifier.kept_extras(tmp_path / "missing.json") == {"keys": [], "faults": []}
    ok(engine, "set_notify_settings", **GOOD)
    assert ok(engine, "get_notify_settings")["kept"] == {"keys": [], "faults": []}


def test_a_corrupt_or_odd_file_is_replaced_not_a_crash(engine) -> None:
    path = Path(engine._notifier.settings.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("{ not json", "[1, 2]", '{"faults": "oops"}'):
        path.write_text(text)
        ok(engine, "set_notify_settings", **GOOD)
        assert json.loads(path.read_text())["topic"] == "sfm-lab-a-7Qx2"


def test_the_topic_is_never_in_the_pushed_state(engine) -> None:
    ok(engine, "set_notify_settings", **GOOD)
    snap = build_snapshot(engine)
    assert snap["notify"]["configured"] is True and snap["notify"]["enabled"] is True
    assert "sfm-lab-a-7Qx2" not in json.dumps(snap), "the topic is the password on the free server"


def test_a_test_needs_a_saved_topic(engine, sent) -> None:
    assert "Save a topic first" in err(engine, "send_test_notification")
    assert sent == []
    assert build_snapshot(engine)["notify"]["configured"] is False


def test_a_test_is_sent_and_its_result_reaches_the_state(engine, sent) -> None:
    ok(engine, "set_notify_settings", **GOOD)
    t0 = time.time()
    ok(engine, "send_test_notification")
    engine._notifier.flush()
    assert len(sent) == 1 and sent[0].url == "https://ntfy.sh/sfm-lab-a-7Qx2" and "Lab A" in sent[0].title
    last = build_snapshot(engine)["notify"]["last"]
    assert last["ok"] is True and last["when"] >= t0


def test_a_failed_send_is_reported_with_the_reason(engine, monkeypatch) -> None:
    def boom(item):
        raise OSError("name or service not known")
    monkeypatch.setattr(notifier, "_post", boom)
    ok(engine, "set_notify_settings", **GOOD)
    ok(engine, "send_test_notification")
    engine._notifier.flush()
    last = build_snapshot(engine)["notify"]["last"]
    assert last["ok"] is False and "name or service not known" in last["detail"]


def test_a_fault_still_sends_an_alert_after_the_settings_were_saved_from_the_page(engine, sent) -> None:
    from base_station.protocol import ServiceStatus
    ok(engine, "set_notify_settings", **GOOD)
    engine._notifier.on_fault(1, ServiceStatus.PelletLost, "front", "run1")
    engine._notifier.on_fault(1, ServiceStatus.FeedTimeout, "front", "run1")      # not ticked: not sent
    engine._notifier.flush()
    assert len(sent) == 1 and "PelletLost" in sent[0].title


# ---------------------------------------------------------------------------
# Moving the log folder
# ---------------------------------------------------------------------------

def rows(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))[1:]


def add(engine, text):
    engine._log.add(LogEntry(timestamp=time.time(), direction="RX", node_id=1, frame_type="EVENT",
                             event_name=text, raw_id=1, raw_data=b""))


def daily(folder: Path):
    return [p for p in folder.glob("session_*.csv") if "_heartbeats" not in p.stem]


def test_the_daily_log_moves_and_the_old_file_stays_complete(engine, tmp_path) -> None:
    add(engine, "before")
    new = tmp_path / "b"
    out = ok(engine, "set_log_dir", path=str(new))
    assert out["log_dir"] == str(new) and engine._exp_log_dir == str(new)
    add(engine, "after")
    engine._log.close()
    old_rows = [r[10] for r in rows(daily(tmp_path / "a")[0])]
    new_rows = [r[10] for r in rows(daily(new)[0])]
    assert "before" in old_rows and "after" not in old_rows, "the old file is complete and no longer written"
    assert "after" in new_rows and "before" not in new_rows
    assert "LogFolderChanged" in new_rows, "the change is recorded in the new log"


def test_sessions_and_exports_follow_the_new_folder(engine, tmp_path) -> None:
    new = tmp_path / "b"
    ok(engine, "set_log_dir", path=str(new))
    ok(engine, "start_session", experiment="free_feeding", session_name="after_move")
    assert (new / "after_move.csv").exists() and not (tmp_path / "a" / "after_move.csv").exists()
    ok(engine, "stop_session")
    assert Path(ok(engine, "log_export")["path"]).parent == new
    assert ok(engine, "preview_session", session_name="after_move")["will_create"] is False
    assert build_snapshot(engine)["log"]["dir"] == str(new)


def test_the_folder_cannot_change_while_a_session_runs(engine, tmp_path) -> None:
    ok(engine, "start_session", experiment="free_feeding", session_name="running")
    assert "Stop the session" in err(engine, "set_log_dir", path=str(tmp_path / "b"))
    assert engine._exp_log_dir == str(tmp_path / "a") and not (tmp_path / "b").exists()
    ok(engine, "stop_session")


def test_a_bad_folder_is_refused_and_logging_carries_on_where_it_was(engine, tmp_path) -> None:
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    assert err(engine, "set_log_dir", path=str(blocker))                 # a file, not a folder
    assert "Enter a log folder" in err(engine, "set_log_dir", path="   ")
    assert engine._exp_log_dir == str(tmp_path / "a")
    add(engine, "still here")
    engine._log.close()
    assert "still here" in [r[10] for r in rows(daily(tmp_path / "a")[0])]


def test_choosing_the_current_folder_again_does_nothing(engine, tmp_path) -> None:
    n = len(engine._log.all_entries())
    ok(engine, "set_log_dir", path=str(tmp_path / "a") + "/")
    assert len(engine._log.all_entries()) == n, "no log row for a no-op"


def test_it_needs_the_bus(engine, tmp_path) -> None:
    real, engine._log = engine._log, None
    try:
        assert "Connect to the bus" in err(engine, "set_log_dir", path=str(tmp_path / "b"))
    finally:
        engine._log = real


def test_with_auto_save_off_only_the_folder_changes(tmp_path) -> None:
    lm = LogManager(log_dir=str(tmp_path / "a"), auto_save=False)
    lm.move_to(str(tmp_path / "b"))
    assert lm.csv_path is None and not (tmp_path / "b").exists(), "no daily file is opened that wasn't asked for"
    lm.close()


def test_the_log_manager_refuses_to_move_under_an_open_session(tmp_path) -> None:
    lm = LogManager(log_dir=str(tmp_path / "a"), auto_save=True)
    lm.open_session("s", str(tmp_path / "a"))
    with pytest.raises(RuntimeError):
        lm.move_to(str(tmp_path / "b"))
    lm.close()


def test_a_folder_that_cannot_be_opened_puts_logging_back(tmp_path, monkeypatch) -> None:
    lm = LogManager(log_dir=str(tmp_path / "a"), auto_save=True)
    real = lm._open_daily
    calls = []

    def flaky(d=None):
        calls.append(d)
        if d == str(tmp_path / "b"):
            raise OSError("read-only")
        return real(d)
    monkeypatch.setattr(lm, "_open_daily", flaky)
    with pytest.raises(OSError):
        lm.move_to(str(tmp_path / "b"))
    assert lm._log_dir == str(tmp_path / "a") and calls == [str(tmp_path / "b"), str(tmp_path / "a")]
    lm.add(LogEntry(timestamp=time.time(), direction="RX", node_id=1, frame_type="EVENT", event_name="x", raw_id=1, raw_data=b""))
    lm.close()
    assert "x" in [r[10] for r in rows(daily(tmp_path / "a")[0])]


def test_a_file_edited_after_the_engine_started_is_what_the_page_sees(engine) -> None:
    path = Path(engine._notifier.settings.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**NEWER, "topic": "edited-by-hand"}))
    assert ok(engine, "get_notify_settings")["settings"]["topic"] == "edited-by-hand"
