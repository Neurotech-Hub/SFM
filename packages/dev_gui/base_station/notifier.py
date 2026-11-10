"""
notifier.py — Phone alerts for node faults, via ntfy.

A Fault event whose code is enabled is posted to the lab's ntfy topic.
Heartbeats are not alerts: the caller invokes ``on_fault`` only for a Fault
CAN event. Each base station keeps its own config, so labs do not share topics.

File format (JSON)::

    {
      "version": 1,
      "enabled": true,
      "server": "https://ntfy.sh",
      "topic": "sfm-lab-a-7Qx2kP9wLm",
      "station_name": "Lab A rig 1",
      "faults": ["PelletLost", "FeedTimeout", "ActuatorTimeout"]
    }

Unknown keys are ignored on load. A missing or corrupt file falls back to
defaults (alerts on, no topic, so nothing is sent until a lab fills one in).

Default path: ``~/.sfm/notify.json``.

Sending runs on a background thread. A slow or unreachable server never
stalls the GUI, and a failed POST is retried once.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

from .protocol import ServiceStatus, fault_user_message

DEFAULT_NOTIFY_PATH = Path("~/.sfm/notify.json")
DEFAULT_SERVER = "https://ntfy.sh"

# Wire fault codes a lab can subscribe to. Order is the GUI checkbox order.
KNOWN_FAULTS = ("PelletLost", "FeedTimeout", "ActuatorTimeout")

DEDUPE_WINDOW_S = 60.0
SEND_TIMEOUT_S = 10.0

@dataclass
class SendResult:
    """Outcome of the most recent send attempt."""

    when: float
    ok: bool
    detail: str


@dataclass
class _Outgoing:
    url: str
    title: str
    body: str


class NotifySettings:
    """JSON-backed ntfy settings for this base station."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path or DEFAULT_NOTIFY_PATH).expanduser().resolve()
        self.enabled: bool = True
        self.server: str = DEFAULT_SERVER
        self.topic: str = ""
        self.station_name: str = ""
        self.faults: List[str] = list(KNOWN_FAULTS)
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        """Load settings from disk. Missing or corrupt file -> defaults."""
        self.enabled = True
        self.server = DEFAULT_SERVER
        self.topic = ""
        self.station_name = ""
        self.faults = list(KNOWN_FAULTS)
        if not self._path.is_file():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        enabled = data.get("enabled")
        if isinstance(enabled, bool):
            self.enabled = enabled
        server = data.get("server")
        if isinstance(server, str) and server.strip():
            self.server = server.strip()
        for attr in ("topic", "station_name"):
            value = data.get(attr)
            if isinstance(value, str):
                setattr(self, attr, value.strip())
        chosen = data.get("faults")
        if isinstance(chosen, list):
            self.faults = [name for name in KNOWN_FAULTS if name in chosen]

    def save(self) -> None:
        """Write the current settings to disk (atomic replace)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "enabled": self.enabled,
            "server": self.server,
            "topic": self.topic,
            "station_name": self.station_name,
            "faults": list(self.faults),
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        tmp.replace(self._path)


class Notifier:
    """
    Queue fault alerts and post them to ntfy off the GUI thread.

    ``on_fault`` and ``send_test`` return immediately. ``last_result`` is the
    latest completed attempt (None until the first one finishes).
    """

    def __init__(self, settings: Optional[NotifySettings] = None) -> None:
        self.settings = settings or NotifySettings()
        self._pending: "queue.Queue[_Outgoing]" = queue.Queue()
        self._recent: Dict[tuple, float] = {}
        self._result_lock = threading.Lock()
        self.last_result: Optional[SendResult] = None
        self._worker = threading.Thread(
            target=self._write_loop, name="sfm-ntfy", daemon=True
        )
        self._worker.start()

    def reload(self) -> None:
        """Re-read the settings file after the GUI saves it."""
        self.settings.load()

    def status_text(self) -> str:
        """One line for the Notifications window. Empty until a send finishes."""
        with self._result_lock:
            result = self.last_result
        if result is None:
            return ""
        when = datetime.fromtimestamp(result.when).strftime("%H:%M:%S")
        if result.ok:
            return f"Sent at {when}"
        return f"Failed at {when}: {result.detail}"

    def on_fault(
        self,
        node_id: int,
        code: Optional[ServiceStatus],
        label: str = "",
        session: str = "",
    ) -> None:
        """
        Queue an alert for this Fault event.

        No-op when alerts are off, the topic is empty, or the code is not
        enabled. A repeat of the same node and code inside
        ``DEDUPE_WINDOW_S`` is dropped. Never raises.
        """
        try:
            self._enqueue_fault(node_id, code, label, session)
        except Exception:  # noqa: BLE001 — an alert must not break frame handling
            pass

    def send_test(self) -> None:
        """Queue a test message through the same sender. Never raises."""
        try:
            self._enqueue_test()
        except Exception as exc:  # noqa: BLE001
            self._set_result(False, str(exc))

    def flush(self) -> None:
        """Block until every message queued so far has been sent. For tests."""
        self._pending.join()

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def _enqueue_fault(
        self,
        node_id: int,
        code: Optional[ServiceStatus],
        label: str,
        session: str,
    ) -> None:
        settings = self.settings
        if not settings.enabled:
            return
        topic = settings.topic.strip()
        if not topic:
            return
        code_name = code.name if code is not None else "unknown"
        if code_name not in settings.faults:
            return
        now = time.monotonic()
        key = (int(node_id), code_name)
        last = self._recent.get(key)
        if last is not None and (now - last) < DEDUPE_WINDOW_S:
            return
        self._recent[key] = now
        self._pending.put(_Outgoing(
            url=_endpoint(settings.server, topic),
            title=_fault_title(settings.station_name, int(node_id), code_name, label),
            body=_fault_body(code, session),
        ))

    def _enqueue_test(self) -> None:
        settings = self.settings
        topic = settings.topic.strip()
        if not topic:
            self._set_result(False, "Set a topic before sending a test")
            return
        station = settings.station_name.strip()
        head = f"SFM {station}".rstrip()
        self._pending.put(_Outgoing(
            url=_endpoint(settings.server, topic),
            title=f"{head}: test",
            body="Test notification from the SFM base station.",
        ))

    def _set_result(self, ok: bool, detail: str) -> None:
        with self._result_lock:
            self.last_result = SendResult(time.time(), ok, detail)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _write_loop(self) -> None:
        while True:
            item = self._pending.get()
            try:
                self._deliver(item)
            finally:
                self._pending.task_done()

    def _deliver(self, item: _Outgoing) -> None:
        error = ""
        for attempt in range(2):
            try:
                _post(item)
            except Exception as exc:  # noqa: BLE001 — record it, retry once
                error = _error_text(exc)
                if attempt == 0:
                    continue
            else:
                self._set_result(True, item.title)
                return
        self._set_result(False, error or "send failed")


def _fault_title(station_name: str, node_id: int, code_name: str, label: str) -> str:
    who = f"Node {node_id}"
    shown = (label or "").strip()
    if shown and shown != who:
        who = f"{who} ({shown})"
    head = f"SFM {station_name.strip()}".rstrip()
    return f"{head}: {who} {code_name}"


def _fault_body(code: Optional[ServiceStatus], session: str) -> str:
    name = session.strip() or "—"
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{fault_user_message(code)}\nSession: {name}\n{when}"


def _endpoint(server: str, topic: str) -> str:
    base = (server or "").strip().rstrip("/")
    if not base:
        base = DEFAULT_SERVER
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return f"{base}/{quote(topic, safe='')}"


def _post(item: _Outgoing) -> None:
    req = urllib.request.Request(
        item.url,
        data=item.body.encode("utf-8"),
        method="POST",
    )
    req.add_header("Title", _header(item.title))
    req.add_header("Priority", "high")
    req.add_header("Tags", "warning")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as resp:
        resp.read()


def _header(value: str) -> str:
    """urllib encodes headers as Latin-1. Keep the title sendable."""
    return value.encode("latin-1", errors="replace").decode("latin-1")


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    text = str(exc).strip()
    return text or type(exc).__name__
