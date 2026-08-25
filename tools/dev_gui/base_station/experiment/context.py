"""
context.py — ExperimentControl (the `control` argument) passed into every
@exp.on_* callback and @exp.script generator.

Provides actions (dispense, recover, BNC pulse), timers (after / every),
named counters, elapsed time, per-node sensor state, and experiment-level
logging.
"""

from __future__ import annotations

import csv
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence, TypeVar, Union

from sfm_analysis.logs import EXP6_HEADER as _EXP6_HEADER

from ..protocol import CanCmd, build_setconfig_heartbeat
from .events import EventKind, NodeEvent
from .script import _as_node_tuple, _Await, _AwaitKind, _node_suffix, _resolve_event_kind, _until_label

if TYPE_CHECKING:
    from ..can_manager import CanManager
    from ..io_manager import IOManager


TimerCallback = Callable[[], None]
T = TypeVar("T")

# Events that count as "the animal did something" for quiet_for(). Deliberately
# excludes HEARTBEAT (arrives on the configured interval; firmware boots at
# ~5s until the base station pushes SetConfig) and the node's
# own phase events (SEEKING/LOWERING/LOADING/RAISING/DWELLING/ON_PLATE/LOADED/
# NO_FEED_PRESENTED), which are caused by our own dispense command, not the
# animal.
_ACTIVITY_KINDS = frozenset(
    {
        EventKind.PG_CHANGED,
        EventKind.DOME_OPENED,
        EventKind.DOME_CLOSED,
        EventKind.PRESENCE_CHANGED,
        EventKind.PELLET_TAKEN,
        EventKind.BNC_IN,
    }
)


@dataclass
class _NodeView:
    """Per-node sensor snapshot, mirrored from the normalized event stream."""

    pellet: bool = False
    load_position: bool = False
    dome_open: bool = False
    presence: bool = False
    online: bool = False
    last_event_ts: float = 0.0
    last_activity_ts: float = 0.0
    last_kind: Optional[EventKind] = None
    # What the most recently COMPLETED dispense cycle on this node raised:
    # a real pellet (LOADED), an empty plate (NO_FEED_PRESENTED), or neither
    # yet (a cycle is in flight, or none has run this session). At most one
    # of these two is ever True — see ExperimentControl.observe_event().
    presented_pellet: bool = False
    presented_empty: bool = False
    # True from the moment dispense()/dispense(feed=False) actually sends a
    # command until the cycle resolves (LOADED / NO_FEED_PRESENTED /
    # FEED_SKIPPED / FAULT / NODE_OFFLINE) — see ExperimentControl.dispense()
    # and observe_event(). Distinct from `pellet`: a cycle in flight has NOT
    # yet reached the plate (ON_PLATE hasn't fired), so `pellet` alone can't
    # catch "this node is still mid-motion from a previous command."
    dispensing: bool = False


@dataclass
class _Timer:
    fire_at: float
    callback: TimerCallback
    interval: Optional[float] = None  # None = one-shot; else repeating
    cancelled: bool = False
    node: int = 0  # 0 = not node-scoped; else tied to a node's program


@dataclass
class ExperimentLogEntry:
    """One experiment-level log row (distinct from raw CAN LogEntry)."""

    timestamp: float
    name: str
    node_id: int = 0
    fields: Dict[str, Any] = field(default_factory=dict)

    @property
    def timestamp_iso(self) -> str:
        return datetime.fromtimestamp(self.timestamp).isoformat(timespec="milliseconds")


