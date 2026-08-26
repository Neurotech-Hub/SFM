"""
fixed_and_random.py — Per-node roles: each node is Off, Fixed, or Random.

On each trigger cycle:
  * every node whose role is ``fixed`` always feeds;
  * among ``random`` nodes, with probability ``random_prob`` pick exactly one
    (uniform) to feed; otherwise none of them feed;
  * the other Random nodes mimic with ``DispenseNoFeed`` when ``mimic`` is on;
  * ``off`` nodes stay idle (no command).

Roles come from ``node_roles``: a dict ``{node_id: "off"|"fixed"|"random"}``
(from the GUI's per-node dropdown). You may instead pass
``fixed_nodes`` (a comma-separated string or a sequence of ids) — those become
fixed and every other node in ``nodes`` becomes random.

The trigger / next-trial wait is the shared sequential kit loop: pause while
any node is faulted, wait for plates to clear, command fed + mimic nodes,
wait for presentation then take, then ``fixed_delay`` / ``presence_clear`` /
``bnc``. A fault on any participating node pauses the whole session until
Recover.

Usage::

    from base_station.experiment.templates.fixed_and_random import build

    exp = build(nodes=[1, 2, 3],
                node_roles={1: "fixed", 2: "random", 3: "off"},
                next_trial_wait="fixed_delay", fixed_delay_s=10.0,
                random_prob=0.5)
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
    next_trial_wait: Optional[str] = None,
    trigger: Optional[str] = None,           # legacy alias of next_trial_wait
    fixed_delay_s: Optional[float] = None,
    interval_s: Optional[float] = None,      # legacy alias of fixed_delay_s
    iti_quiet_s: float = 1.0,
    bnc_channel: int = 0,
    random_prob: float = 0.5,
    mimic: bool = True,
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
        Legacy fallback — nodes that dispense every cycle (string
        "1,3" or an iterable). Every other node becomes random.
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
    random_prob:
        Probability (0..1) that one ``random`` node feeds this cycle.
        The winner is drawn uniformly from the Random pool; the rest mimic
        (or stay idle if ``mimic`` is off). 0 = no Random node feeds.
    mimic:
        When True (default), Random nodes that are not feeding still run a
        no-feed dispense. Off nodes stay idle. When False, only fed nodes
        are commanded.
    seed:
        Optional RNG seed for reproducible runs (tests). Falsy = nondeterministic.
    """
    exp = kit.session(name, nodes, hours=hours, minutes=minutes, seconds=seconds, max_pellets=max_pellets)
    rng = random.Random(seed) if seed else random.Random()
    prob = max(0.0, min(1.0, float(random_prob)))
    roles = _resolve_roles(node_roles, fixed_nodes, exp.nodes)
    advance, delay_s = kit.resolve_advance(
        next_trial_wait=next_trial_wait, trigger=trigger,
        fixed_delay_s=fixed_delay_s, interval_s=interval_s,
        default_delay_s=10.0, allow_bnc=True,
    )
    mimic = bool(mimic)

    def _pick(control):
        fixed = [n for n in control.nodes if roles.get(n) == FIXED]
        random_nodes = [n for n in control.nodes if roles.get(n) == RANDOM]
        fed = list(fixed)
        mimics = []
        if random_nodes:
            if rng.random() < prob:
                winner = rng.choice(random_nodes)
                fed.append(winner)
                control.log("random_dispense", node=winner)
                mimics = [n for n in random_nodes if n != winner]
            else:
                mimics = list(random_nodes)
        if not mimic:
            mimics = []
        return fed, mimics

    @exp.on_start
    def _log_start(control):
        control.log(
            "fixed_and_random_start",
            nodes=control.nodes, roles=roles,
            next_trial_wait=advance, random_prob=prob, mimic=int(mimic),
        )

    kit.wire_synchronized_loop(
        exp, _pick,
        advance=advance, delay_s=delay_s, quiet_s=iti_quiet_s,
        bnc_channel=bnc_channel, mimic=mimic,
    )
    kit.log_faults(exp)
    kit.log_cycle_summary(exp, "fixed_and_random_end")

    return exp
