"""
The engine service over a real socket: commands, pushed state, and clients
that misbehave. The engine is real; the CAN bus is a fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
from types import SimpleNamespace

import pytest

import base_station.service as service_mod
from base_station.commands import COMMANDS
from base_station.host import EngineHost
from base_station.log_manager import LogManager
from base_station.node_registry import NodeRegistry
from base_station.service import EngineService, encode

from tests.test_engine import _args, _FakeCan, _RecordingEngine


def make_engine(tmp_path):
    e = _RecordingEngine(_args())
    e._registry = NodeRegistry(2)
    e._log = LogManager(log_dir=str(tmp_path), auto_save=True)
    e._can = _FakeCan()
    e._exp_log_dir = str(tmp_path)
    return e


class Client:
    def __init__(self, reader, writer) -> None:
        self.reader, self.writer = reader, writer
        self._next = 0

    @classmethod
    async def connect(cls, service: EngineService) -> "Client":
        kind, where = service.address
        if kind == "unix":
            reader, writer = await asyncio.open_unix_connection(where)
        else:
            reader, writer = await asyncio.open_connection(*where)
        return cls(reader, writer)

    async def read(self, timeout: float = 3.0) -> dict:
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        assert line, "connection closed"
        return json.loads(line)

    async def read_until(self, predicate, timeout: float = 3.0) -> dict:
        end = asyncio.get_running_loop().time() + timeout
        while True:
            message = await self.read(max(0.05, end - asyncio.get_running_loop().time()))
            if predicate(message):
                return message

    async def call(self, cmd: str, **args) -> dict:
        self._next += 1
        rid = self._next
        self.writer.write(encode({"id": rid, "cmd": cmd, "args": args}))
        await self.writer.drain()
        return (await self.read_until(lambda m: m.get("id") == rid))["result"]

    def close(self) -> None:
        self.writer.close()


def run(coro_fn, tmp_path, address=("tcp", ("127.0.0.1", 0))):
    """Run ``coro_fn(service, engine, host)`` with a live service and host."""
    async def main():
        engine = make_engine(tmp_path)
        host = EngineHost(engine, tick_s=0.002, snapshot_hz=50)
        host.start()
        svc = EngineService(host, address)
        await svc.start()
        try:
            return await coro_fn(svc, engine, host)
        finally:
            await svc.stop()
            host.stop()
            engine._log.close()
    return asyncio.run(main())


def test_command_round_trip_and_error_reply(tmp_path) -> None:
    async def body(svc, engine, host):
        c = await Client.connect(svc)
        assert (await c.call("dispense", node=2)) == {"ok": True}
        assert (2, __import__("base_station.protocol", fromlist=["CanCmd"]).CanCmd.Dispense, b"") in engine._can.commands
        bad = await c.call("dispense", node=9)
        assert bad["ok"] is False and "not in this setup" in bad["error"]
        assert (await c.call("nope"))["error"].startswith("Unknown command")
        c.close()
    run(body, tmp_path)


def test_replies_are_matched_by_id_when_requests_overlap(tmp_path) -> None:
    async def body(svc, engine, host):
        c = await Client.connect(svc)
        for rid in (101, 102, 103):
            c.writer.write(encode({"id": rid, "cmd": "get_dev_settings"}))
        await c.writer.drain()
        got = {}
        while len(got) < 3:
            m = await c.read_until(lambda m: "id" in m)
            got[m["id"]] = m["result"]
        assert set(got) == {101, 102, 103}
        assert all(r["ok"] for r in got.values())
    run(body, tmp_path)


def test_state_is_sent_on_connect_and_again_on_change(tmp_path) -> None:
    async def body(svc, engine, host):
        c = await Client.connect(svc)
        first = (await c.read_until(lambda m: "state" in m))["state"]
        assert [n["id"] for n in first.get("nodes", [1, 2])] in ([1, 2], [])
        # A change in the engine shows up in a later push without asking.
        await c.call("set_label", node=1, label="front")
        later = (await c.read_until(
            lambda m: "state" in m and any(n["label"] == "front" for n in m["state"].get("nodes", []))
        ))["state"]
        assert later["connected"] is True
    run(body, tmp_path)


def test_bad_lines_get_an_error_and_the_connection_survives(tmp_path) -> None:
    async def body(svc, engine, host):
        c = await Client.connect(svc)
        for junk in (b"this is not json\n", b"[1,2,3]\n", b'{"id":1}\n', b'{"id":2,"cmd":"ping","args":[1]}\n'):
            c.writer.write(junk)
            await c.writer.drain()
            m = await c.read_until(lambda m: "result" in m)
            assert m["result"]["ok"] is False
        assert (await c.call("get_dev_settings"))["ok"] is True
    run(body, tmp_path)


def test_oversized_line_disconnects_only_that_client(tmp_path, monkeypatch) -> None:
    async def body(svc, engine, host):
        bad = await Client.connect(svc)
        good = await Client.connect(svc)
        bad.writer.write(b"x" * (service_mod.MAX_LINE_BYTES + 10) + b"\n")
        try:
            await bad.writer.drain()
        except ConnectionError:
            pass
        await asyncio.sleep(0.2)
        assert (await good.call("get_dev_settings"))["ok"] is True
        assert host.running
        # The bad client's connection was closed by the engine.
        data = await asyncio.wait_for(bad.reader.read(), 3)
        assert b'"state"' in data or data == b""
    run(body, tmp_path)


def test_a_command_that_crashes_is_reported_and_the_service_stays_up(tmp_path, monkeypatch) -> None:
    def explode(engine):
        raise RuntimeError("bug in a command")
    monkeypatch.setitem(COMMANDS, "explode", explode)

    async def body(svc, engine, host):
        c = await Client.connect(svc)
        out = await c.call("explode")
        assert out["ok"] is False and "failed inside the engine" in out["error"]
        assert (await c.call("get_dev_settings"))["ok"] is True
        assert host.running
    run(body, tmp_path)


def test_a_client_vanishing_does_not_disturb_the_engine_or_other_clients(tmp_path) -> None:
    async def body(svc, engine, host):
        a = await Client.connect(svc)
        b = await Client.connect(svc)
        await a.call("get_dev_settings")
        a.writer.transport.abort()  # no goodbye
        await asyncio.sleep(0.3)
        assert svc.client_count == 1
        assert (await b.call("get_dev_settings"))["ok"] is True
        v = host.snapshot_version
        await asyncio.sleep(0.2)
        assert host.snapshot_version > v, "the engine keeps running"
    run(body, tmp_path)


def test_a_session_keeps_running_when_every_client_disconnects(tmp_path) -> None:
    async def body(svc, engine, host):
        c = await Client.connect(svc)
        out = await c.call("start_session", experiment="free_feeding", session_name="survives")
        assert out["ok"], out
        c.writer.transport.abort()
        await asyncio.sleep(0.3)
        assert svc.client_count == 0
        assert engine._exp.is_running
        d = await Client.connect(svc)
        state = (await d.read_until(lambda m: "state" in m))["state"]
        assert state["session"]["running"] is True and state["session"]["name"] == "survives"
        assert (await d.call("stop_session"))["ok"]
    run(body, tmp_path)


def test_log_rows_over_the_wire(tmp_path) -> None:
    async def body(svc, engine, host):
        c = await Client.connect(svc)
        await c.call("dispense", node=1)
        await c.call("ping", node=1)
        out = await c.call("log_rows", after=0, limit=10)
        assert out["ok"] and [r["event"] for r in out["rows"]][:2] == ["Dispense", "Ping"]
        more = await c.call("log_rows", after=out["rows"][-1]["seq"])
        assert more["rows"] == []
        assert (await c.call("log_rows", after="x"))["ok"] is False
    run(body, tmp_path)


def test_a_client_that_is_not_reading_is_cut_off(tmp_path, monkeypatch) -> None:
    aborted = []

    class T:
        def get_write_buffer_size(self): return 10_000_000
        def abort(self): aborted.append(True)

    class W:
        transport = T()
        def is_closing(self): return False
        def write(self, data): raise AssertionError("must not write to a backed-up client")

    svc = EngineService(SimpleNamespace(), ("tcp", ("127.0.0.1", 0)))
    svc._send(W(), b"x" * 10)
    assert aborted == [True]


def test_nan_in_a_message_is_refused_not_sent() -> None:
    with pytest.raises(ValueError):
        encode({"x": float("nan")})


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_unix_socket_is_private_and_removed_on_stop(tmp_path) -> None:
    with tempfile.TemporaryDirectory() as d:   # short path: socket names are limited to ~100 bytes
        path = os.path.join(d, "e.sock")

        async def body(svc, engine, host):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode == 0o600
            c = await Client.connect(svc)
            assert (await c.call("get_dev_settings"))["ok"]
        run(body, tmp_path, address=("unix", path))
        assert not os.path.exists(path)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_a_stale_socket_file_from_a_dead_engine_is_replaced(tmp_path) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "e.sock")
        open(path, "w").close()

        async def body(svc, engine, host):
            c = await Client.connect(svc)
            assert (await c.call("get_dev_settings"))["ok"]
        run(body, tmp_path, address=("unix", path))


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_a_too_long_socket_path_is_refused_with_a_clear_message(tmp_path) -> None:
    async def body():
        host = SimpleNamespace(snapshot_version=0, snapshot=lambda: {})
        svc = EngineService(host, ("unix", str(tmp_path / ("x" * 120) / "e.sock")))
        with pytest.raises(ValueError, match="too long"):
            await svc.start()
    asyncio.run(body())
