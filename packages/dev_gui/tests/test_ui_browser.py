"""
The Rig Map page, driven in headless Chromium against a real engine, the real
gateway and a fake CAN bus. Skipped when there is no Chromium or no aiohttp.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

pytest.importorskip("aiohttp")
from tests.browser import CHROME  # noqa: E402

pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chromium on this machine")

from base_station.protocol import CanCmd  # noqa: E402
from tests.ui_rig import run_ui  # noqa: E402

RUN_HEAD = ".runhead"


async def go_run(rig, p):
    await rig.bring_online()
    await rig.open()
    await p.click_step("Run")
    await p.wait("!document.querySelector('#view-run').hidden")


async def fill_session(p, name="ui session"):
    await p.click_step("Experiment")
    await p.wait("!document.querySelector('#view-experiment').hidden")
    await p.fill("#session-name", name)
    await p.wait(f"document.querySelector('#session-name').closest('section').textContent.includes('Saves')")


def commands_to(rig, node, cmd):
    return [c for c in rig.engine._can.commands if c[0] == node and c[1] == cmd]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_page_loads_cleanly_and_asks_only_this_machine(tmp_path) -> None:
    async def body(rig, p):
        await rig.open()
        await asyncio.sleep(0.5)
        assert p.errors == [], p.errors
        # No CDN: every request goes to this gateway (or is an inline data: URL).
        origin = f"http://127.0.0.1:{rig.client.port}"
        outside = [u for u in p.requests if not (u.startswith(origin) or u.startswith("data:") or u.startswith("ws://127.0.0.1") or u == "about:blank")]
        assert outside == [], outside
        assert any(u.endswith(".woff2") for u in p.requests), "fonts are served from this machine"
    run_ui(body, tmp_path)


def test_a_connected_engine_opens_on_the_experiment_step(tmp_path) -> None:
    async def body(rig, p):
        await rig.open()
        assert await p.eval("document.querySelector('.step[aria-current=step]').textContent.includes('Experiment')")
    run_ui(body, tmp_path)


def test_the_steps_after_connect_are_locked_until_connected(tmp_path) -> None:
    async def body(rig, p):
        # Take the bus away: an engine that was never connected.
        await rig.on_engine(lambda e: (setattr(e, "_can", None), setattr(e, "_registry", None)))
        await rig.open(wait_step=False)
        await p.wait("document.querySelectorAll('.step').length === 3")
        await p.wait("document.querySelector('.step[aria-current=step]').textContent.includes('Connect')")
        assert await p.eval("[...document.querySelectorAll('.step')].map(b => b.disabled).join()") == "false,true,true"
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# Step 2: forms
# ---------------------------------------------------------------------------

def test_every_template_renders_a_form_from_its_json(tmp_path) -> None:
    async def body(rig, p):
        await rig.open()
        names = await p.eval("[...document.querySelectorAll('[aria-labelledby=exp-h] .opt strong')].map(e => e.textContent)")
        assert {"Free Feeding", "Fixed + Random", "Probability Delivery", "Troubleshooting", "Two-Armed Bandit"} <= set(names)
        for name in names:
            await rig.choose(name)
            await p.wait(f"document.querySelector('#view-experiment section:nth-of-type(2) h2').textContent === {json.dumps(name)}")
            n = await p.eval("document.querySelectorAll('#view-experiment section:nth-of-type(2) input, #view-experiment section:nth-of-type(2) .seg button').length")
            assert n >= 2, f"{name} built no controls"
        assert p.errors == []
    run_ui(body, tmp_path)


def test_a_parameter_appears_only_when_its_condition_holds(tmp_path) -> None:
    async def body(rig, p):
        await rig.open()
        await rig.choose("Free Feeding")
        await p.wait("document.querySelector('#f-next_trial_wait') !== null || document.body.textContent.includes('Advance to next trial')")
        vis = "!!(document.querySelector('#f-%s') && !document.querySelector('#f-%s').closest('[hidden]'))"
        await p.click_text("fixed_delay", "#view-experiment")
        await p.wait(vis % ("fixed_delay_s", "fixed_delay_s"))
        assert not await p.eval(vis % ("iti_quiet_s", "iti_quiet_s"))
        await p.click_text("presence_clear", "#view-experiment")
        await p.wait(vis % ("iti_quiet_s", "iti_quiet_s"))
        assert not await p.eval(vis % ("fixed_delay_s", "fixed_delay_s"))
    run_ui(body, tmp_path)


def test_a_bad_number_is_flagged_and_blocks_next(tmp_path) -> None:
    async def body(rig, p):
        await rig.open()
        await rig.choose("Free Feeding")
        await p.fill("#session-name", "numbers")
        await p.click_text("fixed_delay", "#view-experiment")
        await p.wait("document.querySelector('#f-fixed_delay_s')")
        await p.fill("#f-fixed_delay_s", "99999")
        await p.wait("document.querySelector('#f-fixed_delay_s').getAttribute('aria-invalid') === 'true'")
        assert "At most" in await p.eval("document.querySelector('#f-fixed_delay_s').closest('.col').textContent")
        assert await p.eval("[...document.querySelectorAll('#view-experiment button')].find(b => b.textContent === 'Next').disabled")
        await p.fill("#f-fixed_delay_s", "20")
        await p.wait("!([...document.querySelectorAll('#view-experiment button')].find(b => b.textContent === 'Next').disabled)")
    run_ui(body, tmp_path)


def test_the_session_name_previews_its_file_and_a_resume(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.open()
        await p.fill("#session-name", "my session!")
        await p.wait("document.body.textContent.includes('my_session.csv')")
        assert "will be created" not in await p.text("#view-experiment"), "a new name shows no resume notice"
        # Run it once so the file exists, then the same name resumes as run 2.
        await p.click_text("Next", "#view-experiment")
        await p.wait("!document.querySelector('#view-run').hidden")
        await p.click_text("Start session", RUN_HEAD)
        await p.wait("document.querySelector('.runhead .btn.danger') !== null")
        await p.click_text("Stop session", RUN_HEAD)
        await p.wait("document.querySelector('dialog[open]')")
        await p.click_text("Stop session", "dialog")
        await p.wait("document.querySelector('.runhead .btn.primary') !== null")
        await p.click_step("Experiment")
        await p.fill("#session-name", "my session!!")
        await p.wait("document.body.textContent.includes('already exists')", 8)
        assert "run 2" in await p.text("#view-experiment")
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# Step 3: running
# ---------------------------------------------------------------------------

def test_start_and_stop_a_session_from_the_page(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.open()
        await fill_session(p, "from page")
        await p.click_text("Next", "#view-experiment")
        await p.wait("!document.querySelector('#view-run').hidden")
        await p.click_text("Start session", RUN_HEAD)
        await p.wait("document.querySelector('.runhead .btn.danger') !== null")
        assert rig.engine._exp.is_running and rig.engine._log.session_name == "from_page"
        assert await p.text(".runhead .title") == "from_page"
        await p.click_text("Stop session", RUN_HEAD)
        await p.wait("document.querySelector('dialog[open]')")
        await p.click_text("Cancel", "dialog")
        await asyncio.sleep(0.3)
        assert rig.engine._exp.is_running, "Cancel must not stop it"
        await p.click_text("Stop session", RUN_HEAD)
        await p.wait("document.querySelector('dialog[open]')")
        await p.click_text("Stop session", "dialog")
        await p.wait("!document.querySelector('.runhead .btn.danger')")
        assert not rig.engine._exp.is_running
        assert p.errors == []
    run_ui(body, tmp_path)


def test_going_back_from_a_running_session_asks_first(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.open()
        await fill_session(p, "going back")
        await p.click_text("Next", "#view-experiment")
        await p.click_text("Start session", RUN_HEAD)
        await p.wait("document.querySelector('.runhead .btn.danger') !== null")
        await p.click_step("Experiment")
        await p.wait("document.querySelector('dialog[open]')")
        assert "stops the session" in await p.text("dialog")
        await p.click_text("Cancel", "dialog")
        await asyncio.sleep(0.3)
        assert rig.engine._exp.is_running
        assert await p.eval("document.querySelector('.step[aria-current=step]').textContent.includes('Run')")
        await p.click_step("Experiment")
        await p.wait("document.querySelector('dialog[open]')")
        await p.click_text("Stop session and go back", "dialog")
        await p.wait("document.querySelector('.step[aria-current=step]').textContent.includes('Experiment')")
        assert not rig.engine._exp.is_running
    run_ui(body, tmp_path)


def test_a_reloaded_page_returns_to_the_running_session(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.open()
        await fill_session(p, "survives reload")
        await p.click_text("Next", "#view-experiment")
        await p.click_text("Start session", RUN_HEAD)
        await p.wait("document.querySelector('.runhead .btn.danger') !== null")
        await rig.open(wait_step=False)
        await p.wait("document.querySelector('.step[aria-current=step]') && document.querySelector('.step[aria-current=step]').textContent.includes('Run')")
        assert await p.text(".runhead .title") == "survives_reload"
        assert "Stop session" in await p.text(RUN_HEAD)
    run_ui(body, tmp_path)


def test_a_failed_start_says_why_and_nothing_runs(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.open()
        await rig.choose("Free Feeding")
        await p.fill("#session-name", "no nodes")
        await p.click_text("None", "#view-experiment")                 # untick every node
        await p.click_text("Next", "#view-experiment")
        await p.wait("!document.querySelector('#view-run').hidden")
        await p.click_text("Start session", RUN_HEAD)
        await p.wait("!document.querySelector('#start-err').closest('.banner').hidden")
        assert "at least one node" in (await p.text("#start-err")).lower()
        assert not rig.engine._exp.is_running
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# Node panel and broadcast actions
# ---------------------------------------------------------------------------

async def select_node(p, n):
    await p.eval(f"[...document.querySelectorAll('.node')].find(b => b.getAttribute('aria-label').startsWith('Node {n}')).click()")
    await p.wait(f"document.querySelector('.runside h2').textContent.includes('{n}')")


def test_node_actions_send_the_right_commands(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await select_node(p, 2)
        await p.click_text("Dispense", ".runside")
        await p.click_text("Ping", ".runside")
        await asyncio.sleep(0.4)
        assert commands_to(rig, 2, CanCmd.Dispense) and commands_to(rig, 2, CanCmd.Ping)
        assert not commands_to(rig, 1, CanCmd.Dispense)
        await p.click_text("Dispense all", RUN_HEAD)
        await p.click_text("Ping all", RUN_HEAD)
        await asyncio.sleep(0.4)
        assert (CanCmd.Dispense, b"") in rig.engine._can.broadcasts and (CanCmd.Ping, b"") in rig.engine._can.broadcasts
        assert p.errors == []
    run_ui(body, tmp_path)


def test_a_node_can_be_relabelled_and_the_map_follows(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await select_node(p, 1)
        await p.fill("#node-label", "Left feeder")
        await p.wait("[...document.querySelectorAll('.node .name')].some(e => e.textContent === 'Left feeder')")
        assert rig.engine._registry.get(1).label == "Left feeder"
    run_ui(body, tmp_path)


def test_an_offline_node_cannot_be_told_to_dispense_or_recover(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await rig.set_node(2, online=False)
        await select_node(p, 2)
        await p.wait("[...document.querySelectorAll('.runside button')].find(b => b.textContent === 'Dispense').disabled")
        assert await p.eval("[...document.querySelectorAll('.runside button')].find(b => b.textContent === 'Recover').disabled")
        assert "Offline" in await p.text(".runside")
    run_ui(body, tmp_path)


def test_a_faulted_node_raises_a_banner_with_recover(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await rig.fault(1)
        await p.wait("document.querySelector('#fault-box .banner') !== null")
        banner = await p.text("#fault-box .banner")
        assert "pellet lost" in banner and "Recover Node 1" in banner
        await p.click_text("Recover Node 1", "#view-run")
        await asyncio.sleep(0.5)
        assert commands_to(rig, 1, CanCmd.Recover)
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# Arranging nodes
# ---------------------------------------------------------------------------

def test_dragging_a_node_saves_its_position_by_mac_and_it_survives_a_reload(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await p.click_text("Arrange nodes", RUN_HEAD)
        before = await p.box(".node")
        await p.drag(".node", 140, 70)
        mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 1])
        for _ in range(40):
            if rig.engine._layout.get(mac):
                break
            await asyncio.sleep(0.05)
        saved = rig.engine._layout.get(mac)
        assert saved and 0 < saved["x"] < 1 and 0 < saved["y"] < 1
        after = await p.box(".node")
        assert after["x"] > before["x"] + 100 and after["y"] > before["y"] + 40
        # The file on disk is keyed by MAC, as fractions.
        data = json.loads((tmp_path / "layout.json").read_text())
        assert list(data["positions"]) == ["AA:BB:CC:DD:EE:01"]
        # A reload puts it back in the same place.
        await rig.open()
        await p.click_step("Run")
        await p.wait("document.querySelectorAll('.node').length === 2")
        await asyncio.sleep(0.5)
        again = await p.box(".node")
        assert abs(again["x"] - after["x"]) < 2 and abs(again["y"] - after["y"]) < 2
    run_ui(body, tmp_path)


def test_a_node_without_a_mac_is_not_saved_and_the_operator_is_told(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online(macs=False)
        await rig.open()
        await p.click_step("Run")
        await p.wait("!document.querySelector('#view-run').hidden")
        await p.click_text("Arrange nodes", RUN_HEAD)
        await p.drag(".node", 80, 40)
        await p.wait("document.querySelector('.toast') !== null")
        assert "MAC" in await p.text(".toasts")
        assert rig.engine._layout.all() == {}
    run_ui(body, tmp_path)


def test_auto_arrange_forgets_saved_positions(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await rig.on_engine(lambda e: e._layout.set(bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 1]), 0.9, 0.9))
        await p.click_text("Arrange nodes", RUN_HEAD)
        await p.click_text("Auto-arrange", RUN_HEAD)
        await p.wait("document.querySelector('dialog[open]')")
        await p.click_text("Auto-arrange", "dialog")
        for _ in range(40):
            if not rig.engine._layout.all():
                break
            await asyncio.sleep(0.05)
        assert rig.engine._layout.all() == {}
    run_ui(body, tmp_path)


def test_twelve_nodes_fit_on_the_map_without_covering_each_other(tmp_path) -> None:
    async def body(rig, p):
        await p.viewport(1024, 768)
        await go_run(rig, p)
        await p.wait("document.querySelectorAll('.node').length === 12")
        await asyncio.sleep(0.4)
        boxes = await p.eval("""(() => {
            const map = document.querySelector('.map').getBoundingClientRect();
            return [map.x, map.y, map.width, map.height, ...[...document.querySelectorAll('.node .ring')].map(e => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })];
        })()""")
        mx, my, mw, mh, rings = boxes[0], boxes[1], boxes[2], boxes[3], boxes[4:]
        for x, y, w, h in rings:
            assert mx <= x and x + w <= mx + mw and my <= y and y + h <= my + mh, "a ring sits outside the map"
        # Whole nodes (ring, name, state and role) must not overlap, or labels hide behind other rings.
        blocks = await p.eval("[...document.querySelectorAll('.node')].map(e => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })")
        for i, (ax, ay, aw, ah) in enumerate(blocks):
            for bx, by, bw, bh in blocks[i + 1:]:
                apart = ax + aw <= bx + 1 or bx + bw <= ax + 1 or ay + ah <= by + 1 or by + bh <= ay + 1
                assert apart, "two nodes overlap"
        # Text stays readable however many nodes there are.
        sizes = await p.eval("[...document.querySelectorAll('.node .name, .node .state')].map(e => parseFloat(getComputedStyle(e).fontSize))")
        assert min(sizes) >= 12
    run_ui(body, tmp_path, nodes=12)


# ---------------------------------------------------------------------------
# I/O tab: BNC
# ---------------------------------------------------------------------------

async def open_io(p):
    await p.eval("[...document.querySelectorAll('.tab')].find(t => t.textContent === 'I/O').click()")
    await p.wait("document.querySelector('[aria-label=\"BNC input 0\"]') && !document.querySelector('[aria-label=\"BNC input 0\"]').closest('[hidden]')")


def test_bnc_input_settings_reach_the_engine(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await open_io(p)
        sec = '[aria-label="BNC input 0"]'
        await p.eval(f"(() => {{ const c = document.querySelector('{sec} input[type=checkbox]'); c.checked = true; c.dispatchEvent(new Event('change', {{bubbles: true}})); }})()")
        await p.fill(f'{sec} select[aria-label="Mode"]', "pulse")
        await p.wait("document.querySelector('" + sec + " input[aria-label^=\"Minimum width\"]') && !document.querySelector('" + sec + " input[aria-label^=\"Minimum width\"]').closest('[hidden]')")
        await p.fill(f'{sec} input[aria-label^="Minimum width"]', "10")
        await p.fill(f'{sec} select[aria-label="Action when active"]', "dispense_all")
        await asyncio.sleep(0.6)
        cfg = rig.engine._bnc_in1_cfg
        assert (cfg.enabled, cfg.mode, cfg.min_width_ms, cfg.action) == (True, "pulse", 10.0, "dispense_all")
        assert p.errors == []
    run_ui(body, tmp_path)


def test_a_refused_bnc_setting_shows_the_engines_reason(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await open_io(p)
        sec = '[aria-label="BNC input 0"]'
        await p.fill(f'{sec} select[aria-label="Mode"]', "pulse")
        await p.wait("document.querySelector('" + sec + " input[aria-label^=\"Minimum width\"]') && !document.querySelector('" + sec + " input[aria-label^=\"Minimum width\"]').closest('[hidden]')")
        await p.fill(f'{sec} input[aria-label^="Minimum width"]', "0")
        await p.wait("document.querySelector('" + sec + " .err-text').textContent.length > 0")
        assert "between 0 and 60000" in await p.text(f"{sec} .err-text")
    run_ui(body, tmp_path)


def test_bnc_output_mapping_is_applied_and_locked_while_running(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await open_io(p)
        out = '[aria-label="BNC output"]'
        await p.fill(f'{out} select[aria-label="BNC OUT signals"]', "session_start_stop")
        await p.wait(f"!document.querySelector('{out} input[aria-label=\"Pulse width in ms\"]').closest('[hidden]')")
        await p.fill(f'{out} input[aria-label="Pulse width in ms"]', "5")
        await p.click_text("Apply", out)
        for _ in range(40):
            if rig.engine._bnc_out_cfg.mapping == "session_start_stop":
                break
            await asyncio.sleep(0.05)
        assert rig.engine._bnc_out_cfg.mapping == "session_start_stop" and rig.engine._bnc_out_cfg.pulse_width_us == 5000
        # While a session runs the BNC settings are locked, in the page and in the engine.
        await fill_session(p, "bnc lock")
        await p.click_step("Run")
        await p.click_text("Start session", RUN_HEAD)
        await p.wait("document.querySelector('.runhead .btn.danger') !== null")
        await open_io(p)
        await p.wait(f"document.querySelector('{out} select').disabled")
        assert await p.eval("document.querySelector('[aria-label=\"BNC input 0\"] select').disabled")
        assert "locked while a session runs" in await p.text(".runside")
    run_ui(body, tmp_path)


def test_a_bnc_start_needs_arming_by_hand_and_the_page_shows_it(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.on_engine(lambda e: e.set_bnc_input("IN1", enabled=True, mode="pulse", action="start_experiment", min_width_ms=5))
        await rig.open()
        await fill_session(p, "armed run")
        await p.click_step("Run")
        await p.wait("!document.querySelector('#view-run').hidden")
        assert await p.eval("!document.querySelector('#view-run .banner.info') || document.querySelector('#view-run .banner.info').hidden")
        await p.click_text("Arm start", RUN_HEAD)
        await p.wait("!document.querySelector('#view-run .banner.info').hidden")
        assert rig.engine.start_armed and "Armed" in await p.text("#view-run .banner.info")
        await p.click_text("Disarm", "#view-run .banner.info")
        await p.wait("document.querySelector('#view-run .banner.info').hidden")
        assert not rig.engine.start_armed
    run_ui(body, tmp_path)


def test_arming_without_a_start_input_says_why(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.open()
        await fill_session(p, "no input")
        await p.click_step("Run")
        await p.click_text("Arm start", RUN_HEAD)
        await p.wait("!document.querySelector('#start-err').closest('.banner').hidden")
        assert "BNC input" in await p.text("#start-err")
        assert not rig.engine.start_armed
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# Developer tab
# ---------------------------------------------------------------------------

async def open_dev(p):
    await p.click_text("Developer", RUN_HEAD)
    await p.wait("document.querySelector('#dev-hb') && !document.querySelector('#dev-hb').closest('[hidden]')")
    # The current values arrive a moment after the tab opens.
    await p.wait("document.querySelector('#dev-hb').value !== '' && document.querySelector('#dev-factor').value !== ''")


def test_heartbeat_uses_the_real_range_and_has_no_off_switch(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await open_dev(p)
        text = await p.text(".runside")
        assert "turned off" in text and "3 missed beats" in text
        assert not await p.eval("[...document.querySelectorAll('.runside button, .runside [role=switch]')].some(b => /\\b(on|off)\\b/i.test(b.textContent.trim()) && b.textContent.trim().length < 5)")
        await p.fill("#dev-hb", "100")
        await p.click_text("Apply heartbeat to all nodes", ".runside")
        await p.wait("document.querySelector('.toast.error') !== null")
        assert "65.535" in await p.text(".toasts")
        assert rig.engine._hb_interval_s == 60.0
        await p.fill("#dev-hb", "2")
        await p.click_text("Apply heartbeat to all nodes", ".runside")
        for _ in range(40):
            if rig.engine._hb_interval_s == 2.0:
                break
            await asyncio.sleep(0.05)
        assert rig.engine._hb_interval_s == 2.0
        assert "6 s" in await p.text(".runside"), "offline after 3 missed beats = 6 s"
    run_ui(body, tmp_path)


def test_presence_factor_is_range_checked_and_sent_to_every_node(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await open_dev(p)
        await p.fill("#dev-factor", "500")
        await p.click_text("Apply to all nodes", ".runside")
        await p.wait("document.querySelector('.toast.error') !== null")
        assert "0.1 to 100" in await p.text(".toasts")
        await p.fill("#dev-factor", "2.5")
        await p.click_text("Apply to all nodes", ".runside")
        await asyncio.sleep(0.5)
        assert any(c == CanCmd.SetConfig and p_[0] == 0x02 for c, p_ in rig.engine._can.broadcasts)
    run_ui(body, tmp_path)


def test_assign_id_needs_a_known_mac(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.set_node(2, mac=None)
        await rig.open()
        await p.click_step("Run")
        await p.wait("!document.querySelector('#view-run').hidden")
        await open_dev(p)
        await p.wait("document.querySelectorAll('.runside input[aria-label^=\"New ID\"]').length === 2")
        sends = "[...document.querySelectorAll('.runside button')].filter(b => b.textContent === 'Send').map(b => b.disabled)"
        assert await p.eval(sends) == [False, True]
        await p.fill('.runside input[aria-label="New ID for node 1"]', "7")
        await p.click_text("Send", ".runside")
        await asyncio.sleep(0.5)
        assert rig.engine._can.commands[-1][0] == "assign"
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# Calibration is optional
# ---------------------------------------------------------------------------

def test_a_session_starts_without_calibrating_and_calibration_asks_first(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.open()
        await fill_session(p, "no calibration")
        await p.click_step("Run")
        await p.wait("!document.querySelector('#view-run').hidden")
        banner = "#view-run .banner.warn"
        assert await p.eval(f"document.querySelector('{banner}').hidden"), "no calibration block is shown by default"
        await p.click_text("Calibrate presence", RUN_HEAD)
        await p.wait(f"!document.querySelector('{banner}').hidden")
        assert "empty" in await p.text(banner)
        await asyncio.sleep(0.3)
        assert (CanCmd.CalibratePresence, b"") not in rig.engine._can.broadcasts, "nothing is sent before confirming"
        await p.click_text("Cage is empty, calibrate", banner)
        await asyncio.sleep(0.5)
        assert (CanCmd.CalibratePresence, b"") in rig.engine._can.broadcasts
        assert "Calibrating" in await p.text(banner)
    run_ui(body, tmp_path)


def test_calibration_is_unavailable_while_a_session_runs(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.open()
        await fill_session(p, "cal locked")
        await p.click_step("Run")
        await p.click_text("Start session", RUN_HEAD)
        await p.wait("document.querySelector('.runhead .btn.danger') !== null")
        assert await p.eval("[...document.querySelectorAll('.runhead button')].find(b => b.textContent.includes('Calibrate presence')).disabled")
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

def test_the_event_log_shows_filters_clears_and_exports(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await select_node(p, 1)
        await p.click_text("Dispense", ".runside")
        await p.click_text("Ping", ".runside")
        await p.eval("document.querySelector('details.logbox').open = true")
        await p.wait("document.querySelectorAll('table.log tbody tr').length >= 2")
        rows = await p.eval("[...document.querySelectorAll('table.log tbody tr')].map(r => r.children[4].textContent)")
        assert "Dispense" in rows and "Ping" in rows
        # Newest first.
        assert rows[0] == "Ping"
        await p.fill('select[aria-label="Show event type"]', "EVENT")
        await p.wait("document.querySelectorAll('table.log tbody tr').length === 0")
        await p.fill('select[aria-label="Show event type"]', "all")
        await p.wait("document.querySelectorAll('table.log tbody tr').length >= 2")
        await p.click_text("Export", "details.logbox")
        await p.wait("document.querySelector('.toast') !== null")
        exported = [f for f in os.listdir(tmp_path) if "_export_" in f]
        assert exported, "an export file with _export_ in its name was written"
        await p.click_text("Clear", "details.logbox")
        await p.wait("document.querySelectorAll('table.log tbody tr').length === 0")
        assert rig.engine._log.all_entries() == []
    run_ui(body, tmp_path)


def test_a_burst_of_rows_is_windowed_not_drawn_in_full(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await p.eval("document.querySelector('details.logbox').open = true")
        from base_station.log_manager import LogEntry
        import time as _t

        def flood(engine):
            for i in range(700):
                engine._log.add(LogEntry(timestamp=_t.time(), direction="RX", node_id=1, frame_type="EVENT",
                                         event_name=f"burst {i}", raw_id=1, raw_data=b""))
        await rig.on_engine(flood)
        await p.wait("document.querySelector('table.log tbody tr td:nth-child(5)') && document.querySelector('table.log tbody tr td:nth-child(5)').textContent === 'burst 699'", 10)
        n = await p.eval("document.querySelectorAll('table.log tbody tr').length")
        assert n <= 200, f"{n} rows drawn"
        assert "of" in await p.text(".logtools")
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# The engine going away and coming back
# ---------------------------------------------------------------------------

def test_the_page_says_when_the_engine_is_down_and_recovers_by_itself(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await rig.stop_engine_service()
        await p.wait("!document.querySelector('.banner.err[role=alert]').hidden", 10)
        assert "Engine not running" in await p.text(".banner.err[role=alert]")
        await rig.start_engine()
        await p.wait("document.querySelector('.banner.err[role=alert]').hidden", 15)
        assert await p.eval("document.querySelectorAll('.node').length") == 2
    run_ui(body, tmp_path)


def test_an_engine_notice_becomes_a_toast_but_old_ones_are_not_replayed(tmp_path) -> None:
    async def body(rig, p):
        await rig.bring_online()
        await rig.on_engine(lambda e: e._notice("warning", "An old notice"))
        await rig.open()
        await asyncio.sleep(0.6)
        assert await p.eval("document.querySelectorAll('.toast').length") == 0, "a notice from before the page opened is not replayed"
        await rig.on_engine(lambda e: e._notice("warning", "Something to look at"))
        await p.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent === 'Something to look at')")
        await rig.on_engine(lambda e: e._notice("warning", "Something to look at"))     # the same text again
        await p.wait("document.querySelectorAll('.toast').length === 2")
    run_ui(body, tmp_path)


# ---------------------------------------------------------------------------
# Things the old node tile showed
# ---------------------------------------------------------------------------

def test_the_node_panel_shows_heartbeat_age_and_the_dome_warning(tmp_path) -> None:
    async def body(rig, p):
        import time as _t
        await go_run(rig, p)
        await rig.set_node(1, last_heartbeat_time=_t.time() - 5)
        await select_node(p, 1)
        await p.wait("/Last heartbeat [0-9]+ s ago/.test(document.querySelector('#node-hb').textContent)")
        assert "muted" in await p.eval("document.querySelector('#node-hb').className")
        await rig.set_node(1, last_heartbeat_time=_t.time() - 400)      # far past 3 intervals of 60 s
        await p.wait("document.querySelector('#node-hb').className.includes('err-text')")
        await rig.set_node(1, dome_open_warning=True)
        await p.wait("document.querySelector('.runside .note').textContent.includes('30 seconds')")
    run_ui(body, tmp_path)


def test_a_bnc_input_can_be_renamed(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        await open_io(p)
        await p.fill('[aria-label="BNC input 0"] input[aria-label="Label"]', "Camera frame clock")
        for _ in range(40):
            if rig.engine._bnc_in1_cfg.label == "Camera frame clock":
                break
            await asyncio.sleep(0.05)
        assert rig.engine._bnc_in1_cfg.label == "Camera frame clock"
        await p.wait("document.querySelector('[aria-label=\"BNC input 0\"] .label').textContent === 'Camera frame clock'")
    run_ui(body, tmp_path)


def test_an_engine_that_lost_the_bus_sends_the_page_back_to_connect(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        # The engine restarted and is not connected to the bus any more.
        await rig.on_engine(lambda e: (setattr(e, "_can", None), setattr(e, "_registry", None)))
        await p.wait("document.querySelector('.step[aria-current=step]').textContent.includes('Connect')", 10)
        await p.wait("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('not connected to the bus'))")
        assert await p.eval("[...document.querySelectorAll('.step')].map(b => b.disabled).join()") == "false,true,true"
    run_ui(body, tmp_path)


def test_new_log_rows_are_added_without_rebuilding_the_ones_already_drawn(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        from base_station.log_manager import LogEntry
        import time as _t

        def add(engine, names):
            for n in names:
                engine._log.add(LogEntry(timestamp=_t.time(), direction="RX", node_id=1, frame_type="EVENT",
                                         event_name=n, raw_id=1, raw_data=b""))
        await p.eval("document.querySelector('details.logbox').open = true")
        await rig.on_engine(lambda e: add(e, ["first", "second"]))
        await p.wait("document.querySelector('table.log tbody tr td:nth-child(5)').textContent === 'second'")
        # Tag the rows that are on screen, then add more.
        await p.eval("[...document.querySelectorAll('table.log tbody tr')].forEach(tr => { tr.dataset.mine = '1'; })")
        await rig.on_engine(lambda e: add(e, ["third", "fourth"]))
        await p.wait("document.querySelector('table.log tbody tr td:nth-child(5)').textContent === 'fourth'")
        order = await p.eval("[...document.querySelectorAll('table.log tbody tr')].map(tr => tr.children[4].textContent + (tr.dataset.mine ? '*' : ''))")
        top4 = [x for x in order if x.rstrip('*') in ("first", "second", "third", "fourth")]
        assert top4 == ["fourth", "third", "second*", "first*"], f"older rows must be the same elements: {top4}"
    run_ui(body, tmp_path)


def test_the_log_window_is_trimmed_at_the_bottom_as_rows_arrive(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        from base_station.log_manager import LogEntry
        import time as _t

        def burst(engine, start, n):
            for i in range(start, start + n):
                engine._log.add(LogEntry(timestamp=_t.time(), direction="RX", node_id=1, frame_type="EVENT",
                                         event_name=f"row {i}", raw_id=1, raw_data=b""))
        await p.eval("document.querySelector('details.logbox').open = true")
        for start in (0, 150, 300):
            await rig.on_engine(lambda e, s=start: burst(e, s, 150))
            await p.wait(f"document.querySelector('table.log tbody tr td:nth-child(5)').textContent === 'row {start + 149}'", 10)
        assert await p.eval("document.querySelectorAll('table.log tbody tr').length") == 200
        newest, oldest = await p.eval("(() => { const t = [...document.querySelectorAll('table.log tbody tr')]; return [t[0].children[4].textContent, t[t.length-1].children[4].textContent]; })()")
        assert newest == "row 449" and oldest == "row 250", (newest, oldest)
    run_ui(body, tmp_path)


def test_a_late_answer_does_not_overwrite_what_the_operator_typed(tmp_path) -> None:
    async def body(rig, p):
        await go_run(rig, p)
        # Type into the heartbeat field the moment the tab opens, before the engine's values arrive.
        await p.eval("""(() => {
            const real = window.fetch;
            window.fetch = (...a) => String(a[0]).endsWith('/get_dev_settings') ? new Promise((ok) => setTimeout(() => ok(real(...a)), 800)) : real(...a);
        })()""")
        await p.click_text("Developer", RUN_HEAD)
        await p.wait("document.querySelector('#dev-hb') && !document.querySelector('#dev-hb').closest('[hidden]')")
        await p.fill("#dev-hb", "7")
        await asyncio.sleep(1.5)                       # the delayed answer has now arrived
        assert await p.eval("document.querySelector('#dev-hb').value") == "7"
    run_ui(body, tmp_path)
