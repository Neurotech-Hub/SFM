"""
service.py — the base-station engine as a process of its own.

    python -m base_station.service

It runs the engine on an ``EngineHost`` thread and offers it to local clients
(the gateway, tests) over a socket: a Unix socket on Linux, TCP on 127.0.0.1
elsewhere. A browser or gateway that crashes or disconnects changes nothing
here: the engine thread keeps ticking, the log keeps writing.

Wire format: one JSON object per line, in both directions.

    client -> engine   {"id": 7, "cmd": "dispense", "args": {"node": 1}}
    engine -> client   {"id": 7, "result": {"ok": true}}
                       {"id": 7, "result": {"ok": false, "error": "..."}}
    engine -> client   {"state": {...snapshot...}}     (on connect, then on change)

Anything a command does is in ``commands.py``; this file only carries them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from .commands import run_command
from .host import EngineHost, HostNotRunning
from .process_lock import AlreadyRunning, ProcessLock

log = logging.getLogger("sfm.service")

# A request line is a command name and a few values; a megabyte is far more
# than any real one. Anything larger is a broken or hostile client.
MAX_LINE_BYTES = 1 << 20
# A client that lets this much pushed data pile up unread is cut off, so one
# stuck client can't grow the engine's memory.
MAX_CLIENT_BACKLOG_BYTES = 4 << 20
COMMAND_TIMEOUT_S = 30.0
# sockaddr_un.sun_path is 108 bytes on Linux, including the terminating NUL.
MAX_UNIX_PATH_BYTES = 100
PUSH_POLL_S = 0.05

Address = Tuple[str, Any]  # ("unix", path) or ("tcp", (host, port))


def default_address() -> Address:
    """Unix socket under the user's runtime directory on Linux, else TCP on loopback."""
    if hasattr(asyncio, "start_unix_server") and sys.platform != "win32":
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        base = Path(runtime) if runtime else Path("~/.sfm").expanduser()
        return ("unix", str(base / "sfm-engine.sock"))
    return ("tcp", ("127.0.0.1", 8765))


def encode(message: Dict[str, Any]) -> bytes:
    """One line of JSON. Refuses NaN and infinity, which browsers can't parse."""
    return json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


