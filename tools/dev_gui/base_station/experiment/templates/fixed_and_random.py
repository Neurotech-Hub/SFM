"""
fixed_and_random.py — Per-node roles: each node is Off, Fixed, or Random.

On each trigger cycle:
  * every node whose role is ``fixed`` always dispenses;
  * every node whose role is ``random`` dispenses with probability ``random_prob``;
  * ``off`` nodes are skipped.

Roles come from ``node_roles``: a dict ``{node_id: "off"|"fixed"|"random"}``
(from the GUI's per-node dropdown). For headless use you may instead pass
``fixed_nodes`` (a comma-separated string or a sequence of ids) — those become
fixed and every other node in ``nodes`` becomes random.

The trigger is either a periodic timer (``interval_s``) or a BNC IN rising edge
on ``bnc_channel`` (0 = first BNC input, 1 = second). Faulted nodes are halted
by the engine, so their dispenses become no-ops until an operator Recover — no
special handling needed here.

Usage::

    from base_station.experiment.templates.fixed_and_random import build

    exp = build(nodes=[1, 2, 3],
                node_roles={1: "fixed", 2: "random", 3: "off"},
                trigger="timer", interval_s=10.0, random_prob=0.5)
    exp.run(interface="vcan0")
"""

from __future__ import annotations

import random
from typing import Any, Dict, Iterable, Optional, Sequence, Set, Union

from .. import kit
from ..runner import Experiment

NodeSpec = Union[str, Iterable[int], None]

OFF, FIXED, RANDOM = "off", "fixed", "random"


def _parse_node_ids(spec: NodeSpec) -> Set[int]:
    """Parse a comma-separated node-id string (or an iterable of ints) into a set."""
    if spec is None:
        return set()
    if isinstance(spec, (list, tuple, set, frozenset)):
        return {int(n) for n in spec}
    result: Set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def _resolve_roles(
    node_roles: Any, fixed_nodes: NodeSpec, node_list: Sequence[int]
) -> Dict[int, str]:
    """
    Resolve per-node roles for every node in ``node_list``.

    Prefers an explicit ``node_roles`` dict; otherwise derives roles from the
    legacy ``fixed_nodes`` spec (listed = fixed, others = random).
    """
    roles: Dict[int, str] = {}
    if isinstance(node_roles, dict) and node_roles:
        for n in node_list:
            role = str(node_roles.get(n, OFF)).strip().lower()
            roles[n] = role if role in (OFF, FIXED, RANDOM) else OFF
        return roles
    fixed_set = _parse_node_ids(fixed_nodes)
    for n in node_list:
        roles[n] = FIXED if n in fixed_set else RANDOM
    return roles


def build(
    nodes: Optional[Sequence[int]] = None,
    *,
    name: str = "fixed_and_random",
    node_roles: Any = None,
    fixed_nodes: NodeSpec = "",
    trigger: str = "timer",          # "timer" | "bnc"
    interval_s: float = 10.0,
    bnc_channel: int = 0,
    random_prob: float = 0.5,
    hours: float = 0.0,
    minutes: float = 0.0,
    seconds: float = 0.0,
    max_pellets: Optional[int] = None,
    seed: Optional[int] = None,
) -> Experiment:
    """
    Build a per-node-role Experiment.

    Parameters
    ----------
    nodes:
        Node IDs to use. Defaults to [1, 2, 3].
    node_roles:
        Dict ``{node_id: "off"|"fixed"|"random"}`` (GUI). Overrides ``fixed_nodes``.
    fixed_nodes:
        Legacy/headless fallback — nodes that dispense every cycle (string
        "1,3" or an iterable). Every other node becomes random.
    trigger:
        "timer" (every ``interval_s``) or "bnc" (BNC IN rising edge).
    interval_s:
        Cycle period in seconds when ``trigger == "timer"``.
    bnc_channel:
        BNC IN channel (0 = first, 1 = second) that triggers a cycle when
        ``trigger == "bnc"``.
    random_prob:
        Probability (0..1) that each ``random`` node dispenses per cycle.
    seed:
        Optional RNG seed for reproducible runs (tests). Falsy = nondeterministic.
    """
    exp = kit.session(name, nodes, hours=hours, minutes=minutes, seconds=seconds, max_pellets=max_pellets)
    rng = random.Random(seed) if seed else random.Random()
    prob = max(0.0, min(1.0, float(random_prob)))
    roles = _resolve_roles(node_roles, fixed_nodes, exp.nodes)

    def _cycle(control) -> None:
        for n in control.nodes:
            role = roles.get(n, OFF)
            if role == FIXED:
                control.dispense(n)
            elif role == RANDOM and rng.random() < prob:
                control.log("random_dispense", node=n)
                control.dispense(n)

    @exp.on_start
    def _log_start(control):
        control.log(
            "fixed_and_random_start",
            nodes=control.nodes, roles=roles,
            trigger=trigger, random_prob=prob,
        )

    kit.on_trigger(exp, trigger, interval_s, bnc_channel, _cycle)
    kit.log_faults(exp)
    kit.log_cycle_summary(exp, "fixed_and_random_end")

    return exp
