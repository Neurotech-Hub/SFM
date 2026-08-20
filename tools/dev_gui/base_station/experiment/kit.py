"""
kit.py — Small building blocks shared by the built-in templates.

Not a framework layer templates must use — just the boilerplate that was
being copy-pasted between templates: the nodes/duration/pellet-cap Experiment
tail, the shared next-trial wait, session-wide fault pause, plate-clear gate,
and synchronized feed/mimic cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, Optional, Sequence, Tuple, Union

from .runner import Experiment

if TYPE_CHECKING:
    from .context import ExperimentControl

CycleFn = Callable[["ExperimentControl"], None]
PickFn = Callable[["ExperimentControl"], Tuple[Sequence[int], Sequence[int]]]
CycleCallback = Callable[["CycleResult"], None]

# Canonical "advance to next trial" modes (two-armed bandit). Cycle templates
# also accept "bnc". "timer" is a legacy alias of "fixed_delay".
ADVANCE_MODES = ("fixed_delay", "presence_clear")
ADVANCE_MODES_WITH_BNC = ADVANCE_MODES + ("bnc",)
_MODE_ALIASES = {"timer": "fixed_delay"}

# Mechanical safeguard for the presentation sync gate (not a behavioral timeout).
# Comfortably above the firmware's worst-case feed timeout (30s) plus travel.
PRESENTATION_TIMEOUT_S = 90.0


@dataclass
class CycleResult:
    """Outcome of ``synchronized_cycle`` — templates log/branch on this."""

    ok: bool = False
    faulted: bool = False
    faulted_node: int = 0
    invalid: bool = False
    invalid_reason: str = ""
    taken_node: int = 0
    fed: Tuple[int, ...] = ()
    mimic_nodes: Tuple[int, ...] = ()
    accepted: Dict[int, bool] = field(default_factory=dict)


def _int_tuple(nodes: Union[int, Sequence[int], None]) -> Tuple[int, ...]:
    if nodes is None:
        return ()
    if isinstance(nodes, int):
        return (int(nodes),)
    return tuple(int(n) for n in nodes)


def _target_nodes(control, nodes: Union[int, Sequence[int], None]) -> Tuple[int, ...]:
    if nodes is None:
        return tuple(control.nodes)
    return _int_tuple(nodes)


def session(
    name: str,
    nodes: Optional[Sequence[int]] = None,
    *,
    hours: float = 0.0,
    minutes: float = 0.0,
    seconds: float = 0.0,
    max_pellets: Optional[int] = None,
) -> Experiment:
    """Build an Experiment with the standard nodes/duration/pellet-cap tail."""
    node_list = list(nodes) if nodes else [1, 2, 3]
    exp = Experiment(nodes=node_list, name=name)
    exp.end_after(hours=hours, minutes=minutes, seconds=seconds, pellets=max_pellets)
    return exp


def resolve_advance(
    *,
    next_trial_wait: Optional[str] = None,
    trigger: Optional[str] = None,
    fixed_delay_s: Optional[float] = None,
    interval_s: Optional[float] = None,
    reload_delay_s: Optional[float] = None,
    default_delay_s: float = 5.0,
    allow_bnc: bool = False,
) -> Tuple[str, float]:
    """
    Normalize the shared advance/trigger parameters.

    Canonical kwargs are ``next_trial_wait`` (``fixed_delay`` / ``presence_clear``
    / optionally ``bnc``) and ``fixed_delay_s``. Legacy aliases still work:
    ``trigger="timer"`` → ``fixed_delay``, ``trigger="bnc"`` → ``bnc``,
    ``interval_s`` / ``reload_delay_s`` → ``fixed_delay_s``.
    """
    raw = next_trial_wait if next_trial_wait not in (None, "") else trigger
    mode = str(raw or "presence_clear")
    mode = _MODE_ALIASES.get(mode, mode)
    allowed = ADVANCE_MODES_WITH_BNC if allow_bnc else ADVANCE_MODES
    if mode not in allowed:
        mode = "fixed_delay"
    delay = default_delay_s
    for value in (fixed_delay_s, interval_s, reload_delay_s):
        if value is not None:
            delay = float(value)
            break
    return mode, max(0.0, delay)


def on_trigger(
    exp: Experiment,
    trigger: str,
    interval_s: float,
    bnc_channel: int,
    cycle_fn: CycleFn,
) -> None:
    """
    Wire a periodic timer or BNC-edge trigger to ``cycle_fn(control)``.

    ``trigger`` is "timer" / "fixed_delay" (fires immediately, then every
    ``interval_s``) or "bnc" (fires on a rising edge on ``bnc_channel``).
    Increments the ``cycles`` counter once per firing before calling
    ``cycle_fn``. Prefer ``wire_advance`` for new templates — it also covers
    ``presence_clear``.
    """
    trigger, interval_s = resolve_advance(
        trigger=trigger, interval_s=interval_s, allow_bnc=True,
    )

    def _cycle(control) -> None:
        control.incr("cycles")
        cycle_fn(control)

    @exp.on_start
    def _start(control) -> None:
        if trigger == "fixed_delay":
            _cycle(control)  # fire an immediate first cycle
            control.every(max(0.001, float(interval_s)), lambda: _cycle(control))

    @exp.on_bnc_in
    def _bnc(control, event) -> None:
        if trigger != "bnc":
            return
        if event.data.get("edge") != "rising":
            return
        if event.data.get("channel") not in (None, bnc_channel):
            return
        _cycle(control)


def wire_advance(
    exp: Experiment,
    mode: str,
    cycle_fn: CycleFn,
    *,
    delay_s: float = 5.0,
    bnc_channel: int = 0,
    quiet_s: float = 1.0,
    nodes: Optional[Sequence[int]] = None,
) -> None:
    """
    Drive ``cycle_fn`` with the shared next-trial wait used by every template.

    ``mode``:
      ``fixed_delay``    — fire immediately, then wait ``delay_s`` between cycles
      ``presence_clear`` — fire immediately, then wait until plates + presence
                           are clear on ``nodes`` (and quiet for ``quiet_s``)
      ``bnc``            — fire on a BNC IN rising edge on ``bnc_channel``
                           (no immediate first cycle; same as the old trigger)
    ``timer`` is accepted as an alias of ``fixed_delay``. Increments
    ``cycles`` once per firing.
    """
    mode, delay_s = resolve_advance(
        next_trial_wait=mode, fixed_delay_s=delay_s, allow_bnc=True,
    )

    if mode == "bnc":
        on_trigger(exp, "bnc", delay_s, bnc_channel, cycle_fn)
        return

    @exp.script
    def run(control) -> None:
        wait_nodes = tuple(nodes) if nodes is not None else tuple(control.nodes)
        while True:
            control.incr("cycles")
            cycle_fn(control)
            yield next_trial_wait(
                control, mode,
                delay_s=delay_s, nodes=wait_nodes, quiet_s=quiet_s,
            )


def after_advance(
    control,
    mode: str,
    callback: Callable[[], None],
    *,
    node: Optional[int] = None,
    delay_s: float = 5.0,
    quiet_s: float = 1.0,
    session_pause: bool = False,
) -> None:
    """
    Event-based counterpart of ``next_trial_wait`` — schedule ``callback``
    after the shared ITI (free_feeding reloads one node at a time, so it
    cannot ``yield`` a script wait).

    Node-scoped: a fault on ``node`` cancels a pending reload.
    ``session_pause``: while ANY session node is halted, keep polling instead
    of firing — the whole rig waits for Recover.
    """
    mode, delay_s = resolve_advance(next_trial_wait=mode, fixed_delay_s=delay_s)

    def _session_halted() -> bool:
        return session_pause and any(control.is_halted(n) for n in control.nodes)

    def _run() -> None:
        if control.stop_requested:
            return
        if node is not None and control.is_halted(node):
            return
        if _session_halted():
            control.after(0.1, _run, node=node)
            return
        callback()

    if mode != "presence_clear":
        if delay_s <= 0:
            _run()
        else:
            control.after(delay_s, _run, node=node)
        return

    def _poll() -> None:
        if control.stop_requested:
            return
        if node is not None and control.is_halted(node):
            return
        if _session_halted():
            control.after(0.1, _poll, node=node)
            return
        if not control.pellet_clear(node):
            control.after(0.1, _poll, node=node)
            return
        if node is not None and control.is_dispensing(node):
            control.after(0.1, _poll, node=node)
            return
        if not control.presence_clear(node):
            control.after(0.1, _poll, node=node)
            return
        if quiet_s > 0 and not control.quiet_for(quiet_s, node):
            control.after(0.1, _poll, node=node)
            return
        callback()

    _poll()


def log_faults(exp: Experiment) -> None:
    """Register the standard fault/recover logging (no re-arm logic)."""

    @exp.on_fault
    def _fault(control, event) -> None:
        control.log("fault", node=event.node_id, fault_code=event.data.get("fault_code"))

    @exp.on_recover
    def _recovered(control, event) -> None:
        control.log("recovered", node=event.node_id)


def log_feed_skipped(exp: Experiment) -> None:
    """
    Register standard FeedSkipped logging.

    FeedSkipped means a dispense (fed OR no-feed) arrived at an already
    occupied plate, so a REAL pellet was presented regardless of which
    command was sent — worth flagging distinctly in the log, since it's the
    one case where a "no-feed" cycle can still deliver.
    """

    @exp.on_feed_skipped
    def _skipped(control, event) -> None:
        control.log(
            "feed_skipped", node=event.node_id,
            note="plate was already occupied — a REAL pellet was presented",
        )


def log_cycle_summary(exp: Experiment, event_name: str) -> None:
    """Register the standard end-of-session summary for cycle-based templates."""

    @exp.on_end
    def _end(control) -> None:
        control.log(
            event_name,
            cycles=control.counter("cycles"),
            pellets=control.counter("pellets"),
            elapsed_s=round(control.elapsed(), 3),
        )


def next_trial_wait(
    control,
    mode: str,
    *,
    delay_s: float = 5.0,
    nodes: Optional[Sequence[int]] = None,
    quiet_s: float = 0.0,
):
    """
    Build the inter-trial wait for a sequential (``@exp.script``) trial-based
    template — ``yield`` the result:

        yield kit.next_trial_wait(control, next_trial_wait_mode,
                                  delay_s=fixed_delay_s, nodes=(arm_a, arm_b))

    ``mode`` is one of:
      "fixed_delay"    — wait ``delay_s`` seconds (default 5s).
      "presence_clear" — wait until no cycle is in flight, the plate is clear,
                         AND presence is clear on ``nodes`` (all session nodes
                         if omitted), and — if ``quiet_s`` > 0 — presence stays
                         clear with no dome/sensor/presence activity for that
                         long. Pellet state is checked first and
                         unconditionally: an animal walking off the pad never
                         counts as "ready" while a pellet is still sitting on
                         the plate — see ``ExperimentControl.pellet_clear`` /
                         ``presence_clear``.

    An unrecognized mode falls back to "fixed_delay" so a stale saved
    parameter can never crash a session outright. ``timer`` is accepted as
    an alias of ``fixed_delay``.

    In "presence_clear" mode the wait is scoped to ``nodes``, so a fault on
    one of them aborts the wait instead of hanging the trial loop forever —
    the same behavior ``control.wait_until(..., node=...)`` already gives
    you. It has **no timeout by design**: an animal that parks on the pad
    stalls the trial loop until the session's own ``end_after()``
    duration/pellet cap or an operator Stop ends things. That stall is
    visible, not silent — the scheduler logs ``script_stalled`` every 120s
    while it waits (``waiting_on=presence_clear``).
    """
    mode, delay_s = resolve_advance(
        next_trial_wait=mode, fixed_delay_s=delay_s, default_delay_s=delay_s,
    )
    if mode == "presence_clear":
        def _ready(c) -> bool:
            targets = c.nodes if nodes is None else (
                (nodes,) if isinstance(nodes, int) else tuple(nodes)
            )
            if any(c.is_dispensing(n) for n in targets):
                return False
            if not c.pellet_clear(targets):
                return False
            if not c.presence_clear(targets):
                return False
            if quiet_s > 0 and not c.quiet_for(quiet_s, targets):
                return False
            return True

        return control.wait_until(_ready, node=nodes, label="presence_clear")
    return control.wait(max(0.0, float(delay_s)))


def pause_while_faulted(control, nodes: Union[int, Sequence[int], None] = None):
    """
    Pause the WHOLE experiment while any of ``nodes`` is halted.

    Unscoped wait (no ``node=``) so this wait can never itself be
    fault-aborted — it is the thing watching for recovery. A no-op
    (yields nothing) on the common healthy path.

        yield from kit.pause_while_faulted(control)
    """
    targets = _target_nodes(control, nodes)
    halted = [n for n in targets if control.is_halted(n)]
    if not halted:
        return
    control.log("paused_for_fault", nodes=halted)
    yield control.wait_until(
        lambda c: not any(c.is_halted(n) for n in targets),
        label="fault_recovery",
    )
    control.log("resumed_after_fault")


def wait_plates_clear(control, nodes: Union[int, Sequence[int], None] = None):
    """
    Pause until every given node's plate is clear of a pellet.

    Pellet sensing outranks presence: call this before every trial so a
    leftover pellet blocks the next dispense instead of racing it. A no-op
    when plates are already clear.

        yield from kit.wait_plates_clear(control)
    """
    targets = _target_nodes(control, nodes)
    occupied = [n for n in targets if control.pellet_on_plate(n)]
    if not occupied:
        return
    control.log(
        "plate_occupied_wait", warning=1,
        occupied=occupied, action="waiting for plate(s) to clear",
    )
    yield control.wait_until(
        lambda c: not any(c.pellet_on_plate(n) for n in targets),
        node=targets,
        label="plates_clear",
    )
    control.log("plates_clear")


def wait_bnc_rising(control, channel: int = 0):
    """Wait for a BNC IN rising edge on ``channel`` (0 = first, 1 = second)."""
    while True:
        r = yield control.wait_for("bnc_in")
        if r.faulted:
            return r
        ev = r.event
        if ev is None:
            continue
        if ev.data.get("edge") != "rising":
            continue
        if ev.data.get("channel") not in (None, channel):
            continue
        return r


def synchronized_cycle(
    control,
    fed: Union[int, Sequence[int], None] = (),
    mimic_nodes: Union[int, Sequence[int], None] = (),
    *,
    dwell_s: Optional[float] = None,
    timeout: float = PRESENTATION_TIMEOUT_S,
    on_commanded: Optional[CycleCallback] = None,
    on_presented: Optional[CycleCallback] = None,
):
    """
    One synchronized multi-node cycle: feed ``fed``, no-feed ``mimic_nodes``,
    wait until every commanded node has presented, then wait for PelletTaken
    on the fed node(s).

        result = yield from kit.synchronized_cycle(control, fed=2, mimic_nodes=(1, 3))

    Idle nodes (not in either list) get no command. An empty ``fed`` with
    only mimics skips the take wait. Returns a ``CycleResult``.

    ``on_commanded`` fires after Dispense/DispenseNoFeed go out (or are
    vetoed); ``on_presented`` fires after the sync gate, before the take
    wait — so a sequential template can log mid-trial without duplicating
    the wait logic.
    """
    fed_t = _int_tuple(fed)
    mimic_t = tuple(n for n in _int_tuple(mimic_nodes) if n not in fed_t)
    commanded = fed_t + mimic_t
    result = CycleResult(fed=fed_t, mimic_nodes=mimic_t)
    if not commanded:
        result.ok = True
        return result

    for n in commanded:
        control.clear_presentation(n)
    accepted: Dict[int, bool] = {}
    for n in fed_t:
        accepted[n] = bool(control.dispense(n))
    for n in mimic_t:
        accepted[n] = bool(control.dispense(n, feed=False, dwell_s=dwell_s))
    result.accepted = accepted

    if not all(accepted.values()):
        failed = next(n for n, ok in accepted.items() if not ok)
        cause = "halted" if control.is_halted(failed) else "plate_occupied"
        result.invalid = True
        result.invalid_reason = cause
        if on_commanded is not None:
            on_commanded(result)
        return result

    if on_commanded is not None:
        on_commanded(result)

    ready = yield control.wait_until(
        lambda c: all(c.presentation_done(n) for n in commanded),
        node=commanded,
        timeout=timeout,
        label="arms_presented",
    )
    if ready.faulted:
        result.faulted = True
        result.faulted_node = ready.faulted_node
        return result
    if ready.timed_out:
        result.invalid = True
        result.invalid_reason = "arm_never_presented"
        return result

    baited_mimics = tuple(n for n in mimic_t if control.presented_pellet(n))
    if baited_mimics:
        control.incr("trials_invalid")
        result.invalid = True
        result.invalid_reason = "mimic_baited"

    if on_presented is not None:
        on_presented(result)

    watch: Union[int, Sequence[int]]
    if baited_mimics:
        watch = fed_t + baited_mimics
    else:
        watch = fed_t
    if not watch:
        result.ok = not result.invalid
        return result

    r = yield control.wait_for("pellet_taken", node=watch)
    if r.faulted:
        result.faulted = True
        result.faulted_node = r.faulted_node
        return result
    result.taken_node = r.event.node_id if r.event else 0
    result.ok = not result.invalid
    return result


def wire_synchronized_loop(
    exp: Experiment,
    pick_fn: PickFn,
    *,
    advance: str,
    delay_s: float = 5.0,
    quiet_s: float = 1.0,
    bnc_channel: int = 0,
    mimic: bool = True,
    dwell_s: Optional[float] = None,
) -> None:
    """
    Drive ``pick_fn`` through the shared sequential trial loop:

    pause while faulted → plates clear → (BNC wait) → pick fed/mimics →
    synchronized_cycle → next_trial_wait (unless BNC).

    ``pick_fn(control) -> (fed, mimic_nodes)``. When ``mimic`` is False the
    mimic list is dropped (fed nodes still run). Increments ``cycles`` and
    ``next_trial()`` once per loop.
    """
    advance, delay_s = resolve_advance(
        next_trial_wait=advance, fixed_delay_s=delay_s, allow_bnc=True,
    )

    @exp.script
    def run(control) -> None:
        nodes = tuple(control.nodes)
        while True:
            yield from pause_while_faulted(control, nodes)
            yield from wait_plates_clear(control, nodes)
            if advance == "bnc":
                r = yield from wait_bnc_rising(control, bnc_channel)
                if r is not None and getattr(r, "faulted", False):
                    continue
            control.incr("cycles")
            control.next_trial()
            fed, mimics = pick_fn(control)
            if not mimic:
                mimics = ()
            result = yield from synchronized_cycle(
                control, fed, mimics, dwell_s=dwell_s,
            )
            if result is not None and getattr(result, "faulted", False):
                continue
            if advance != "bnc":
                yield next_trial_wait(
                    control, advance,
                    delay_s=delay_s, nodes=nodes, quiet_s=quiet_s,
                )