class EngineService:
    """Serves one ``EngineHost`` on a socket. Create inside a running event loop."""

    def __init__(self, host: EngineHost, address: Address) -> None:
        self._host = host
        self._address = address
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._push_task: Optional[asyncio.Task] = None
        self._last_pushed = 0

    @property
    def address(self) -> Address:
        return self._address

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def start(self) -> None:
        kind, where = self._address
        if kind == "unix":
            path = Path(where)
            if len(os.fsencode(str(path))) > MAX_UNIX_PATH_BYTES:
                raise ValueError(
                    f"The socket path is too long for a Unix socket ({MAX_UNIX_PATH_BYTES} bytes at most): {path}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            # The process lock guarantees no other engine is listening, so a
            # socket file still here belongs to a process that died.
            if path.exists():
                path.unlink()
            self._server = await asyncio.start_unix_server(self._client, path=str(path), limit=MAX_LINE_BYTES)
            os.chmod(path, 0o600)  # only this user may command the rig
        else:
            host, port = where
            self._server = await asyncio.start_server(self._client, host, port, limit=MAX_LINE_BYTES)
            if port == 0:
                self._address = ("tcp", (host, self._server.sockets[0].getsockname()[1]))
        self._push_task = asyncio.create_task(self._push_loop(), name="sfm-push")

    async def stop(self) -> None:
        if self._push_task is not None:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
        if self._server is not None:
            self._server.close()
            for writer in list(self._clients):
                writer.close()
            await self._server.wait_closed()
        if self._address[0] == "unix":
            try:
                Path(self._address[1]).unlink()
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------
    # One client
    # ------------------------------------------------------------------

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        tasks: Set[asyncio.Task] = set()
        try:
            self._write(writer, {"state": self._host.snapshot()})
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    break
                except (asyncio.LimitOverrunError, ValueError):
                    log.warning("client sent an oversized line; disconnecting it")
                    break
                task = asyncio.create_task(self._handle(writer, line))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            self._clients.discard(writer)
            writer.close()

    async def _handle(self, writer: asyncio.StreamWriter, line: bytes) -> None:
        request_id: Any = None
        try:
            try:
                request = json.loads(line)
            except ValueError:
                self._write(writer, {"id": None, "result": {"ok": False, "error": "That was not valid JSON."}})
                return
            if not isinstance(request, dict) or not isinstance(request.get("cmd"), str):
                self._write(writer, {"id": None, "result": {"ok": False, "error": "A request needs a 'cmd'."}})
                return
            request_id = request.get("id")
            args = request.get("args") or {}
            if not isinstance(args, dict):
                self._write(writer, {"id": request_id, "result": {"ok": False, "error": "'args' must be an object."}})
                return
            result = await self._run(request["cmd"], args)
            self._write(writer, {"id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("handling a request failed")
            self._write(writer, {"id": request_id, "result": {"ok": False, "error": "Internal error."}})

    async def _run(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        future = self._host.submit(run_command, name, args)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), COMMAND_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"{name} is taking too long; it may still finish."}
        except HostNotRunning:
            return {"ok": False, "error": "The engine is shutting down."}
        except Exception:
            # run_command turns the expected failures into replies. This is a bug in a command.
            log.exception("command %s raised", name)
            return {"ok": False, "error": f"{name} failed inside the engine. See the engine log."}

    # ------------------------------------------------------------------
    # Pushing state
    # ------------------------------------------------------------------

    def _write(self, writer: asyncio.StreamWriter, message: Dict[str, Any]) -> None:
        try:
            data = encode(message)
        except ValueError:
            log.exception("a message could not be encoded")
            return
        self._send(writer, data)

    def _send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        if writer.is_closing():
            return
        if writer.transport.get_write_buffer_size() + len(data) > MAX_CLIENT_BACKLOG_BYTES:
            log.warning("client is not reading; disconnecting it")
            writer.transport.abort()
            return
        writer.write(data)

    async def _push_loop(self) -> None:
        """Send the state to every client each time a new snapshot is published."""
        while True:
            await asyncio.sleep(PUSH_POLL_S)
            version = self._host.snapshot_version
            if version == self._last_pushed:
                continue
            self._last_pushed = version
            if not self._clients:
                continue
            try:
                data = encode({"state": self._host.snapshot()})
            except ValueError:
                log.exception("the state could not be encoded")
                continue
            for writer in list(self._clients):
                self._send(writer, data)


# ---------------------------------------------------------------------------
# The process
# ---------------------------------------------------------------------------

def build_engine(args: argparse.Namespace) -> Any:
    from .engine import BaseStationEngine
    return BaseStationEngine(argparse.Namespace(
        interface=args.interface, bitrate=args.bitrate, nodes=args.nodes, log_dir=args.log_dir,
    ))


async def serve(args: argparse.Namespace) -> None:
    lock = ProcessLock(name="sfm-engine").acquire()
    engine = build_engine(args)
    host = EngineHost(engine)
    service = EngineService(host, args.address)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        host.start()
        await service.start()
        log.info("engine service listening on %s %s", *service.address)
        await stop.wait()
    finally:
        log.info("shutting down")
        await service.stop()
        host.stop()
        try:
            engine._shutdown()
        finally:
            lock.release()


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sfm-engine", description="SFM base-station engine service")
    p.add_argument("--socket", help="Unix socket path (default: $XDG_RUNTIME_DIR/sfm-engine.sock)")
    p.add_argument("--tcp-port", type=int, help="listen on 127.0.0.1:PORT instead of a Unix socket")
    p.add_argument("--interface", "-i", default="can0", help="default CAN interface the UI offers")
    p.add_argument("--bitrate", "-b", type=int, default=250_000)
    p.add_argument("--nodes", "-n", type=int, default=2)
    p.add_argument("--log-dir", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.tcp_port is not None:
        args.address = ("tcp", ("127.0.0.1", args.tcp_port))
    elif args.socket:
        args.address = ("unix", args.socket)
    else:
        args.address = default_address()
    return args


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(serve(args))
    except AlreadyRunning as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
