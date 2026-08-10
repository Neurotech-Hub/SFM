"""
kit.py — Small building blocks shared by the built-in templates.

Not a framework layer templates must use — just the boilerplate that was
being copy-pasted between fixed_and_random.py and probability_delivery.py
(and part of free_feeding.py): the nodes/duration/pellet-cap Experiment
tail, the timer-or-BNC trigger dispatch, and fault/recover logging.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional, Sequence

from .runner import Experiment

if TYPE_CHECKING:
    from .context import ExperimentControl

CycleFn = Callable[["ExperimentControl"], None]


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


def on_trigger(
    exp: Experiment,
    trigger: str,
    interval_s: float,
    bnc_channel: int,
    cycle_fn: CycleFn,
) -> None:
    """
    Wire a periodic timer or BNC-edge trigger to ``cycle_fn(control)``.

    ``trigger`` is "timer" (fires immediately, then every ``interval_s``) or
    "bnc" (fires on a rising edge on ``bnc_channel``). Increments the
    ``cycles`` counter once per firing before calling ``cycle_fn``.
    """

    def _cycle(control) -> None:
        control.incr("cycles")
        cycle_fn(control)

    @exp.on_start
    def _start(control) -> None:
        if trigger == "timer":
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
      "presence_clear" — wait until presence is clear on ``nodes`` (all
                         session nodes if omitted), and — if ``quiet_s`` > 0 —
                         stays clear with no dome/sensor/presence activity for
                         that long. See ``ExperimentControl.presence_clear``.

    An unrecognized mode falls back to "fixed_delay" so a stale saved
    parameter can never crash a session outright.

    In "presence_clear" mode the wait is scoped to ``nodes``, so a fault on
    one of them aborts the wait instead of hanging the trial loop forever —
    the same behavior ``control.wait_until(..., node=...)`` already gives
    you. It has **no timeout by design**: an animal that parks on the pad
    stalls the trial loop until the session's own ``end_after()``
    duration/pellet cap or an operator Stop ends things. That stall is
    visible, not silent — the scheduler logs ``script_stalled`` every 120s
    while it waits.
    """
    if mode == "presence_clear":
        def _ready(c) -> bool:
            if not c.presence_clear(nodes):
                return False
            if quiet_s > 0 and not c.quiet_for(quiet_s, nodes):
                return False
            return True

        return control.wait_until(_ready, node=nodes)
    return control.wait(max(0.0, float(delay_s)))
