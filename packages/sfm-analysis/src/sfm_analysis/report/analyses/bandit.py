"""analyses/bandit.py — two_armed_bandit trial-level metrics.

The task (see experiment/templates/two_armed_bandit.py): both arms run
the identical dispense motion every trial, but only one is baited. This
means ``taken_node`` (logged by ``bandit_trial_end``) is almost always
the baited arm — it confirms retrieval, it is *not* the animal's choice.

The choice is which arm the animal visits first once both arms have
presented. ``build_trials`` derives ``first_visit_node`` (first
MousePresence Detected after the later of the two ``arm_presented`` rows)
and ``first_dome_node`` (same, for the first dome opening) — every
downstream metric here takes a ``choice_source`` option
(``"first_visit"`` default, ``"first_dome"``, ``"taken"``) so an analyst
can see all three and their agreement rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..session import RunData
from ..stats import wilson_ci

ChoiceSource = str  # "first_visit" | "first_dome" | "taken"


@dataclass
class BanditTrial:
    trial: int
    block: Optional[int] = None
    rich: Optional[int] = None
    lean: Optional[int] = None
    fed: Optional[int] = None
    empty: Optional[int] = None
    t_start: Optional[float] = None
    t_presented: Optional[float] = None
    t_end: Optional[float] = None
    delivered_node: Optional[int] = None
    taken_node: Optional[int] = None
    baited_arms: Optional[int] = None
    outcome: Optional[str] = None
    valid: Optional[int] = None
    invalid_reason: Optional[str] = None
    first_visit_node: Optional[int] = None
    first_visit_t: Optional[float] = None
    first_dome_node: Optional[int] = None
    first_dome_t: Optional[float] = None

    @property
    def is_analyzed(self) -> bool:
        """Excluded from choice statistics unless valid and a visit occurred."""
        return self.valid == 1 and self.first_visit_node is not None


def choice_node(bt: BanditTrial, source: ChoiceSource = "first_visit") -> Optional[int]:
    if source == "first_dome":
        return bt.first_dome_node
    if source == "taken":
        return bt.taken_node
    return bt.first_visit_node


def rewarded(bt: BanditTrial, source: ChoiceSource = "first_visit") -> Optional[bool]:
    c = choice_node(bt, source)
    if c is None or bt.fed is None:
        return None
    return c == bt.fed


def build_trials(run: RunData, visit_grace_s: float = 2.0, default_trial_window_s: float = 60.0) -> List[BanditTrial]:
    """
    Join bandit_trial / arm_presented / bandit_trial_end / bandit_trial_invalid
    rows into one BanditTrial per trial number, and derive first_visit_node /
    first_dome_node from the presence/dome stream after both arms presented.
    """
    trials: Dict[int, BanditTrial] = {}

    def get(trial_no: int) -> BanditTrial:
        return trials.setdefault(trial_no, BanditTrial(trial=trial_no))

    for row in run.exp("bandit_trial"):
        f = row.fields
        trial_no = int(f.get("trial", row.trial))
        bt = get(trial_no)
        bt.block = f.get("block")
        bt.rich = f.get("rich")
        bt.lean = f.get("lean")
        bt.fed = f.get("fed")
        bt.empty = f.get("empty")
        bt.t_start = row.t

    presented_by_trial: Dict[int, List[float]] = {}
    for row in run.exp("arm_presented"):
        trial_no = int(row.fields.get("trial", row.trial))
        presented_by_trial.setdefault(trial_no, []).append(row.t)
    for trial_no, ts in presented_by_trial.items():
        get(trial_no).t_presented = max(ts)

    for row in run.exp("bandit_trial_end"):
        f = row.fields
        trial_no = int(f.get("trial", row.trial))
        bt = get(trial_no)
        bt.delivered_node = f.get("delivered_node")
        bt.taken_node = f.get("taken_node")
        bt.baited_arms = f.get("baited_arms")
        bt.outcome = f.get("outcome")
        bt.valid = f.get("valid")
        bt.invalid_reason = f.get("invalid_reason")
        bt.t_end = row.t
        for k in ("block", "rich", "lean", "fed", "empty"):
            if getattr(bt, k) is None and k in f:
                setattr(bt, k, f[k])

    for row in run.exp("bandit_trial_invalid"):
        f = row.fields
        trial_no = int(f.get("trial", row.trial))
        bt = get(trial_no)
        if bt.valid is None:
            bt.valid = f.get("valid", 0)
        if bt.invalid_reason is None:
            bt.invalid_reason = f.get("invalid_reason")
        if bt.baited_arms is None:
            bt.baited_arms = f.get("baited_arms")
        if bt.t_end is None:
            bt.t_end = row.t

    ordered = [trials[k] for k in sorted(trials.keys())]

    for bt in ordered:
        if bt.t_presented is None:
            continue
        window_end = (bt.t_end if bt.t_end is not None else bt.t_presented + default_trial_window_s) + visit_grace_s

        presence_rows = [
            r for r in run.rows if r.frame_type == "EVENT"
            and r.event_name == "MousePresence Detected"
            and bt.t_presented < r.t <= window_end
        ]
        if presence_rows:
            first = min(presence_rows, key=lambda r: r.t)
            bt.first_visit_node = first.node_id
            bt.first_visit_t = first.t

        dome_rows = [
            r for r in run.rows if r.frame_type == "EVENT"
            and r.event_name == "Dome Opened"
            and bt.t_presented < r.t <= window_end
        ]
        if dome_rows:
            first = min(dome_rows, key=lambda r: r.t)
            bt.first_dome_node = first.node_id
            bt.first_dome_t = first.t

    return ordered


@dataclass
class ValiditySummary:
    n_total: int
    n_valid: int
    n_analyzed: int   # valid AND a visit was observed
    invalid_reasons: Dict[str, int] = field(default_factory=dict)
    choice_agreement: Dict[str, float] = field(default_factory=dict)  # e.g. "first_visit_vs_taken"


def validity_summary(trials: List[BanditTrial]) -> ValiditySummary:
    n_total = len(trials)
    n_valid = sum(1 for t in trials if t.valid == 1)
    n_analyzed = sum(1 for t in trials if t.is_analyzed)
    reasons: Dict[str, int] = {}
    for t in trials:
        if t.invalid_reason:
            reasons[t.invalid_reason] = reasons.get(t.invalid_reason, 0) + 1

    agreement = {}
    pairs = [("first_visit", "first_dome"), ("first_visit", "taken"), ("first_dome", "taken")]
    for a, b in pairs:
        comparable = [t for t in trials if choice_node(t, a) is not None and choice_node(t, b) is not None]
        if comparable:
            matches = sum(1 for t in comparable if choice_node(t, a) == choice_node(t, b))
            agreement[f"{a}_vs_{b}"] = matches / len(comparable)

    return ValiditySummary(n_total=n_total, n_valid=n_valid, n_analyzed=n_analyzed,
                            invalid_reasons=reasons, choice_agreement=agreement)


@dataclass
class BlockPoint:
    block: int
    n: int
    k: int
    p: float
    lo: float
    hi: float


def block_curve(trials: List[BanditTrial], choice_source: ChoiceSource = "first_visit") -> List[BlockPoint]:
    by_block: Dict[int, List[BanditTrial]] = {}
    for bt in trials:
        if not bt.is_analyzed or bt.block is None:
            continue
        by_block.setdefault(bt.block, []).append(bt)

    out = []
    for block in sorted(by_block):
        bts = by_block[block]
        n = len(bts)
        k = sum(1 for bt in bts if choice_node(bt, choice_source) == bt.rich)
        p, lo, hi = wilson_ci(k, n)
        out.append(BlockPoint(block=block, n=n, k=k, p=p, lo=lo, hi=hi))
    return out


@dataclass
class WSLSResult:
    win_stay_k: int
    win_stay_n: int
    lose_shift_k: int
    lose_shift_n: int

    @property
    def win_stay(self):
        return wilson_ci(self.win_stay_k, self.win_stay_n)

    @property
    def lose_shift(self):
        return wilson_ci(self.lose_shift_k, self.lose_shift_n)


def win_stay_lose_shift(
    trials: List[BanditTrial],
    choice_source: ChoiceSource = "first_visit",
    bridge_invalid: bool = False,
) -> WSLSResult:
    """
    Win-stay / lose-shift over consecutive analyzed trials.

    The chain breaks at any gap (an invalid/omitted trial in between) unless
    ``bridge_invalid`` is set, in which case the nearest previous analyzed
    trial is used regardless of the gap size.
    """
    analyzed = [t for t in trials if t.is_analyzed]
    ws_k = ws_n = ls_k = ls_n = 0
    prev: Optional[BanditTrial] = None

    for bt in analyzed:
        if prev is not None:
            adjacent = bridge_invalid or (bt.trial == prev.trial + 1)
            if adjacent:
                c_prev = choice_node(prev, choice_source)
                c_curr = choice_node(bt, choice_source)
                prev_rewarded = rewarded(prev, choice_source)
                if c_prev is not None and c_curr is not None and prev_rewarded is not None:
                    if prev_rewarded:
                        ws_n += 1
                        if c_curr == c_prev:
                            ws_k += 1
                    else:
                        ls_n += 1
                        if c_curr != c_prev:
                            ls_k += 1
        prev = bt

    return WSLSResult(win_stay_k=ws_k, win_stay_n=ws_n, lose_shift_k=ls_k, lose_shift_n=ls_n)


@dataclass
class ReversalPoint:
    rel: int
    n: int
    k: int
    p: float
    lo: float
    hi: float


def _flip_trial_numbers(trials: List[BanditTrial]) -> List[int]:
    analyzed = [t for t in trials if t.is_analyzed and t.block is not None]
    flips = []
    prev_block = None
    for bt in analyzed:
        if prev_block is not None and bt.block != prev_block:
            flips.append(bt.trial)
        prev_block = bt.block
    return flips


def reversal_curve(
    trials: List[BanditTrial], choice_source: ChoiceSource = "first_visit", pre: int = 5, post: int = 15
) -> List[ReversalPoint]:
    """Average P(choose new-rich arm) at trial positions relative to each block flip."""
    by_trial = {t.trial: t for t in trials if t.is_analyzed}
    flips = _flip_trial_numbers(trials)
    buckets: Dict[int, List[bool]] = {}

    for flip_trial in flips:
        for rel in range(-pre, post + 1):
            bt = by_trial.get(flip_trial + rel)
            if bt is None or bt.rich is None:
                continue
            buckets.setdefault(rel, []).append(choice_node(bt, choice_source) == bt.rich)

    out = []
    for rel in sorted(buckets):
        vals = buckets[rel]
        n = len(vals)
        k = sum(vals)
        p, lo, hi = wilson_ci(k, n)
        out.append(ReversalPoint(rel=rel, n=n, k=k, p=p, lo=lo, hi=hi))
    return out


def trials_to_criterion(
    trials: List[BanditTrial], choice_source: ChoiceSource = "first_visit",
    streak: int = 3, max_post: int = 15,
) -> List[Optional[int]]:
    """
    Per flip, the smallest post-flip relative position where `streak`
    consecutive analyzed trials chose the new rich arm. None if no such
    streak occurs within max_post trials after the flip.
    """
    by_trial = {t.trial: t for t in trials if t.is_analyzed}
    flips = _flip_trial_numbers(trials)
    out: List[Optional[int]] = []

    for flip_trial in flips:
        run_len = 0
        found: Optional[int] = None
        for rel in range(0, max_post + 1):
            bt = by_trial.get(flip_trial + rel)
            if bt is None or bt.rich is None:
                run_len = 0
                continue
            if choice_node(bt, choice_source) == bt.rich:
                run_len += 1
                if run_len >= streak and found is None:
                    found = rel - streak + 1
                    break
            else:
                run_len = 0
        out.append(found)
    return out


def side_bias(trials: List[BanditTrial], choice_source: ChoiceSource = "first_visit") -> Optional[float]:
    """P(chose arm_a) — a value near 1.0 or 0.0 invariant across blocks means
    the animal isn't tracking the reward contingency at all."""
    analyzed = [t for t in trials if t.is_analyzed]
    if not analyzed:
        return None
    arm_a = min((t.rich if t.rich is not None else t.lean) for t in analyzed if t.rich is not None or t.lean is not None)
    chosen_a = sum(1 for t in analyzed if choice_node(t, choice_source) == arm_a)
    return chosen_a / len(analyzed)
