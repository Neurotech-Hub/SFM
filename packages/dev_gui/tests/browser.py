"""
A small Chrome DevTools Protocol driver for UI tests: drive the real page in
headless Chromium, read the DOM, click and drag, take screenshots, and collect
every console error and uncaught exception.

Needs a `chromium` (or `chromium-browser`, `google-chrome`) on the PATH. Tests
that use it skip when there is none.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

import aiohttp

CHROME = next((p for p in (shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome", "chrome")) if p), None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Page:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.sid: Optional[str] = None
        self._id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.requests: List[str] = []    # every URL the page asked for
        self.errors: List[str] = []      # console errors and uncaught exceptions
        self.console: List[str] = []

    async def __aenter__(self) -> "Page":
        port = _free_port()
        flags = ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--hide-scrollbars"]
        if (hasattr(os, "geteuid") and os.geteuid() == 0) or os.environ.get("CI"):
            flags.append("--no-sandbox")   # Chromium refuses to sandbox as root, and CI containers lack the kernel support
        self.proc = subprocess.Popen(
            [CHROME, *flags, f"--user-data-dir={self._tmp.name}", f"--remote-debugging-port={port}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.session = aiohttp.ClientSession()
        ws_url = None
        for _ in range(100):
            try:
                async with self.session.get(f"http://127.0.0.1:{port}/json/version") as r:
                    ws_url = (await r.json())["webSocketDebuggerUrl"]
                    break
            except aiohttp.ClientError:
                await asyncio.sleep(0.1)
        if not ws_url:
            raise RuntimeError("Chromium did not start")
        self.ws = await self.session.ws_connect(ws_url, max_msg_size=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read())
        target = (await self._send("Target.createTarget", {"url": "about:blank"}, session=False))["targetId"]
        self.sid = (await self._send("Target.attachToTarget", {"targetId": target, "flatten": True}, session=False))["sessionId"]
        for domain in ("Page", "Runtime", "Log", "Network"):
            await self._send(f"{domain}.enable")
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self._tmp.cleanup()

    async def _read(self) -> None:
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if "id" in data and data["id"] in self._pending:
                fut = self._pending.pop(data["id"])
                if "error" in data:
                    fut.set_exception(RuntimeError(f"{data['error']}"))
                else:
                    fut.set_result(data.get("result", {}))
            else:
                self._event(data)

    def _event(self, data: Dict[str, Any]) -> None:
        method, params = data.get("method"), data.get("params", {})
        if method == "Network.requestWillBeSent":
            self.requests.append(params["request"]["url"])
        elif method == "Runtime.exceptionThrown":
            d = params["exceptionDetails"]
            self.errors.append("exception: " + (d.get("exception", {}).get("description") or d.get("text", "")))
        elif method == "Runtime.consoleAPICalled":
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
            self.console.append(f"{params.get('type')}: {text}")
            if params.get("type") in ("error", "assert"):
                self.errors.append("console.error: " + text)
        elif method == "Log.entryAdded":
            e = params["entry"]
            if e.get("level") == "error":
                self.errors.append(f"log: {e.get('text')} {e.get('url', '')}")

    async def _send(self, method: str, params: Optional[dict] = None, session: bool = True) -> dict:
        self._id += 1
        msg: Dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = self.sid
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self.ws.send_str(json.dumps(msg))
        return await asyncio.wait_for(fut, 30)

    # ---- page actions ----
    async def viewport(self, width: int, height: int) -> None:
        await self._send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})

    async def goto(self, url: str) -> None:
        await self._send("Page.navigate", {"url": url})
        await self.wait("document.readyState === 'complete'")

    async def eval(self, expr: str) -> Any:
        r = await self._send("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError("JS error: " + (d.get("exception", {}).get("description") or d.get("text", "")))
        return r["result"].get("value")

    async def wait(self, expr: str, timeout: float = 8.0) -> None:
        end = asyncio.get_running_loop().time() + timeout
        last = None
        while asyncio.get_running_loop().time() < end:
            try:
                last = await self.eval(expr)
                # A DOM node comes back as {}: that counts as found. Only null/false/0/"" mean "not yet".
                if last is not None and last is not False and last != 0 and last != "":
                    return
            except RuntimeError as exc:
                last = str(exc)
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for: {expr} (last: {last})")

    async def click(self, selector: str) -> None:
        ok = await self.eval(f"(() => {{ const e = document.querySelector({json.dumps(selector)}); if (!e) return false; e.click(); return true; }})()")
        assert ok, f"nothing matches {selector}"

    async def click_text(self, text: str, scope: str = "body") -> None:
        """Click the visible button whose text is exactly `text` (inside `scope`)."""
        ok = await self.eval(f"""(() => {{
            const root = document.querySelector({json.dumps(scope)});
            const b = [...root.querySelectorAll('button')].find(b => b.textContent.trim() === {json.dumps(text)} && !b.closest('[hidden]') && !b.disabled);
            if (!b) return false; b.click(); return true; }})()""")
        assert ok, f"no enabled button '{text}' in {scope}"

    async def click_step(self, label: str) -> None:
        """Click a step in the top navigation (its text is the step number plus the label)."""
        ok = await self.eval(f"""(() => {{
            const b = [...document.querySelectorAll('.nav .step')].find(b => b.textContent.endsWith({json.dumps(label)}) && !b.disabled);
            if (!b) return false; b.click(); return true; }})()""")
        assert ok, f"step '{label}' is not available"

    async def fill(self, selector: str, value: str) -> None:
        ok = await self.eval(f"""(() => {{
            const e = document.querySelector({json.dumps(selector)}); if (!e) return false;
            e.value = {json.dumps(value)};
            e.dispatchEvent(new Event('input', {{bubbles: true}})); e.dispatchEvent(new Event('change', {{bubbles: true}})); return true; }})()""")
        assert ok, f"nothing matches {selector}"

    async def text(self, selector: str) -> str:
        return await self.eval(f"(document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''")

    async def box(self, selector: str) -> Dict[str, float]:
        b = await self.eval(f"(() => {{ const e = document.querySelector({json.dumps(selector)}); if (!e) return null; const r = e.getBoundingClientRect(); return {{x: r.x, y: r.y, w: r.width, h: r.height}}; }})()")
        assert b, f"nothing matches {selector}"
        return b

    async def mouse(self, kind: str, x: float, y: float) -> None:
        await self._send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "buttons": 1 if kind != "mouseReleased" else 0, "clickCount": 1})

    async def drag(self, selector: str, dx: float, dy: float) -> None:
        b = await self.box(selector)
        x, y = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        await self.mouse("mouseMoved", x, y)
        await self.mouse("mousePressed", x, y)
        for i in range(1, 6):
            await self.mouse("mouseMoved", x + dx * i / 5, y + dy * i / 5)
        await self.mouse("mouseReleased", x + dx, y + dy)

    async def screenshot(self, path: str) -> None:
        import base64
        r = await self._send("Page.captureScreenshot", {"format": "png"})
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
