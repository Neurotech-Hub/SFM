"""
script.py — Sequential (generator-based) experiment authoring.

Lets a template write a task top-to-bottom as one Python generator instead of
a web of ``@exp.on_*`` callbacks:

    @exp.script
    def run(control):
        while True:
            trial = control.next_trial()
            control.dispense(fed)
            r = yield control.wait_for("pellet_taken", node=fed, timeout=30.0)
            if r.faulted:
                yield control.wait(5.0)
                continue
            yield control.wait_until(lambda c: c.domes_closed() and c.quiet_for(5.0))

``control.wait_for`` / ``wait_until`` / ``wait`` are pure constructors — they
build an ``_Await`` and do nothing else. All arming happens in
``ScriptScheduler`` at the moment the object comes back out of the generator's
``yield``, so building one without yielding it is harmless.

The scheduler is driven by ``ExperimentRunner`` at three points: once on
session activation, once after every dispatched event batch, and once after
timers tick each step. See runner.py for exactly where.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Callable, Generator, Optional, Tuple, Union

from .events import EventKind, NodeEvent

if TYPE_CHECKING:
    from .context import ExperimentControl


ScriptFn = Callable[["ExperimentControl"], Generator[Any, Any, None]]

# How many times the generator may be resumed within a single advance() call
# before we give up for this tick. Backstop against a script that yields
# awaits which resolve immediately in a tight loop.
MAX_ADVANCES_PER_TICK = 64

# Minimum interval between repeated "still waiting" log rows for one await.
STALL_WARN_S = 120.0


class _AwaitKind(Enum):
    EVENT = auto()  # wait_for
    UNTIL = auto()  # wait_until
    DELAY = auto()  # wait(seconds > 0)
    TICK = auto()  # bare yield / yield None / wait(<= 0)


@dataclass
class _Await:
    """
    One yielded wait request. Built by ``control.wait_for`` / ``wait_until`` /
    ``wait`` — pure, no side effects. Armed and resolved by ScriptScheduler.
    ``yield`` returns this same object, mutated in place, as the send() value.
    """

    kind: _AwaitKind
    event_kind: Optional[EventKind] = None
    nodes: Tuple[int, ...] = ()
    predicate: Optional[Callable[["ExperimentControl"], bool]] = None
    seconds: float = 0.0
    timeout: Optional[float] = None
    label: str = ""

    # Filled in by ScriptScheduler at arm time / resolution time.
    armed_at: Optional[float] = None
    armed_seq: int = -1
    deadline: Optional[float] = None
    satisfied: bool = False
    timed_out: bool = False
    faulted: bool = False
    faulted_node: int = 0
    event: Optional[NodeEvent] = None

    @property
    def ok(self) -> bool:
        return self.satisfied and not self.timed_out and not self.faulted

    def __bool__(self) -> bool:
        return self.ok


def _resolve_event_kind(name: str) -> EventKind:
    try:
        return EventKind[name.upper()]
    except KeyError:
        valid = ", ".join(k.name.lower() for k in EventKind)
        raise ValueError(
            f"wait_for(): unknown event kind {name!r}. Valid names: {valid}"
        ) from None


def _as_node_tuple(node: Union[int, "list[int]", "tuple[int, ...]", None]) -> Tuple[int, ...]:
    if node is None:
        return ()
    if isinstance(node, int):
        return (node,)
    return tuple(node)


def _node_suffix(nodes: Tuple[int, ...]) -> str:
    if not nodes:
        return ""
    return f"(node={','.join(str(n) for n in nodes)})"


class ScriptScheduler:
    """Drives one ``@exp.script`` generator against the runner's event/timer feed."""

    def __init__(self, ctx: "ExperimentControl", script_fn: ScriptFn) -> None:
        self._ctx = ctx
        self._script_fn = script_fn
        self._gen: Optional[Generator[Any, Any, None]] = None
        self._pending: Optional[_Await] = None
        self._done = False
        self._advance_seq = 0
        self._warned_cap = False

    # ------------------------------------------------------------------
    # Lifecycle (called by ExperimentRunner)
    # ------------------------------------------------------------------

    def start(self, now: float) -> None:
        if self._gen is not None or self._done:
            return
        self._gen = self._script_fn(self._ctx)

    def close(self) -> None:
        if self._gen is not None:
            try:
                self._gen.close()
            except Exception as exc:  # noqa: BLE001 — never let teardown kill the session
                self._ctx.log("script_close_error", error=str(exc))
        self._gen = None
        self._done = True

    # ------------------------------------------------------------------
    # Event delivery — called by _dispatch_all for every dispatched event,
    # strictly before the batch's single advance() call.
    # ------------------------------------------------------------------

    def observe(self, ev: NodeEvent) -> None:
        aw = self._pending
        if aw is None or aw.satisfied or aw.kind is not _AwaitKind.EVENT:
            return
        if aw.event_kind is not None and ev.kind is not aw.event_kind:
            return
        if aw.nodes and ev.node_id not in aw.nodes:
            return
        aw.satisfied = True
        aw.event = ev

    def on_node_halted(self, node_id: int) -> None:
        """Hook for ctx.halt_node(): abort a pending await scoped to this node."""
        aw = self._pending
        if aw is None or aw.satisfied or node_id not in aw.nodes:
            return
        aw.satisfied = True
        aw.faulted = True
        aw.faulted_node = node_id

    # ------------------------------------------------------------------
    # Advance
    # ------------------------------------------------------------------

    def advance(self, now: float) -> None:
        if self._done or self._gen is None or self._ctx.stop_requested:
            return
        self._advance_seq += 1
        for _ in range(MAX_ADVANCES_PER_TICK):
            aw = self._pending
            if aw is not None and not self._resolve(aw, now):
                self._maybe_warn_stalled(aw, now)
                return
            self._pending = None

            try:
                sent = self._gen.send(aw)
            except StopIteration:
                self._done = True
                self._gen = None
                self._ctx.log("script_finished", trials=self._ctx.trial)
                self._ctx.stop("script_finished")
                return
            except Exception as exc:  # noqa: BLE001 — the script *is* the experiment
                self._fail(exc)
                return

            if self._ctx.stop_requested:
                return

            self._pending = self._arm(self._coerce(sent), now)
        if not self._warned_cap:
            self._warned_cap = True
            self._ctx.log("script_advance_cap", advances=MAX_ADVANCES_PER_TICK)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _coerce(self, sent: Any) -> _Await:
        if isinstance(sent, _Await):
            return sent
        if sent is None:
            return _Await(kind=_AwaitKind.TICK, label="tick")
        if isinstance(sent, (int, float)) and not isinstance(sent, bool):
            return _Await(kind=_AwaitKind.DELAY, seconds=float(sent), label=f"wait({sent:g}s)")
        self._ctx.log("script_bad_yield", value=repr(sent))
        return _Await(kind=_AwaitKind.TICK, label="tick")

    def _arm(self, aw: _Await, now: float) -> _Await:
        aw.armed_at = now
        aw.armed_seq = self._advance_seq
        aw.satisfied = False
        aw.timed_out = False
        aw.faulted = False
        aw.faulted_node = 0
        aw.event = None
        if aw.kind is _AwaitKind.DELAY:
            aw.deadline = now + aw.seconds
        elif aw.timeout is not None:
            aw.deadline = now + max(0.0, float(aw.timeout))
        else:
            aw.deadline = None
        return aw

    def _resolve(self, aw: _Await, now: float) -> bool:
        if aw.satisfied:
            return True

        # Node-scoped abort + bare-tick resolution: deferred to the NEXT
        # advance (armed_seq guard) so these can never satisfy in the same
        # advance() call that armed them — that would let a spinning script
        # (or a fully-halted rig) burn the whole MAX_ADVANCES_PER_TICK budget
        # on zero-progress iterations. One resolution per tick is enough;
        # end_after() bounds the session regardless.
        if aw.armed_seq < self._advance_seq:
            for n in aw.nodes:
                if self._ctx.is_halted(n):
                    aw.satisfied = True
                    aw.faulted = True
                    aw.faulted_node = n
                    self._ctx.log("script_await_aborted", node=n, waiting_on=aw.label)
                    return True
            if aw.kind is _AwaitKind.TICK:
                aw.satisfied = True
                return True

        if aw.kind is _AwaitKind.DELAY:
            if now >= aw.deadline:
                aw.satisfied = True
                return True
            return False

        if aw.kind is _AwaitKind.UNTIL:
            try:
                if aw.predicate(self._ctx):
                    aw.satisfied = True
                    return True
            except Exception as exc:  # noqa: BLE001 — a broken predicate must not hang or kill the session
                self._ctx.log("script_predicate_error", waiting_on=aw.label, error=str(exc))

        if aw.deadline is not None and now >= aw.deadline:
            aw.satisfied = True
            aw.timed_out = True
            self._ctx.log(
                "script_timeout",
                waiting_on=aw.label,
                after_s=round(now - (aw.armed_at or now), 3),
            )
            return True

        return False

    def _maybe_warn_stalled(self, aw: _Await, now: float) -> None:
        armed_at = aw.armed_at if aw.armed_at is not None else now
        last_warn = getattr(aw, "_last_stall_warn", None)
        elapsed = now - armed_at
        if elapsed < STALL_WARN_S:
            return
        if last_warn is not None and (now - last_warn) < STALL_WARN_S:
            return
        aw._last_stall_warn = now  # type: ignore[attr-defined]
        self._ctx.log("script_stalled", waiting_on=aw.label, elapsed_s=round(elapsed, 1))

    def _fail(self, exc: Exception) -> None:
        self._done = True
        self._gen = None
        frames = traceback.extract_tb(exc.__traceback__)
        last = frames[-1] if frames else None
        where = f"{last.filename}:{last.lineno} in {last.name}" if last else "?"
        self._ctx.log(
            "script_error",
            error_type=type(exc).__name__,
            error=str(exc),
            at=where,
        )
        self._ctx.stop("script_error")
