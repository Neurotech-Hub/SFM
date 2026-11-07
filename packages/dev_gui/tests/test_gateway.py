"""
The web gateway against a real engine service: what it forwards, what it
refuses, and what happens when either side goes away.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import ClientSession, web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from base_station.gateway import REQUEST_HEADER, create_app  # noqa: E402
from base_station.host import EngineHost  # noqa: E402
from base_station.service import EngineService  # noqa: E402

from tests.test_service import make_engine  # noqa: E402

CMD = {REQUEST_HEADER: "1"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Rig:
    """An engine service plus a gateway in front of it, torn down together."""

    def __init__(self, tmp_path) -> None:
        self.tmp_path = tmp_path
        self.port = free_port()
        self.engine = make_engine(tmp_path)
        self.host = EngineHost(self.engine, tick_s=0.002, snapshot_hz=50)
        self.service = None
        self.client = None

    async def start_engine(self) -> None:
        self.host.start() if not self.host.running else None
        self.service = EngineService(self.host, ("tcp", ("127.0.0.1", self.port)))
        await self.service.start()

    async def stop_engine_service(self) -> None:
        await self.service.stop()

    async def __aenter__(self) -> "Rig":
        await self.start_engine()
        app = create_app(("tcp", ("127.0.0.1", self.port)))
        self.client = TestClient(TestServer(app, host="127.0.0.1"))
        await self.client.start_server()
        await self.wait_engine(True)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.close()
        await self.service.stop()
        self.host.stop()
        self.engine._log.close()

    async def wait_engine(self, up: bool, timeout: float = 8.0) -> dict:
        end = asyncio.get_running_loop().time() + timeout
        while True:
            body = await (await self.client.get("/api/state")).json()
            if body["engine"] is up and (not up or body["state"]):
                return body
            if asyncio.get_running_loop().time() > end:
                raise AssertionError(f"engine never became up={up}: {body}")
            await asyncio.sleep(0.05)

    async def cmd(self, name: str, status: int = None, **args) -> dict:
        resp = await self.client.post(f"/api/cmd/{name}", json=args, headers=CMD)
        if status is not None:
            assert resp.status == status, await resp.text()
        return await resp.json()


def run(fn, tmp_path):
    async def main():
        async with Rig(tmp_path) as rig:
            return await fn(rig)
    return asyncio.run(main())


# ---------------------------------------------------------------------------
# Commands and state
# ---------------------------------------------------------------------------

def test_state_and_a_command_round_trip(tmp_path) -> None:
    async def body(rig):
        state = (await rig.wait_engine(True))["state"]
        assert [n["id"] for n in state["nodes"]] == [1, 2]
        assert (await rig.cmd("dispense", 200, node=1)) == {"ok": True}
        assert rig.engine._can.commands
    run(body, tmp_path)


def test_operator_errors_are_422_with_a_message(tmp_path) -> None:
    async def body(rig):
        out = await rig.cmd("dispense", 422, node=9)
        assert out["ok"] is False and "not in this setup" in out["error"]
        assert (await rig.cmd("no_such_command", 422))["error"].startswith("Unknown command")
    run(body, tmp_path)


def test_a_command_with_no_body_runs_with_no_arguments(tmp_path) -> None:
    async def body(rig):
        resp = await rig.client.post("/api/cmd/get_dev_settings", headers=CMD)
        assert resp.status == 200 and (await resp.json())["ok"] is True
    run(body, tmp_path)


def test_malformed_requests_are_rejected_before_the_engine(tmp_path) -> None:
    async def body(rig):
        c = rig.client
        assert (await c.post("/api/cmd/Bad-Name", json={}, headers=CMD)).status == 400
        assert (await c.post("/api/cmd/ping", data=b"not json", headers={**CMD, "Content-Type": "application/json"})).status == 400
        assert (await c.post("/api/cmd/ping", json=[1, 2], headers=CMD)).status == 400
        assert (await c.post("/api/cmd/ping", data=b"x=1", headers={**CMD, "Content-Type": "text/plain"})).status == 415
        big = json.dumps({"x": "a" * (300 * 1024)})
        assert (await c.post("/api/cmd/ping", data=big, headers={**CMD, "Content-Type": "application/json"})).status == 413
        assert rig.engine._can.commands == []
    run(body, tmp_path)


def test_a_session_runs_through_the_gateway(tmp_path) -> None:
    async def body(rig):
        out = await rig.cmd("start_session", 200, experiment="free_feeding", session_name="via web")
        assert out["name"] == "via_web"
        state = (await (await rig.client.get("/api/state")).json())["state"]
        for _ in range(60):
            if state["session"]["running"]:
                break
            await asyncio.sleep(0.05)
            state = (await (await rig.client.get("/api/state")).json())["state"]
        assert state["session"]["running"] is True
        assert (await rig.cmd("calibrate_presence", 422, cage_empty=True))["ok"] is False
        await rig.cmd("stop_session", 200)
    run(body, tmp_path)


# ---------------------------------------------------------------------------
# Who may talk to it
# ---------------------------------------------------------------------------

def test_a_foreign_host_name_is_refused(tmp_path) -> None:
    async def body(rig):
        for host in ("evil.example", "evil.example:8080", "127.0.0.1.evil.example"):
            r = await rig.client.get("/api/state", headers={"Host": host})
            assert r.status == 403, host
            r = await rig.client.post("/api/cmd/ping", json={"node": 1}, headers={**CMD, "Host": host})
            assert r.status == 403, host
        assert rig.engine._can.commands == []
    run(body, tmp_path)


def test_a_cross_origin_page_cannot_command_or_read(tmp_path) -> None:
    async def body(rig):
        for origin in ("http://evil.example", "http://127.0.0.1:1", "https://127.0.0.1:%d" % rig.client.port, "null"):
            r = await rig.client.post("/api/cmd/dispense", json={"node": 1}, headers={**CMD, "Origin": origin})
            assert r.status == 403, origin
            r = await rig.client.get("/api/state", headers={"Origin": origin})
            assert r.status == 403, origin
        assert rig.engine._can.commands == []
        same = f"http://127.0.0.1:{rig.client.port}"
        r = await rig.client.post("/api/cmd/dispense", json={"node": 1}, headers={**CMD, "Origin": same})
        assert r.status == 200
    run(body, tmp_path)


def test_a_post_without_the_custom_header_is_refused(tmp_path) -> None:
    async def body(rig):
        r = await rig.client.post("/api/cmd/dispense", json={"node": 1})
        assert r.status == 403
        r = await rig.client.post("/api/cmd/dispense", json={"node": 1}, headers={REQUEST_HEADER: "0"})
        assert r.status == 403
        assert rig.engine._can.commands == []
    run(body, tmp_path)


def test_a_cross_origin_websocket_is_refused(tmp_path) -> None:
    async def body(rig):
        with pytest.raises(aiohttp.WSServerHandshakeError) as info:
            await rig.client.ws_connect("/ws", origin="http://evil.example")
        assert info.value.status == 403
        ws = await rig.client.ws_connect("/ws", origin=f"http://127.0.0.1:{rig.client.port}")
        assert json.loads((await ws.receive(timeout=3)).data)["type"] == "state"
        await ws.close()
    run(body, tmp_path)


def test_responses_carry_security_headers(tmp_path) -> None:
    async def body(rig):
        r = await rig.client.get("/")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["Cache-Control"] == "no-store"
    run(body, tmp_path)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

async def next_state(ws, predicate=lambda s: True, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while True:
        msg = await ws.receive(timeout=max(0.05, end - asyncio.get_running_loop().time()))
        data = json.loads(msg.data)
        if data["type"] == "state" and predicate(data):
            return data


def test_websocket_pushes_state_and_changes(tmp_path) -> None:
    async def body(rig):
        ws = await rig.client.ws_connect("/ws")
        first = await next_state(ws, lambda d: d["engine"] and d["state"])
        assert first["state"]["connected"] is True
        await rig.cmd("set_label", 200, node=1, label="front")
        await next_state(ws, lambda d: d["state"] and d["state"]["nodes"][0]["label"] == "front")
        await ws.close()
    run(body, tmp_path)


def test_two_browsers_both_get_updates_and_one_leaving_changes_nothing(tmp_path) -> None:
    async def body(rig):
        a = await rig.client.ws_connect("/ws")
        b = await rig.client.ws_connect("/ws")
        await next_state(a, lambda d: d["state"])
        await next_state(b, lambda d: d["state"])
        await a.close()
        await rig.cmd("set_label", 200, node=2, label="back")
        await next_state(b, lambda d: d["state"] and d["state"]["nodes"][1]["label"] == "back")
        await b.close()
    run(body, tmp_path)


def test_engine_restart_is_reported_and_the_link_recovers(tmp_path) -> None:
    async def body(rig):
        ws = await rig.client.ws_connect("/ws")
        await next_state(ws, lambda d: d["engine"] and d["state"])

        await rig.stop_engine_service()
        down = await next_state(ws, lambda d: d["engine"] is False)
        assert down["state"] is None
        r = await rig.client.post("/api/cmd/dispense", json={"node": 1}, headers=CMD)
        assert r.status == 503 and "not running" in (await r.json())["error"]

        await rig.start_engine()   # the same engine, listening again
        up = await next_state(ws, lambda d: d["engine"] and d["state"], timeout=10)
        assert up["state"]["connected"] is True
        assert (await rig.cmd("dispense", 200, node=1))["ok"]
        await ws.close()
    run(body, tmp_path)


def test_a_pending_command_is_failed_not_hung_when_the_engine_drops(tmp_path) -> None:
    async def body(rig):
        # Occupy the engine thread so the next command stays pending.
        blocker = rig.host.submit(lambda e: __import__("time").sleep(1.0))
        task = asyncio.create_task(rig.client.post("/api/cmd/get_dev_settings", headers=CMD))
        await asyncio.sleep(0.2)
        await rig.stop_engine_service()
        resp = await asyncio.wait_for(task, 5)
        assert resp.status == 503
        blocker.result(3)
    run(body, tmp_path)


def test_the_session_survives_the_gateway_going_away(tmp_path) -> None:
    async def body(rig):
        await rig.cmd("start_session", 200, experiment="free_feeding", session_name="gw_gone")
        await rig.client.close()                 # the whole gateway stops
        await asyncio.sleep(0.3)
        assert rig.engine._exp.is_running
        # A new gateway sees the session still running.
        app = create_app(("tcp", ("127.0.0.1", rig.port)))
        rig.client = TestClient(TestServer(app, host="127.0.0.1"))
        await rig.client.start_server()
        state = (await rig.wait_engine(True))["state"]
        assert state["session"]["running"] is True and state["session"]["name"] == "gw_gone"
        await rig.cmd("stop_session", 200)
    run(body, tmp_path)
