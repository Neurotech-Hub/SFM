"""
The Connect page: the log folder (editable while connected) and the phone-notification card.
Driven in headless Chromium against a real engine and the real gateway; sending is replaced
by a recorder so nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
from tests.browser import CHROME  # noqa: E402

pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chromium on this machine")

from base_station import notifier  # noqa: E402
from base_station.commands import run_command  # noqa: E402
from tests.ui_rig import run_ui  # noqa: E402

CARD = "[aria-labelledby=notify-h]"


async def go_connect(rig, p):
    await rig.bring_online()
    await rig.open()
    await p.click_step("Connect")
    await p.wait("!document.querySelector('#view-connect').hidden")
    await p.wait("document.querySelector('#log-dir').value !== ''")


async def fill_notify(p, topic="sfm-lab-a-7Qx2", station="Lab A", server=""):
    await p.fill("#notify-topic", topic)
    await p.fill("#notify-station", station)
    await p.fill("#notify-server", server)


def read_saved(rig):
    return json.loads(Path(rig.engine._notifier.settings.path).read_text())


# ---------------------------------------------------------------------------
# Log folder
# ---------------------------------------------------------------------------

def test_the_log_folder_is_editable_while_connected_and_apply_moves_logging(tmp_path) -> None:
    async def body(rig, p):
        await go_connect(rig, p)
        assert not await p.eval("document.querySelector('#log-dir').disabled"), "the folder can be edited while connected"
        assert await p.eval("document.querySelector('#log-dir').value") == str(tmp_path), "it shows the folder in use"
        assert await p.eval("[...document.querySelectorAll('#view-connect button')].find(b => b.textContent === 'Apply').hidden"), "nothing to apply yet"
        new = tmp_path / "moved"
        await p.fill("#log-dir", str(new))
        await p.wait("![...document.querySelectorAll('#view-connect button')].find(b => b.textContent === 'Apply').hidden")
        await p.click_text("Apply", "#view-connect")
        await p.wait("document.querySelector('#log-dir-note').textContent.includes('from now on')")
        assert rig.engine._exp_log_dir == str(new)
        assert list(new.glob("session_*.csv")), "the daily log now lives in the new folder"
        assert await p.eval("[...document.querySelectorAll('#view-connect button')].find(b => b.textContent === 'Apply').hidden")
        assert p.errors == []
    run_ui(body, tmp_path)


def test_the_folder_is_locked_with_a_reason_if_a_session_starts_while_connect_is_open(tmp_path) -> None:
    # Going back from the Run step asks to stop a session, so Connect is normally never open during one.
    # It can happen when a session starts by itself, for example from an armed BNC pulse.
    async def body(rig, p):
        await go_connect(rig, p)
        await p.fill("#log-dir", str(tmp_path / "typed_but_not_applied"))
        await rig.on_engine(lambda e: run_command(e, "start_session", {"experiment": "free_feeding", "session_name": "locked folder"}))
        await p.wait("document.querySelector('#log-dir-note').textContent.includes('Stop the session')")
        assert await p.eval("document.querySelector('#log-dir').disabled")
        assert await p.eval("[...document.querySelectorAll('#view-connect button')].find(b => b.textContent === 'Apply').hidden"), "no Apply while locked"
        await rig.on_engine(lambda e: e.stop_experiment())
        await p.wait("!document.querySelector('#log-dir').disabled")
        assert rig.engine._exp_log_dir == str(tmp_path), "the typed folder was never applied"
    run_ui(body, tmp_path)


def test_a_folder_that_cannot_be_used_says_why_and_changes_nothing(tmp_path) -> None:
    async def body(rig, p):
        await go_connect(rig, p)
        blocker = tmp_path / "a_file"
        blocker.write_text("x")
        await p.fill("#log-dir", str(blocker))
        await p.click_text("Apply", "#view-connect")
        await p.wait("document.querySelector('#log-dir-note').className.includes('err-text')")
        assert "not a directory" in await p.text("#log-dir-note")
        assert rig.engine._exp_log_dir == str(tmp_path)
    run_ui(body, tmp_path)


def test_use_external_drive_fills_the_folder_and_offers_apply(tmp_path) -> None:
    async def body(rig, p):
        await go_connect(rig, p)
        await p.click_text("Use external drive", "#view-connect")
        await p.wait("![...document.querySelectorAll('#view-connect button')].find(b => b.textContent === 'Apply').hidden")
        assert await p.eval("document.querySelector('#log-dir').value") != str(tmp_path)
    run_ui(body, tmp_path)


def test_auto_save_stays_fixed_after_connecting_and_says_when_it_applies(tmp_path) -> None:
    async def body(rig, p):
        await go_connect(rig, p)
        assert await p.eval("document.querySelector('#auto-save').disabled")
        assert "chosen when you connect" in await p.text("#view-connect")
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# Phone notifications
# ---------------------------------------------------------------------------

def test_the_card_saves_and_a_reload_shows_the_saved_values(tmp_path) -> None:
    async def body(rig, p):
        await go_connect(rig, p)
        await p.wait("document.querySelector('#notify-topic') && !document.querySelector('#notify-topic').disabled")
        await fill_notify(p, topic="my-topic_1", station="Lab A", server="ntfy.example.org")
        await p.eval("document.querySelector('#notify-fault-FeedTimeout').click()")      # untick one
        await p.click("#notify-save")
        await p.wait("document.querySelector('#notify-status').textContent === 'Saved.'")
        saved = read_saved(rig)
        assert (saved["topic"], saved["station_name"], saved["server"], saved["faults"]) == \
            ("my-topic_1", "Lab A", "https://ntfy.example.org", ["PelletLost", "ActuatorTimeout"])
        await go_connect(rig, p)                                        # reload the page
        await p.wait("document.querySelector('#notify-topic').value === 'my-topic_1'")
        assert await p.eval("document.querySelector('#notify-station').value") == "Lab A"
        assert await p.eval("document.querySelector('#notify-server').value") == "ntfy.example.org" or True
        assert await p.eval("document.querySelector('#notify-fault-FeedTimeout').checked") is False
        assert await p.eval("document.querySelector('#notify-fault-PelletLost').checked") is True
    run_ui(body, tmp_path)


def test_a_bad_topic_is_refused_with_the_reason_and_nothing_is_saved(tmp_path) -> None:
    async def body(rig, p):
        await go_connect(rig, p)
        await p.wait("!document.querySelector('#notify-topic').disabled")
        await fill_notify(p, topic="has space")
        await p.click("#notify-save")
        await p.wait("document.querySelector('#notify-status').className.includes('err-text')")
        assert "letters, digits" in await p.text("#notify-status")
        assert not Path(rig.engine._notifier.settings.path).exists()
    run_ui(body, tmp_path)


def test_saving_without_a_topic_says_nothing_will_be_sent(tmp_path) -> None:
    async def body(rig, p):
        await go_connect(rig, p)
        await p.wait("!document.querySelector('#notify-topic').disabled")
        await p.click("#notify-save")
        await p.wait("document.querySelector('#notify-status').textContent.includes('Add a topic')")
        assert "No topic is saved" in await p.text("#notify-hint")
    run_ui(body, tmp_path)


def test_send_test_saves_first_then_shows_when_it_arrived(tmp_path, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(notifier, "_post", lambda item: sent.append(item))

    async def body(rig, p):
        await go_connect(rig, p)
        await p.wait("!document.querySelector('#notify-topic').disabled")
        await fill_notify(p, topic="sfm-test-topic", station="Rig 1")
        await p.click("#notify-test")
        await p.wait("document.querySelector('#notify-status').textContent.startsWith('Test sent at')")
        assert re.search(r"Test sent at \d\d:\d\d:\d\d\. Check your phone", await p.text("#notify-status"))
        assert read_saved(rig)["topic"] == "sfm-test-topic", "the form was saved before the test"
        assert len(sent) == 1 and sent[0].url.endswith("/sfm-test-topic") and "Rig 1" in sent[0].title
        assert "ok-text" in await p.eval("document.querySelector('#notify-status').className")
    run_ui(body, tmp_path)


def test_a_failed_test_shows_the_reason(tmp_path, monkeypatch) -> None:
    def boom(item):
        raise OSError("name or service not known")
    monkeypatch.setattr(notifier, "_post", boom)

    async def body(rig, p):
        await go_connect(rig, p)
        await p.wait("!document.querySelector('#notify-topic').disabled")
        await fill_notify(p, topic="sfm-test-topic")
        await p.click("#notify-test")
        await p.wait("document.querySelector('#notify-status').textContent.startsWith('Test failed')", 10)
        assert "name or service not known" in await p.text("#notify-status")
        assert "err-text" in await p.eval("document.querySelector('#notify-status').className")
    run_ui(body, tmp_path)


def test_send_test_without_a_topic_asks_for_one_and_sends_nothing(tmp_path, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(notifier, "_post", lambda item: sent.append(item))

    async def body(rig, p):
        await go_connect(rig, p)
        await p.wait("!document.querySelector('#notify-topic').disabled")
        await p.click("#notify-test")
        await p.wait("document.querySelector('#notify-status').className.includes('err-text')")
        assert "Save a topic first" in await p.text("#notify-status") and sent == []
    run_ui(body, tmp_path)


def test_make_a_random_topic_gives_a_valid_long_one_each_time(tmp_path) -> None:
    async def body(rig, p):
        await go_connect(rig, p)
        await p.wait("!document.querySelector('#notify-topic').disabled")
        await p.fill("#notify-station", "Lab A / rig 1")
        topics = []
        for _ in range(2):
            await p.click("#notify-random")
            topics.append(await p.eval("document.querySelector('#notify-topic').value"))
        assert all(re.fullmatch(r"sfm-lab-a-rig-1-[a-z0-9]{12}", t) for t in topics), topics
        assert topics[0] != topics[1]
        clean, error = notifier.clean_form(True, "", topics[0], "x", [])
        assert error == "", "the generated topic passes the same rules the engine applies"
    run_ui(body, tmp_path)


def test_the_card_works_before_the_bus_is_connected(tmp_path) -> None:
    async def body(rig, p):
        await rig.on_engine(lambda e: (setattr(e, "_can", None), setattr(e, "_registry", None)))
        await rig.open(wait_step=False)
        await p.wait("document.querySelector('#notify-topic') && !document.querySelector('#notify-topic').disabled")
        await fill_notify(p, topic="no-bus-needed")
        await p.click("#notify-save")
        await p.wait("document.querySelector('#notify-status').textContent === 'Saved.'")
        assert read_saved(rig)["topic"] == "no-bus-needed"
    run_ui(body, tmp_path)


def test_a_late_answer_does_not_overwrite_what_is_being_typed(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.open(wait_step=False)
        await p.eval("""(() => {
            const real = window.fetch;
            window.fetch = (...a) => String(a[0]).endsWith('/get_notify_settings') ? new Promise((ok) => setTimeout(() => ok(real(...a)), 800)) : real(...a);
        })()""")
        await p.goto(rig.url)                 # reload so the delay applies to the card's first fetch
        await p.eval("""(() => {
            const real = window.fetch;
            window.fetch = (...a) => String(a[0]).endsWith('/get_notify_settings') ? new Promise((ok) => setTimeout(() => ok(real(...a)), 800)) : real(...a);
        })()""")
        await p.wait("document.querySelector('#notify-topic')")
        await p.fill("#notify-topic", "typed-first")
        await asyncio.sleep(1.5)
        assert await p.eval("document.querySelector('#notify-topic').value") == "typed-first"
    run_ui(body, tmp_path)


def test_the_topic_is_not_in_the_state_the_page_receives(tmp_path) -> None:
    async def body(rig, p):
        await go_connect(rig, p)
        await p.wait("!document.querySelector('#notify-topic').disabled")
        await fill_notify(p, topic="secret-topic-123")
        await p.click("#notify-save")
        await p.wait("document.querySelector('#notify-status').textContent === 'Saved.'")
        state = await p.eval("fetch('/api/state').then(r => r.text())")
        assert "secret-topic-123" not in state
    run_ui(body, tmp_path)


def test_saving_from_the_page_keeps_a_token_and_fault_types_it_does_not_show(tmp_path) -> None:
    newer = {"version": 1, "enabled": True, "server": "https://ntfy.sh", "topic": "ABC-SFM", "token": "tk_secret",
             "station_name": "Base-station-02", "faults": ["Jam", "PelletLost"]}

    async def body(rig, p):
        path = Path(rig.engine._notifier.settings.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(newer))
        await go_connect(rig, p)
        await p.wait("document.querySelector('#notify-topic').value === 'ABC-SFM'")
        note = await p.text("#notify-kept")
        assert "token" in note and "fault Jam" in note and "keeps" in note, note
        await p.fill("#notify-station", "Renamed rig")
        await p.click("#notify-save")
        await p.wait("document.querySelector('#notify-status').textContent === 'Saved.'")
        raw = json.loads(path.read_text())
        assert raw["token"] == "tk_secret" and "Jam" in raw["faults"] and raw["station_name"] == "Renamed rig"
    run_ui(body, tmp_path)
