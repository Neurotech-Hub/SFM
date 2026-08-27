"""
pellet_ledger.py — session-scoped pellet accounting, owned by the base station.

Node firmware counts pellets from power-on and never resets: ``pelletCount_``
and ``takenCount_`` are zeroed in the ``DispenserService`` constructor and by
nothing else, not even ``recover()``. A session opened on a node that has been
powered all morning therefore logs its first pellet as number 48, which is
meaningless to whoever analyses that run.

This module owns the number that goes in the log and into analysis. It starts
at zero when a run opens and counts what happened during that run.

Why the node counter is still read
----------------------------------
Counting only the frames the base station happens to receive would be *less*
reliable, not more: a dropped CAN frame silently loses a pellet and nothing
ever notices. The node's monotonic counter is the only independent witness.

So every frame that carries a counter is treated as a witness, and the session
total advances by the **delta** in the node's counter rather than by one per
event. A dropped ``Loaded`` frame is corrected by the next frame that carries
the counter — the following ``DomeOpened``, ``PelletTaken``, or simply the next
heartbeat — and the gap is reported so it lands in the log instead of vanishing.

Which counter each frame carries (see ``SFM::sendDispenseEvent``):

* ``PelletTaken`` carries ``takenCount_``; only it advances the taken counter.
* Every other count-carrying event carries ``pelletCount_`` (pellets presented);
  only ``Loaded`` advances it — the rest report it without incrementing.
* Heartbeats carry both, and are the recovery path for a node that reconnects
  mid-session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from .protocol import CanEvent

# Node counters are LE16 on the wire, so they wrap here.
_COUNTER_WRAP = 1 << 16

# A forward delta larger than this is not a run of missed frames — it is a node
# that power-cycled and restarted its counter, read through modular arithmetic.
# Missing 100 consecutive frames from one node would mean the bus was down for
# most of an hour, in which case the heartbeat gap is the real story.
_MAX_PLAUSIBLE_GAP = 100

# Events carrying pelletCount_ (pellets presented). PelletTaken is deliberately
# absent: it carries takenCount_ instead.
_PRESENTED_EVENTS = frozenset({
    CanEvent.OnPlate, CanEvent.Loaded, CanEvent.DomeOpened, CanEvent.FeedSkipped,
    CanEvent.Seeking, CanEvent.Lowering, CanEvent.Loading, CanEvent.Raising,
    CanEvent.NoFeedPresented, CanEvent.Dwelling,
})


@dataclass
class Reconciliation:
    """What one witness did to a counter."""

    total: int              # session total after this witness
    delta: int              # how far the node counter advanced
    missed: int             # frames that must have been dropped to explain it
    restarted: bool = False # node counter went backwards → power cycle

    @property
    def had_gap(self) -> bool:
        return self.missed > 0


@dataclass
class _Counter:
    """One monotonic node counter, tracked against a session total."""

    total: int = 0
    last_node: Optional[int] = None
    missed: int = 0
    restarts: int = 0

    def witness(self, node_value: int, increments: bool) -> Reconciliation:
        """
        Fold one observation of the node's counter into the session total.

        ``increments`` says whether this particular frame is the one that
        advances the node counter (``Loaded`` / ``PelletTaken``) or is merely
        reporting it. That is what separates "the node counted a pellet we saw"
        from "the node counted a pellet whose frame we lost".
        """
        expected = 1 if increments else 0

        if self.last_node is None:
            # First sighting of this node in this run. An incrementing frame is
            # itself the run's first event, so baseline one below it; any other
            # frame reports a count that accrued before the run opened.
            self.last_node = (node_value - expected) % _COUNTER_WRAP

        delta = (node_value - self.last_node) % _COUNTER_WRAP
        restarted = False

        if delta > _MAX_PLAUSIBLE_GAP:
            # Backwards through the wrap: the node rebooted and restarted at 0.
            # Re-baseline rather than inventing tens of thousands of pellets.
            restarted = True
            self.restarts += 1
            delta = expected

        self.last_node = node_value
        self.total += delta

        missed = max(0, delta - expected)
        self.missed += missed
        return Reconciliation(
            total=self.total, delta=delta, missed=missed, restarted=restarted
        )


@dataclass
class NodeTally:
    """Read-only view of one node's session accounting."""

    presented: int = 0
    taken: int = 0
    missed_presented: int = 0
    missed_taken: int = 0
    restarts: int = 0


class PelletLedger:
    """
    Per-run, per-node pellet accounting.

    ``reset()`` at the start of every run — the totals it hands out are what
    the log and the report call "pellet 1", "pellet 2", and so on.
    """

    def __init__(self) -> None:
        self._presented: Dict[int, _Counter] = {}
        self._taken: Dict[int, _Counter] = {}

    def reset(self) -> None:
        """Start a fresh run. Every node's session count returns to zero."""
        self._presented.clear()
        self._taken.clear()

    # ------------------------------------------------------------------
    # Witnesses
    # ------------------------------------------------------------------

    def witness_event(
        self, node_id: int, event: CanEvent, node_value: int
    ) -> Optional[Reconciliation]:
        """
        Fold in the counter carried by one CAN event.

        Returns None for events that carry no counter, so callers can use the
        result as "was this a pellet-bearing frame?".
        """
        if event == CanEvent.PelletTaken:
            counter = self._taken.setdefault(node_id, _Counter())
            return counter.witness(node_value, increments=True)
        if event in _PRESENTED_EVENTS:
            counter = self._presented.setdefault(node_id, _Counter())
            return counter.witness(node_value, increments=(event == CanEvent.Loaded))
        return None

    def witness_heartbeat(
        self, node_id: int, presented: int, taken: int
    ) -> Dict[str, Reconciliation]:
        """
        Fold in both counters from a heartbeat.

        Heartbeats never increment anything themselves, so any advance they
        reveal is a frame the base station never received. This is the recovery
        path for a node that was offline or noisy for a stretch.
        """
        return {
            "presented": self._presented.setdefault(node_id, _Counter())
            .witness(presented, increments=False),
            "taken": self._taken.setdefault(node_id, _Counter())
            .witness(taken, increments=False),
        }

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def presented(self, node_id: int) -> int:
        c = self._presented.get(node_id)
        return c.total if c else 0

    def taken(self, node_id: int) -> int:
        c = self._taken.get(node_id)
        return c.total if c else 0

    def tally(self, node_id: int) -> NodeTally:
        p = self._presented.get(node_id)
        t = self._taken.get(node_id)
        return NodeTally(
            presented=p.total if p else 0,
            taken=t.total if t else 0,
            missed_presented=p.missed if p else 0,
            missed_taken=t.missed if t else 0,
            restarts=(p.restarts if p else 0) + (t.restarts if t else 0),
        )

    def node_ids(self) -> list:
        return sorted(set(self._presented) | set(self._taken))

    def summary(self) -> Dict[int, NodeTally]:
        """Every node this run touched, for a session-close summary row."""
        return {nid: self.tally(nid) for nid in self.node_ids()}
