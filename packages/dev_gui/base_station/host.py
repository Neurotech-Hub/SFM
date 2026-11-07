"""
host.py — runs the engine on its own thread, with no front-end attached.

The engine has no locks and expects to be driven from one thread. ``EngineHost``
is that thread. Everything else (a gateway, a test) reaches the engine through
``submit`` / ``call``: the function is queued and runs on the engine thread,
between ticks. Nothing else may touch the engine.

Each loop it does, in order: run the queued commands, call ``engine.tick()``,
and, at most ``snapshot_hz`` times a second, rebuild the state snapshot. A
command or tick that raises is logged and reported to the caller; it never
stops the loop, so a bad request cannot stop the base station.

``snapshot()`` and ``rows_after()`` are safe from any thread: the first returns
the last published snapshot (an immutable dict, replaced whole, never edited);
the second reads the log through the engine thread.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional

from .state import build_snapshot, log_row_dict

log = logging.getLogger("sfm.host")

DEFAULT_TICK_S = 0.005
DEFAULT_SNAPSHOT_HZ = 10.0
# Commands run in one batch before the next tick, so a burst of them can't
# starve the CAN drain. The rest wait for the next loop.
MAX_COMMANDS_PER_LOOP = 32


class HostNotRunning(RuntimeError):
    """A command was sent to a host that is stopped (or stopping)."""


class EngineHost:
    """Owns the engine thread. See the module docstring."""

    def __init__(
        self,
        engine: Any,
        tick_s: float = DEFAULT_TICK_S,
        snapshot_hz: float = DEFAULT_SNAPSHOT_HZ,
    ) -> None:
        self._engine = engine
        self._tick_s = max(0.001, float(tick_s))
        self._snapshot_period = 1.0 / max(0.1, float(snapshot_hz))
        self._commands: "queue.Queue[tuple]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._accepting = False
        self._lock = threading.Lock()  # guards _accepting against stop()
        self._snapshot: Dict[str, Any] = {}
        self._snapshot_version = 0
        self._snapshot_changed = threading.Condition()
        self._last_snapshot_at = 0.0
        self._errors = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("EngineHost already started")
        self._stop.clear()
        with self._lock:
            self._accepting = True
        self._thread = threading.Thread(target=self._run, name="sfm-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the loop. Commands still queued are failed with HostNotRunning."""
        with self._lock:
            self._accepting = False
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._fail_pending()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def error_count(self) -> int:
        return self._errors

    # ------------------------------------------------------------------
    # Commands (any thread)
    # ------------------------------------------------------------------

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> "Future[Any]":
        """
        Queue ``fn(engine, *args, **kwargs)`` to run on the engine thread.

        The Future holds its result, or the exception it raised.
        """
        future: "Future[Any]" = Future()
        with self._lock:
            if not self._accepting:
                future.set_exception(HostNotRunning("engine host is not running"))
                return future
            self._commands.put((fn, args, kwargs, future))
        return future

    def call(self, fn: Callable[..., Any], *args: Any, timeout: Optional[float] = 10.0, **kwargs: Any) -> Any:
        """``submit`` and wait. Raises what ``fn`` raised, or TimeoutError."""
        if threading.current_thread() is self._thread:
            # Already on the engine thread: waiting on our own queue would deadlock.
            return fn(self._engine, *args, **kwargs)
        return self.submit(fn, *args, **kwargs).result(timeout)

    # ------------------------------------------------------------------
    # State (any thread)
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """The last published snapshot ({} before the first). Treat it as read-only."""
        return self._snapshot

    def wait_for_snapshot(self, after_version: int, timeout: float = 1.0) -> int:
        """Block until a snapshot newer than ``after_version`` exists. Returns the current version."""
        with self._snapshot_changed:
            self._snapshot_changed.wait_for(
                lambda: self._snapshot_version > after_version or self._stop.is_set(), timeout
            )
            return self._snapshot_version

    @property
    def snapshot_version(self) -> int:
        return self._snapshot_version

    def rows_after(
        self,
        seq: int,
        limit: int = 200,
        node_id: Optional[int] = None,
        frame_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Log rows after ``seq`` as dicts, oldest-first (see ``LogManager.rows_after``)."""
        def read(engine: Any) -> List[Dict[str, Any]]:
            if engine._log is None:
                return []
            return [log_row_dict(e) for e in engine._log.rows_after(seq, limit, node_id, frame_type)]
        return self.call(read)

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            self._run_commands()
            try:
                self._engine.tick()
            except Exception as exc:  # the engine guards its own tick; this is the last net
                self._note_error("tick", exc)
            now = time.monotonic()
            if now - self._last_snapshot_at >= self._snapshot_period:
                self._publish_snapshot()
                self._last_snapshot_at = now
            next_tick += self._tick_s
            delay = next_tick - time.monotonic()
            if delay > 0:
                # Wake early for a command instead of sleeping the full tick.
                try:
                    item = self._commands.get(timeout=delay)
                except queue.Empty:
                    continue
                self._run_one(item)
            else:
                # Behind schedule (a long command or a stall): don't try to catch up.
                next_tick = time.monotonic()

    def _run_commands(self) -> None:
        for _ in range(MAX_COMMANDS_PER_LOOP):
            try:
                item = self._commands.get_nowait()
            except queue.Empty:
                return
            self._run_one(item)

    def _run_one(self, item: tuple) -> None:
        fn, args, kwargs, future = item
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(fn(self._engine, *args, **kwargs))
        except BaseException as exc:  # reported to the caller; the loop goes on
            self._note_error(getattr(fn, "__name__", "command"), exc, report=False)
            future.set_exception(exc)

    def _publish_snapshot(self) -> None:
        try:
            snap = build_snapshot(self._engine)
        except Exception as exc:
            self._note_error("snapshot", exc)
            return
        with self._snapshot_changed:
            self._snapshot = snap
            self._snapshot_version += 1
            self._snapshot_changed.notify_all()

    def _note_error(self, where: str, exc: BaseException, report: bool = True) -> None:
        self._errors += 1
        self.last_error = f"{where}: {type(exc).__name__}: {exc}"
        if report:
            log.exception("engine host: %s failed", where)

    def _fail_pending(self) -> None:
        while True:
            try:
                _, _, _, future = self._commands.get_nowait()
            except queue.Empty:
                return
            if future.set_running_or_notify_cancel():
                future.set_exception(HostNotRunning("engine host stopped"))