class ExperimentControl:
    """
    Surface available to user callbacks and @exp.script generators — the
    ``control`` argument (an ``event`` argument follows it in @exp.on_*
    callbacks).

    Constructed by ExperimentRunner and passed as the first argument to
    every registered handler. Actions go through CanManager / IOManager;
    timers and counters live on this object.
    """

    # Schema now lives in sfm_analysis.logs.EXP6_HEADER so this writer and
    # sfm_analysis.report.loader's schema-sniffing (which reads this exact
    # header) cannot drift apart across the two distributions.
    CSV_HEADER = _EXP6_HEADER

    def __init__(
        self,
        nodes: List[int],
        can: Optional["CanManager"] = None,
        io: Optional["IOManager"] = None,
        log_dir: Optional[str] = None,
        session_name: str = "experiment",
        seed: Optional[int] = None,
    ) -> None:
        self.nodes: List[int] = list(nodes)
        self._can = can
        self._io = io
        self._start_time: Optional[float] = None
        self._now: float = time.time()  # updated each runner step
        self._counters: Dict[str, int] = {}
        self._timers: List[_Timer] = []
        self._log_entries: List[ExperimentLogEntry] = []
        # Nodes latched into a fault; dispense() is a no-op for them until
        # an operator recovers the node (see halt_node / recover_node).
        self._halted: set = set()
        self._csv_file = None
        self._csv_writer = None
        self._log_path: Optional[Path] = None
        self._session_name = session_name
        self._log_dir = log_dir
        # Optional sink so a GUI host can mirror experiment log rows.
        self.on_log: Optional[Callable[[ExperimentLogEntry], None]] = None
        # Fired once, right after the "session_start" log row, when the
        # session actually activates (immediately if the template has no
        # start_when, later otherwise) — lets a GUI host fire a one-shot
        # side effect (e.g. the camera sync flash) at the true start moment
        # rather than at the operator's click.
        self.on_session_start: Optional[Callable[[], None]] = None
        # Commands issued during the session (for tests / inspection).
        self.commands_sent: List[tuple] = []
        # Set by stop(); the runner ends the session on the next end-check.
        self._stop_requested: bool = False
        self._stop_reason: str = ""

        # Hook for ScriptScheduler: fired from halt_node() so a pending
        # `wait_for(..., node=X)` in a @exp.script aborts when X faults.
        self.on_node_halted: Optional[Callable[[int], None]] = None

        # Per-node sensor mirror, fed by observe_event() from every dispatched
        # NodeEvent (not from EventNormalizer._NodeTrack, which only sees real
        # CAN frames and is blind to runner.inject() — the mechanism tests and
        # @exp.script development both rely on).
        self._node_views: Dict[int, _NodeView] = {}

        # Reproducible RNG for @exp.script tasks (control.chance/pick/shuffled).
        # A seed is always generated and logged, even if the author didn't
        # supply one, so every session can be replayed after the fact.
        self.seed: int = int(seed) if seed is not None else random.randrange(2**32)
        self._rng = random.Random(self.seed)

        # Trial counter for @exp.script tasks (control.next_trial() / .trial).
        self._trial: int = 0

    # ------------------------------------------------------------------
    # Lifecycle (called by runner)
    # ------------------------------------------------------------------

    def bind(self, can: Optional["CanManager"], io: Optional["IOManager"] = None) -> None:
        self._can = can
        if io is not None:
            self._io = io

    def set_now(self, now: float) -> None:
        """Advance the context clock (called by the runner each step)."""
        self._now = now

    def begin(self, now: Optional[float] = None) -> None:
        """Mark session start and open the experiment CSV if configured."""
        self._now = now if now is not None else time.time()
        self._start_time = self._now
        # Seed each node's activity timestamp at session start so quiet_for(N)
        # is False for the first N seconds rather than trivially true at t=0.
        for n in self.nodes:
            self._view(n).last_activity_ts = self._now
        self._open_csv()

    def end(self) -> None:
        """Close the experiment CSV."""
        self._close_csv()

    def stop(self, reason: str = "stopped") -> None:
        """
        Request that the runner end the session as soon as possible.

        Cancels pending timers immediately so reload/dispense callbacks
        cannot fire after a fault or other stop condition.
        """
        if self._stop_requested:
            return
        self._stop_requested = True
        self._stop_reason = str(reason)
        self.cancel_all_timers()
        self.log("stop_requested", reason=self._stop_reason)

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def dispense(self, node: int, *, feed: bool = True) -> bool:
        """
        Send Dispense (or, with ``feed=False``, DispenseNoFeed) to one node.

        DispenseNoFeed runs the identical dispense motion — lower, seek the
        load position, raise — but M1 never turns: the node holds at the drop
        position and raises an empty plate. Use it to run a module through the
        motions with no pellet — e.g. so a two-armed task can activate both
        arms every trial and the animal can't use sound or vibration alone to
        find the baited one.

        **The raise is triggered by a peer node, not by a timer.** A no-feed
        node holds at the drop position until it hears another node's Raising
        event on the bus, then raises within one CAN frame time (~0.5 ms). It
        has to work that way: a fed arm's hold lasts however long M1 takes to
        drop a pellet plus a 2 s confirm window, which varies trial to trial,
        so any fixed dwell would land the two plates at the top at visibly
        different moments — the exact cue a no-feed cycle exists to remove.

        A node whose peer never raises (hopper empty, jam) holds at the drop
        position indefinitely rather than presenting out of sync; it stays
        there until ``recover()``. ``kit.synchronized_cycle`` does that
        automatically when an arm fails to present.

        Three standardized, top-priority vetoes apply here — automatically,
        for every template, with no per-template wiring — since this is the
        single choke point every ``dispense(...)`` call in the codebase goes
        through:

        1. **Halted.** No-op (logged) while the node is halted by a fault —
           templates can keep calling ``dispense(n)`` unconditionally; a
           faulted node simply stops receiving pellets until recovered.
        2. **Cycle already in flight.** No-op (logged as a warning) while a
           previous ``dispense()``/``dispense(feed=False)`` on this node
           hasn't resolved yet (see ``is_dispensing()``) — this is what
           stops a fast timer/BNC trigger from re-picking the SAME node
           while its last command is still mid-motion (before a pellet even
           reaches the plate, so the plate-occupied check below can't see
           it yet).
        3. **Pellet already on the plate.** No-op (logged as a warning) when
           this node's own sensor mirror already shows a pellet on the
           plate — a real pellet was never delivered twice, and a plate
           that's already occupied when a dispense is requested is treated
           as an unexpected/"strange" state worth flagging, not something to
           silently retry motion for.

        Firmware's own occupancy guard (``DispenserService::dispense()`` /
        ``dispenseNoFeed()`` — see FeedSkipped) remains as a backstop for the
        small race between these client-side checks and the command reaching
        the bus.
        """
        if node in self._halted:
            self.log("dispense_skipped_halted", node=node)
            return False
        if self.is_dispensing(node):
            self.log("dispense_skipped_in_flight", node=node, warning=1, feed=feed)
            return False
        if self.pellet_on_plate(node):
            self.log(
                "dispense_skipped_pellet_present", node=node, warning=1,
                feed=feed,
            )
            return False
        ok = self._send(node, CanCmd.Dispense if feed else CanCmd.DispenseNoFeed)
        if ok:
            self._view(node).dispensing = True
        return ok

    def recover(self, node: int) -> bool:
        """Send Recover to one node."""
        return self._send(node, CanCmd.Recover)

    def broadcast_dispense(self) -> bool:
        """
        Send Dispense to all session nodes.

        Sends one true broadcast frame (synchronized arrival) when every node
        is clear — no pellet on its plate AND no cycle already in flight (see
        ``dispense()``'s standardized vetoes). If any node isn't clear, the
        single-frame broadcast can't selectively withhold the command from
        just that node — so this falls back to individual ``dispense(n)``
        calls (each carrying its own vetoes) and returns False, matching
        ``dispense()``'s "not clear = no command sent" contract for at least
        one node.
        """
        blocked = [
            n for n in self.nodes
            if self.pellet_on_plate(n) or self.is_dispensing(n)
        ]
        if blocked:
            self.log("broadcast_dispense_partial_pellet_present", warning=1, occupied=blocked)
            ok = True
            for n in self.nodes:
                ok = self.dispense(n) and ok
            return ok
        ok = self._send(0, CanCmd.Dispense)
        if ok:
            for n in self.nodes:
                self._view(n).dispensing = True
        return ok

    def broadcast_recover(self) -> bool:
        """Send Recover to all nodes (broadcast)."""
        return self._send(0, CanCmd.Recover)

    def set_heartbeat_interval(self, node: int, ms: int) -> bool:
        """SetConfig HeartbeatInterval for one node."""
        payload = build_setconfig_heartbeat(ms)
        return self._send(node, CanCmd.SetConfig, payload)

    def bnc_pulse(self, duration_us: int = 100) -> None:
        """Pulse BNC OUT. No-op when no IOManager is bound."""
        if self._io is not None:
            self._io.pulse_bnc_out(duration_us)
        self.log("bnc_pulse", duration_us=duration_us)

    # ------------------------------------------------------------------
    # Per-node fault handling (sticky)
    # ------------------------------------------------------------------

    def halt_node(self, node_id: int) -> None:
        """
        Latch ``node_id`` into a halted state after a fault.

        Cancels the node's pending timers (so a scheduled reload cannot fire)
        and makes ``dispense(node_id)`` a no-op until ``recover_node``. Other
        nodes are unaffected — the rest of the experiment keeps running.
        """
        if node_id in self._halted:
            return
        self._halted.add(node_id)
        self.cancel_node_timers(node_id)
        self.log("node_halted", node=node_id)
        if self.on_node_halted is not None:
            self.on_node_halted(node_id)

    def recover_node(self, node_id: int) -> None:
        """
        Clear a node's halted state and clear its firmware fault.

        Sends ``Recover`` to the node (firmware ``recover()`` resets the fault to
        Idle) and un-latches it so ``dispense`` works again. The runner fires
        ``on_recover`` handlers afterwards so a template can re-arm the node.
        Also clears the "cycle in flight" veto defensively — a FAULT event
        should already have cleared it, but a node whose fault predates this
        experiment session (never saw the FAULT event) must not stay
        veto-locked out of dispense() after being recovered.
        """
        self._halted.discard(node_id)
        self._view(node_id).dispensing = False
        self._send(node_id, CanCmd.Recover)
        self.log("node_recovered", node=node_id)

    def is_halted(self, node_id: int) -> bool:
        return node_id in self._halted

    @property
    def halted_nodes(self) -> List[int]:
        return sorted(self._halted)

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def after(self, seconds: float, callback: TimerCallback, node: int = 0) -> _Timer:
        """
        Schedule a one-shot callback after ``seconds`` (uses runner clock).

        Pass ``node`` to tie the timer to a node's program so it is cancelled
        automatically if that node faults (``halt_node``).
        """
        timer = _Timer(
            fire_at=self._now + max(0.0, float(seconds)), callback=callback, node=node
        )
        self._timers.append(timer)
        return timer

    def every(self, seconds: float, callback: TimerCallback, node: int = 0) -> _Timer:
        """Schedule a repeating callback every ``seconds`` (uses runner clock)."""
        interval = max(0.001, float(seconds))
        timer = _Timer(
            fire_at=self._now + interval, callback=callback, interval=interval, node=node
        )
        self._timers.append(timer)
        return timer

    def cancel_timer(self, timer: _Timer) -> None:
        timer.cancelled = True

    def cancel_node_timers(self, node_id: int) -> None:
        """Cancel every pending timer tied to ``node_id``."""
        for timer in self._timers:
            if timer.node == node_id:
                timer.cancelled = True
        self._timers = [t for t in self._timers if not t.cancelled]

    def cancel_all_timers(self) -> None:
        """Cancel every pending one-shot / repeating timer."""
        for timer in self._timers:
            timer.cancelled = True
        self._timers = []

    def tick_timers(self, now: float) -> None:
        """Fire due timers. Called by the runner each step."""
        if self._stop_requested:
            return
        self._now = now
        due = [t for t in self._timers if not t.cancelled and t.fire_at <= now]
        for timer in due:
            try:
                timer.callback()
            except Exception as exc:  # noqa: BLE001 — user callbacks
                self.log("timer_error", error=str(exc))
            if timer.interval is not None and not timer.cancelled:
                timer.fire_at = now + timer.interval
            else:
                timer.cancelled = True
        self._timers = [t for t in self._timers if not t.cancelled]

    # ------------------------------------------------------------------
    # Counters / elapsed
    # ------------------------------------------------------------------

    def counter(self, name: str) -> int:
        """Return the current value of a named counter (default 0)."""
        return self._counters.get(name, 0)

    def incr(self, name: str, amount: int = 1) -> int:
        """Increment a named counter and return the new value."""
        self._counters[name] = self._counters.get(name, 0) + amount
        return self._counters[name]

    def set_counter(self, name: str, value: int) -> None:
        self._counters[name] = int(value)

    def elapsed(self) -> float:
        """Seconds since session begin (uses runner clock). 0.0 before begin()."""
        if self._start_time is None:
            return 0.0
        return max(0.0, self._now - self._start_time)

    # ------------------------------------------------------------------
    # Sequential scripts (@exp.script) — awaitables
    # ------------------------------------------------------------------
    #
    # These are pure constructors: they build an _Await and do nothing else.
    # All arming happens in ScriptScheduler at the moment the object comes
    # back out of a `yield` — constructing one without yielding it is
    # harmless.

    def wait_for(
        self,
        kind: Union[str, EventKind],
        node: Union[int, Sequence[int], None] = None,
        timeout: Optional[float] = None,
    ) -> _Await:
        """
        Wait for the next matching event.

        ``kind`` is an EventKind or its lower-case name (e.g. "pellet_taken").
        ``node`` restricts to one node id or a list of ids; omit for any node.
        """
        ek = kind if isinstance(kind, EventKind) else _resolve_event_kind(str(kind))
        nodes = _as_node_tuple(node)
        return _Await(
            kind=_AwaitKind.EVENT,
            event_kind=ek,
            nodes=nodes,
            timeout=timeout,
            label=f"{ek.name.lower()}{_node_suffix(nodes)}",
        )

    def wait_until(
        self,
        predicate: Callable[["ExperimentControl"], bool],
        timeout: Optional[float] = None,
        node: Union[int, Sequence[int], None] = None,
        label: Optional[str] = None,
    ) -> _Await:
        """
        Wait until ``predicate(control)`` returns True (polled every tick).

        Pass ``node`` only if this condition should also abort when that node
        faults; otherwise it is purely time/state-driven.

        ``label`` is what ``script_stalled`` / ``script_timeout`` /
        ``script_await_aborted`` print as ``waiting_on``. Give lambdas a
        short name (``"plates_clear"``, ``"fault_recovery"``) — otherwise
        an unlabeled lambda is logged as ``condition``, not ``<lambda>``.
        Named helper functions keep their ``__name__`` if ``label`` is omitted.
        """
        nodes = _as_node_tuple(node)
        return _Await(
            kind=_AwaitKind.UNTIL,
            predicate=predicate,
            nodes=nodes,
            timeout=timeout,
            label=_until_label(predicate, nodes, label),
        )

    def wait(self, seconds: float) -> _Await:
        """Wait a fixed duration on the runner clock."""
        s = float(seconds)
        if s <= 0:
            return _Await(kind=_AwaitKind.TICK, label="tick")
        return _Await(kind=_AwaitKind.DELAY, seconds=s, label=f"wait({s:g}s)")

    # ------------------------------------------------------------------
    # Sequential scripts (@exp.script) — per-node state
    # ------------------------------------------------------------------

    def observe_event(self, ev: NodeEvent) -> None:
        """
        Mirror one dispatched NodeEvent into per-node sensor state.

        Called by the runner for every event in a dispatch batch (real CAN,
        simulator, or runner.inject() — all three go through this, unlike
        EventNormalizer._NodeTrack which only sees real CAN frames).
        """
        if ev.node_id == 0:
            return
        view = self._view(ev.node_id)
        view.last_event_ts = ev.timestamp
        if ev.kind in _ACTIVITY_KINDS:
            view.last_activity_ts = ev.timestamp
        view.last_kind = ev.kind

        if ev.kind is EventKind.PG_CHANGED:
            gate = ev.data.get("gate")
            active = bool(ev.data.get("active"))
            if gate == "pellet":
                view.pellet = active
            elif gate == "load_position":
                view.load_position = active
            elif gate == "dome":
                view.dome_open = active
        elif ev.kind is EventKind.DOME_OPENED:
            view.dome_open = True
        elif ev.kind is EventKind.DOME_CLOSED:
            view.dome_open = False
        elif ev.kind is EventKind.PRESENCE_CHANGED:
            view.presence = bool(ev.data.get("active"))
        elif ev.kind is EventKind.ON_PLATE:
            # Pellet sensor confirmed during Loading — the raise hasn't
            # happened yet, so this is NOT a completed presentation. Do not
            # touch presented_pellet/presented_empty here.
            view.pellet = True
        elif ev.kind is EventKind.LOADED:
            # Raise finished with a real pellet at the top.
            view.presented_pellet, view.presented_empty = True, False
            view.dispensing = False
        elif ev.kind is EventKind.NO_FEED_PRESENTED:
            # Raise finished with an empty plate at the top (a no-feed /
            # mimic cycle — see ExperimentControl.dispense(feed=False)).
            view.presented_pellet, view.presented_empty = False, True
            view.dispensing = False
        elif ev.kind is EventKind.PELLET_TAKEN:
            # Deliberately does NOT clear presented_pellet/presented_empty.
            # "Presented" describes the outcome of the last COMPLETED cycle,
            # and stays true — through the take — until a new cycle starts.
            # This matters for gating a response window on multiple nodes
            # (see presentation_done()): an animal can legitimately take a
            # fast-presenting arm's pellet before a slower arm finishes
            # raising, and that must not un-resolve the first arm's
            # "finished presenting" reading. Call clear_presentation()
            # explicitly at the start of a new trial instead.
            view.pellet = False
        elif ev.kind in (
            EventKind.SEEKING, EventKind.LOWERING, EventKind.LOADING,
            EventKind.DWELLING, EventKind.RAISING,
        ):
            # A new cycle starting means whatever was presented before is no
            # longer current. DOME_OPENED is deliberately NOT in this list —
            # a dome bout on an already-presented plate (pellet or empty)
            # must not erase what was actually presented. `dispensing` is
            # NOT cleared here — the cycle is still in flight through these
            # phases; see LOADED/NO_FEED_PRESENTED/FEED_SKIPPED/FAULT.
            view.presented_pellet = view.presented_empty = False
        elif ev.kind in (EventKind.FEED_SKIPPED, EventKind.FAULT):
            # Terminal without a normal raise: FeedSkipped means firmware
            # never ran a motion (plate already occupied), and a fault aborts
            # whatever was in progress — either way no further completion
            # event is coming for this cycle, so clear `dispensing` here too
            # (not just presented_*) or the node would stay veto-locked out
            # of every future dispense() call forever.
            view.presented_pellet = view.presented_empty = False
            view.dispensing = False
        elif ev.kind in (EventKind.NODE_ONLINE, EventKind.HEARTBEAT):
            view.online = True
            if ev.kind is EventKind.HEARTBEAT:
                view.pellet = bool(ev.data.get("pellet", view.pellet))
                view.load_position = bool(ev.data.get("load_position", view.load_position))
                view.dome_open = bool(ev.data.get("dome_open", view.dome_open))
                view.presence = bool(ev.data.get("mouse_presence", view.presence))
        elif ev.kind is EventKind.NODE_OFFLINE:
            # A lost node's in-flight cycle will never resolve on its own —
            # clear `dispensing` so it isn't stuck vetoing dispense() forever
            # if/when the node reconnects.
            view.online = False
            view.dispensing = False

    def quiet_for(self, seconds: float, node: Union[int, Sequence[int], None] = None) -> bool:
        """True if no activity (PG/dome/presence/pellet-taken/BNC) on the
        given node(s) — or all session nodes, if omitted — for ``seconds``."""
        cutoff = self._now - max(0.0, float(seconds))
        targets = self._node_list(node)
        return all(self._view(n).last_activity_ts <= cutoff for n in targets)

    def domes_closed(self, nodes: Union[int, Sequence[int], None] = None) -> bool:
        targets = self._node_list(nodes)
        return all(not self._view(n).dome_open for n in targets)

    def dome_open(self, node: int) -> bool:
        return self._view(node).dome_open

    def pellet_on_plate(self, node: int) -> bool:
        return self._view(node).pellet

    def is_dispensing(self, node: int) -> bool:
        """
        True from the moment ``dispense(node)`` sends a command until that
        cycle resolves (a real pellet, an empty plate, FeedSkipped, a fault,
        or the node going offline). See ``dispense()`` — this is the
        standardized "cycle already in flight" veto every ``dispense()`` call
        checks, regardless of template.
        """
        return self._view(node).dispensing

    def pellet_clear(self, nodes: Union[int, Sequence[int], None] = None) -> bool:
        """True when no pellet is on the plate on every given node (all
        session nodes if omitted). Mirrors ``presence_clear()``/
        ``domes_closed()`` — pellet state, not presence, is the ground truth
        for whether it's safe to dispense again."""
        targets = self._node_list(nodes)
        return all(not self._view(n).pellet for n in targets)

    def is_online(self, node: int) -> bool:
        return self._view(node).online

    # ------------------------------------------------------------------
    # Presentation state: what did the last completed dispense cycle raise?
    # ------------------------------------------------------------------
    #
    # Reusable primitives for writing your own trial-based template — e.g. a
    # two-armed task that needs to know which arm actually delivered versus
    # which one only ran the motion (control.dispense(node, feed=False)).

    def presented_pellet(self, node: int) -> bool:
        """True if this node's last completed dispense cycle raised a REAL pellet."""
        return self._view(node).presented_pellet

    def presented_empty(self, node: int) -> bool:
        """True if this node's last completed cycle raised an EMPTY plate
        (a ``dispense(node, feed=False)`` / no-feed cycle) — motion only,
        nothing delivered."""
        return self._view(node).presented_empty

    def presentation(self, node: int) -> str:
        """``'pellet'`` | ``'empty'`` | ``'none'`` — ``'none'`` while a cycle
        is in flight (or before any cycle has completed this session)."""
        view = self._view(node)
        if view.presented_pellet:
            return "pellet"
        if view.presented_empty:
            return "empty"
        return "none"

    def presentation_done(self, node: int) -> bool:
        """True once this node's cycle has finished raising, either way (a
        real pellet OR an empty plate). Stays True even after the pellet is
        taken — so an animal that grabs a fast-presenting arm's pellet before
        a slower arm finishes raising can't un-resolve this — until you call
        ``clear_presentation(node)`` or a new cycle starts. Use ``wait_until``
        on this across several nodes to gate a response window on ALL of them
        being ready before acting on any — e.g. so neither arm of a
        two-armed task tips off the animal by finishing first."""
        view = self._view(node)
        return view.presented_pellet or view.presented_empty

    def clear_presentation(self, node: int) -> None:
        """Reset this node's presentation state. Call at the start of a new
        trial so a stale reading from the previous cycle can't be mistaken
        for this one's result before the new cycle completes.

        Also releases the in-flight dispense veto. A multi-node cycle aborted
        by a fault on one node never delivers LOADED/NO_FEED_PRESENTED on the
        siblings — without this, those siblings would stay veto-locked out of
        ``dispense()`` forever after Recover.
        """
        view = self._view(node)
        view.presented_pellet = False
        view.presented_empty = False
        view.dispensing = False

    # ------------------------------------------------------------------
    # Mouse presence (capacitive pad)
    # ------------------------------------------------------------------

    def presence(self, node: int) -> bool:
        """True if the animal is on this node's capacitive presence pad.

        Defaults to False until the first heartbeat or InputChanged for this
        node arrives, exactly like ``pellet_on_plate`` / ``dome_open`` — gate
        on ``is_online(node)`` first if you need to distinguish "confirmed
        clear" from "not yet known".
        """
        return self._view(node).presence

    def presence_clear(self, nodes: Union[int, Sequence[int], None] = None) -> bool:
        """True when presence is clear on every given node (all session
        nodes if omitted). Mirrors ``domes_closed()``; see ``presence()``
        for the same before-first-heartbeat caveat."""
        targets = self._node_list(nodes)
        return all(not self._view(n).presence for n in targets)

    def _node_list(self, node: Union[int, Sequence[int], None]) -> List[int]:
        if node is None:
            return self.nodes
        if isinstance(node, int):
            return [node]
        return list(node)

    def _view(self, node_id: int) -> _NodeView:
        view = self._node_views.get(node_id)
        if view is None:
            view = _NodeView()
            self._node_views[node_id] = view
        return view

    # ------------------------------------------------------------------
    # Sequential scripts (@exp.script) — trials + RNG
    # ------------------------------------------------------------------

    def next_trial(self) -> int:
        """Advance and return the trial counter (also mirrored in counter('trials'))."""
        self._trial += 1
        self.set_counter("trials", self._trial)
        self.log("trial", trial=self._trial)
        return self._trial

    @property
    def trial(self) -> int:
        """Current trial number (0 before the first next_trial() call)."""
        return self._trial

    def chance(self, p: float) -> bool:
        """True with probability ``p`` (session-seeded RNG; see .seed)."""
        return self._rng.random() < max(0.0, min(1.0, float(p)))

    def pick(self, seq: Sequence[T]) -> T:
        """Pick one item uniformly at random (session-seeded RNG)."""
        return self._rng.choice(list(seq))

    def shuffled(self, seq: Iterable[T]) -> List[T]:
        """Return a shuffled copy of ``seq`` (session-seeded RNG)."""
        out = list(seq)
        self._rng.shuffle(out)
        return out

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log(self, name: str, node: int = 0, **fields: Any) -> None:
        """Append an experiment-level log entry (and write to CSV if open)."""
        ts = self._now
        entry = ExperimentLogEntry(
            timestamp=ts,
            name=name,
            node_id=node,
            fields=dict(fields),
        )
        self._log_entries.append(entry)
        if self._csv_writer is not None:
            field_str = " ".join(f"{k}={v}" for k, v in fields.items())
            self._csv_writer.writerow(
                [
                    entry.timestamp_iso,
                    int(ts * 1000),
                    f"{self.elapsed():.3f}",
                    name,
                    node,
                    field_str,
                ]
            )
            if self._csv_file is not None:
                self._csv_file.flush()
        if self.on_log is not None:
            try:
                self.on_log(entry)
            except Exception:  # noqa: BLE001 — GUI sink must not break callbacks
                pass

    @property
    def log_entries(self) -> List[ExperimentLogEntry]:
        return list(self._log_entries)

    @property
    def log_path(self) -> Optional[Path]:
        return self._log_path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send(self, node_id: int, cmd: CanCmd, payload: bytes = b"") -> bool:
        self.commands_sent.append((node_id, cmd, payload))
        self.log(
            "command",
            node=node_id,
            cmd=cmd.name,
            payload_hex=payload.hex() if payload else "",
        )
        if self._can is None:
            return True  # dry-run / test mode
        return self._can.send_command(node_id, cmd, payload)

    def _open_csv(self) -> None:
        if not self._log_dir:
            return
        log_dir = Path(self._log_dir).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in self._session_name)
        self._log_path = log_dir / f"experiment_{safe_name}_{stamp}.csv"
        self._csv_file = open(self._log_path, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(self.CSV_HEADER)

    def _close_csv(self) -> None:
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None


# Old name, kept as an alias — existing code and tests import ExperimentContext.
ExperimentContext = ExperimentControl
