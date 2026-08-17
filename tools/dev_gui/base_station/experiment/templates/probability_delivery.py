"""
probability_delivery.py — Each trigger delivers a pellet on ONE node, chosen by
weighted probability.

Example: for a 3-node setup with weights 20/30/50 each cycle picks node 1 ~20%
of the time, node 2 ~30%, node 3 ~50% — an independent weighted-random draw
every cycle (not a fixed split). Weights are normalized, so they need not sum
to 100; a weight of 0 means that node is never picked (inactive). In the GUI
weights come from a per-node % input (a dict); headless callers may pass a
comma-separated string mapped to ``nodes`` in order.

The trigger / next-trial wait is the shared sequential kit loop: pause while
any node is faulted, wait for plates to clear, pick one weight > 0 node to
feed (the rest of those nodes mimic with ``DispenseNoFeed`` when ``mimic`` is
on), wait for every commanded node to present, wait for the take, then
``fixed_delay`` / ``presence_clear`` / ``bnc``. Weight 0 stays idle.

Usage::

    from base_station.experiment.templates.probability_delivery import build

    exp = build(nodes=[1, 2, 3], probabilities="20,30,50",
                next_trial_wait="fixed_delay", fixed_delay_s=10.0)
    exp.run(interface="vcan0")
"""

from __future__ import annotations

import random
from typing import Any, List, Optional, Sequence

from .. import kit
from ..runner import Experiment


def _weights_for_nodes(spec: Any, node_list: Sequence[int]) -> List[float]:
    """
    Resolve a weight spec into one non-negative weight per node in ``node_list``.

    ``spec`` may be:
      * a dict ``{node_id: weight}`` (from the GUI's per-node % inputs);
      * a comma-separated string ``"20,80"`` mapped to ``node_list`` in order;
      * a sequence of numbers mapped to ``node_list`` in order.
    Missing entries default to an equal share; an all-zero result → uniform.
    """
    n = len(node_list)
    weights: List[float] = []
    if isinstance(spec, dict):
        for node_id in node_list:
            try:
                weights.append(max(0.0, float(spec.get(node_id, 0.0))))
            except (TypeError, ValueError):
                weights.append(0.0)
    else:
        if isinstance(spec, str):
            parts = [p.strip() for p in spec.split(",") if p.strip() != ""]
        elif isinstance(spec, (list, tuple)):
            parts = list(spec)
        else:
            parts = []
        for p in parts:
            try:
                weights.append(max(0.0, float(p)))
            except (TypeError, ValueError):
                weights.append(0.0)
        if len(weights) < n:
            weights += [1.0] * (n - len(weights))  # pad missing with equal share
        weights = weights[:n]
    if sum(weights) <= 0:
        weights = [1.0] * n  # all-zero → uniform
    return weights


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "probability_delivery",
    probabilities: Any = "",
    next_trial_wait: Optional[str] = None,
    trigger: Optional[str] = None,           # legacy alias of next_trial_wait
    fixed_delay_s: Optional[float] = None,
    interval_s: Optional[float] = None,      # legacy alias of fixed_delay_s
    iti_quiet_s: float = 1.0,
    bnc_channel: int = 0,
    mimic: bool = True,
    hours: float = 0.0,
    minutes: float = 0.0,
    seconds: float = 0.0,
    max_pellets: Optional[int] = None,
    seed: Optional[int] = None,
) -> Experiment:
    """
    Build a probability-based single-delivery Experiment.

    Parameters
    ----------
    nodes:
        Node IDs to use. Defaults to [1, 2, 3].
    probabilities:
        Per-node weights. A dict ``{node_id: weight}`` (GUI), a comma-separated
        string ``"20,80"`` mapped to ``nodes`` in order, or a sequence of
        numbers. A weight of 0 means that node is never picked. Empty = uniform.
    trigger / next_trial_wait:
        "fixed_delay" (wait ``fixed_delay_s``), "presence_clear" (wait until
        the animal is off the pads), or "bnc" (BNC IN rising edge). Legacy
        ``trigger="timer"`` is an alias of ``fixed_delay``.
    fixed_delay_s / interval_s:
        Seconds between cycles when the mode is ``fixed_delay``.
        ``interval_s`` is the legacy alias.
    iti_quiet_s:
        When mode is ``presence_clear``, seconds presence must stay clear
        before the next cycle.
    bnc_channel:
        BNC IN channel (0 = first, 1 = second) when mode is ``bnc``.
    mimic:
        When True (default), every other weight > 0 node runs a no-feed
        dispense so the animal cannot use sound or motion to find the pellet.
        Weight 0 stays idle. When False, only the fed node is commanded.
    seed:
        Optional RNG seed for reproducible runs (tests). Falsy = nondeterministic.
    """
    exp = kit.session(name, nodes, hours=hours, minutes=minutes, seconds=seconds, max_pellets=max_pellets)
    rng = random.Random(seed) if seed else random.Random()
    weights = _weights_for_nodes(probabilities, exp.nodes)
    advance, delay_s = kit.resolve_advance(
        next_trial_wait=next_trial_wait, trigger=trigger,
        fixed_delay_s=fixed_delay_s, interval_s=interval_s,
        default_delay_s=10.0, allow_bnc=True,
    )
    mimic = bool(mimic)

    def _pick(control):
        node_list = list(control.nodes)
        w = weights[: len(node_list)]
        active = [n for n, wt in zip(node_list, w) if wt > 0]
        if not active:
            control.log("probability_pick", node=0, fed=[], mimic=[])
            return (), ()
        target = rng.choices(node_list, weights=w, k=1)[0]
        mimics = [n for n in active if n != target] if mimic else []
        control.log("probability_pick", node=target, fed=[target], mimic=list(mimics))
        return (target,), mimics

    @exp.on_start
    def _log_start(control):
        control.log(
            "probability_delivery_start",
            nodes=control.nodes, weights=weights, next_trial_wait=advance,
            mimic=int(mimic),
        )

    kit.wire_synchronized_loop(
        exp, _pick,
        advance=advance, delay_s=delay_s, quiet_s=iti_quiet_s,
        bnc_channel=bnc_channel, mimic=mimic,
    )
    kit.log_faults(exp)
    kit.log_cycle_summary(exp, "probability_delivery_end")

    return exp
