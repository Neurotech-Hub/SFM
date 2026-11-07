"""
gateway.py — the web face of the engine service.

    python -m base_station.gateway            # http://127.0.0.1:8080

A small aiohttp app, bound to 127.0.0.1 only, that sits between a browser and
the engine service (``service.py``). It holds no state of its own beyond the
last one the engine sent, so it can crash or restart at any time without
touching a session.

    GET  /                    the page (Phase 4 fills ``web/``)
    GET  /api/state           the latest engine state, as JSON
    POST /api/cmd/<name>      run a command; the JSON body is its arguments
    GET  /ws                  WebSocket: pushes the engine state as it changes

Because a kiosk browser can be pointed at untrusted pages, a page from another
site must not be able to drive the rig. Every request is checked:

* ``Host`` must be a loopback name. That stops DNS-rebinding, where a hostile
  name is made to resolve to 127.0.0.1.
* ``Origin``, when the browser sends one, must be this same origin.
* A POST must carry ``X-SFM-Request: 1``. A web page can't add a custom header
  to a cross-site request without the browser asking this server first.

There is no login: the gateway is for the monitor on the Pi.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .service import Address, MAX_LINE_BYTES, default_address, encode

log = logging.getLogger("sfm.gateway")

ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}
REQUEST_HEADER = "X-SFM-Request"
MAX_BODY_BYTES = 256 * 1024
COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
WS_SEND_TIMEOUT_S = 5.0
WS_IDLE_PING_S = 15.0
RECONNECT_DELAYS_S = (0.25, 0.5, 1.0, 2.0, 5.0)
WEB_DIR = Path(__file__).resolve().parent / "web"

LINK: "web.AppKey[EngineLink]" = web.AppKey("link")
SOCKETS: "web.AppKey[set]" = web.AppKey("sockets")

ENGINE_DOWN = {"ok": False, "error": "The engine is not running. Start sfm-engine and try again."}


# ---------------------------------------------------------------------------
# The link to the engine
# ---------------------------------------------------------------------------

class EngineLink:
    """
    One connection to the engine service, kept open and re-opened when it drops.

    ``call`` sends a command and waits for its reply. ``state`` is the newest
    state the engine pushed. ``version`` changes whenever the state or the
    connection does, and ``wait_change`` lets a listener sleep until then.
    """

    def __init__(self, address: Address, call_timeout_s: float = 35.0) -> None:
        self._address = address
        self._call_timeout_s = call_timeout_s
        self._writer: Optional[asyncio.StreamWriter] = None
        self._pending: Dict[int, "asyncio.Future[Dict[str, Any]]"] = {}
        self._next_id = 0
        self._task: Optional[asyncio.Task] = None
        self._changed = asyncio.Condition()
        self.state: Optional[Dict[str, Any]] = None
        self.connected = False
        self.version = 0
        self._encoded: Optional[tuple] = None  # (version, json text)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="sfm-engine-link")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._drop()

    async def call(self, cmd: str, args: Dict[str, Any]) -> Dict[str, Any]:
        writer = self._writer
        if writer is None or writer.is_closing():
            return dict(ENGINE_DOWN)
        self._next_id += 1
        request_id = self._next_id
        future: "asyncio.Future[Dict[str, Any]]" = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            writer.write(encode({"id": request_id, "cmd": cmd, "args": args}))
            return await asyncio.wait_for(future, self._call_timeout_s)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"{cmd} did not answer in time."}
        except (ConnectionError, ValueError):
            return dict(ENGINE_DOWN)
        finally:
            self._pending.pop(request_id, None)

    async def wait_change(self, seen_version: int, timeout: float) -> int:
        """Sleep until ``version`` differs from ``seen_version`` or ``timeout`` passes."""
        async with self._changed:
            try:
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: self.version != seen_version), timeout
                )
            except asyncio.TimeoutError:
                pass
            return self.version

    def state_json(self) -> str:
        """The state message for browsers, encoded once per version."""
        if self._encoded is None or self._encoded[0] != self.version:
            text = json.dumps(
                {"type": "state", "engine": self.connected, "state": self.state},
                separators=(",", ":"), allow_nan=False,
            )
            self._encoded = (self.version, text)
        return self._encoded[1]

    # -- internals --------------------------------------------------------

    async def _bump(self) -> None:
        async with self._changed:
            self.version += 1
            self._changed.notify_all()

    def _drop(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
        for future in self._pending.values():
            if not future.done():
                future.set_result(dict(ENGINE_DOWN))
        self._pending.clear()

    async def _open(self):
        kind, where = self._address
        if kind == "unix":
            return await asyncio.open_unix_connection(where, limit=MAX_LINE_BYTES * 4)
        return await asyncio.open_connection(*where, limit=MAX_LINE_BYTES * 4)

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                reader, writer = await self._open()
            except (OSError, asyncio.TimeoutError):
                delay = RECONNECT_DELAYS_S[min(attempt, len(RECONNECT_DELAYS_S) - 1)]
                attempt += 1
                await asyncio.sleep(delay)
                continue
            attempt = 0
            self._writer = writer
            self.connected = True
            await self._bump()
            log.info("connected to the engine")
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    await self._on_message(line)
            except (ConnectionError, asyncio.IncompleteReadError, ValueError):
                pass
            finally:
                self.connected = False
                self.state = None
                self._drop()
                await self._bump()
                log.warning("lost the engine connection; retrying")

    async def _on_message(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            log.warning("the engine sent something that is not JSON")
            return
        if "state" in message:
            self.state = message["state"]
            await self._bump()
        elif "id" in message:
            future = self._pending.get(message["id"])
            if future is not None and not future.done():
                future.set_result(message.get("result") or {"ok": False, "error": "Empty reply."})


# ---------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------

def _split_host(value: str) -> tuple:
    """(hostname, port or None) from a Host header or the authority of an Origin."""
    parts = urlsplit("//" + value)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    return host, port


@web.middleware
async def security(request: web.Request, handler):
    host, host_port = _split_host(request.headers.get("Host", ""))
    if host not in ALLOWED_HOSTNAMES:
        return web.json_response({"ok": False, "error": "Wrong host."}, status=403)

    origin = request.headers.get("Origin")
    if origin is not None:
        parts = urlsplit(origin)
        o_host, o_port = _split_host(parts.netloc)
        # Same origin only: loopback host, and the very port this server answered on.
        if parts.scheme != "http" or o_host not in ALLOWED_HOSTNAMES or o_port != host_port:
            return web.json_response({"ok": False, "error": "Cross-origin request refused."}, status=403)

    if request.method == "POST" and request.headers.get(REQUEST_HEADER) != "1":
        return web.json_response({"ok": False, "error": f"Missing {REQUEST_HEADER} header."}, status=403)

    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Cache-Control", "no-store")
    if not isinstance(response, web.WebSocketResponse):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
            "img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        )
    return response


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def index(request: web.Request) -> web.StreamResponse:
    page = WEB_DIR / "index.html"
    if page.is_file():
        return web.FileResponse(page)
    return web.Response(text="SFM gateway is running. The Rig Map page has not been installed.", content_type="text/plain")


async def api_state(request: web.Request) -> web.Response:
    link: EngineLink = request.app[LINK]
    return web.Response(text=link.state_json(), content_type="application/json")


async def api_command(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if not COMMAND_NAME.match(name):
        return web.json_response({"ok": False, "error": "Bad command name."}, status=400)
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        return web.json_response({"ok": False, "error": "Request too large."}, status=413)
    if request.can_read_body:
        if not request.content_type.startswith("application/json"):
            return web.json_response({"ok": False, "error": "Send JSON."}, status=415)
        try:
            args = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "That was not valid JSON."}, status=400)
    else:
        args = {}
    if not isinstance(args, dict):
        return web.json_response({"ok": False, "error": "The body must be a JSON object."}, status=400)

    link: EngineLink = request.app[LINK]
    result = await link.call(name, args)
    if result.get("ok"):
        status = 200
    elif result.get("error") == ENGINE_DOWN["error"]:
        status = 503
    else:
        status = 422
    return web.json_response(result, status=status)


async def websocket(request: web.Request) -> web.WebSocketResponse:
    link: EngineLink = request.app[LINK]
    ws = web.WebSocketResponse(heartbeat=WS_IDLE_PING_S, max_msg_size=4096)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)

    async def drain_incoming() -> None:
        # The browser sends nothing we act on; reading is how a close is noticed.
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break

    reader = asyncio.create_task(drain_incoming())
    seen = -1
    try:
        while not ws.closed and not reader.done():
            if link.version != seen:
                seen = link.version
                await asyncio.wait_for(ws.send_str(link.state_json()), WS_SEND_TIMEOUT_S)
                continue
            await link.wait_change(seen, timeout=1.0)
    except (asyncio.TimeoutError, ConnectionError):
        log.info("a browser stopped reading; dropping it")
    finally:
        reader.cancel()
        request.app[SOCKETS].discard(ws)
        await ws.close()
    return ws


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_app(address: Address) -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=MAX_BODY_BYTES)
    app[LINK] = EngineLink(address)
    app[SOCKETS] = set()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/cmd/{name}", api_command)
    app.router.add_get("/ws", websocket)
    if (WEB_DIR / "static").is_dir():
        app.router.add_static("/static/", WEB_DIR / "static", follow_symlinks=False)

    async def on_startup(app: web.Application) -> None:
        await app[LINK].start()

    async def on_cleanup(app: web.Application) -> None:
        for ws in list(app[SOCKETS]):
            await ws.close()
        await app[LINK].stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sfm-gateway", description="SFM web gateway (localhost only)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--engine-socket", help="engine Unix socket (default: $XDG_RUNTIME_DIR/sfm-engine.sock)")
    p.add_argument("--engine-tcp-port", type=int, help="engine on 127.0.0.1:PORT instead")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.engine_tcp_port is not None:
        args.address = ("tcp", ("127.0.0.1", args.engine_tcp_port))
    elif args.engine_socket:
        args.address = ("unix", args.engine_socket)
    else:
        args.address = default_address()
    return args


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # 127.0.0.1 only: never the network.
    web.run_app(create_app(args.address), host="127.0.0.1", port=args.port, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
