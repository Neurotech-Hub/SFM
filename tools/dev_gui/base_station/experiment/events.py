"""
events.py — Normalized experiment event model.

Adapts raw CAN frames (via protocol.py) into NodeEvent objects that user
callbacks consume. Also derives higher-level events such as DOME_CLOSED
from dome sensor edge transitions and NODE_ONLINE/OFFLINE from heartbeats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional

from ..node_registry import DEFAULT_OFFLINE_TIMEOUT_S
from ..pellet_ledger import PelletLedger
from ..protocol import (
    CanEvent,
    InputId,
    ServiceStatus,
    classify_frame,
    node_id_from_event_id,
    node_id_from_hb_id,
    parse_event,
    parse_event_context,
    parse_fault_code,
    parse_heartbeat,
    parse_input_changed,
    parse_presence_cal,
)


class EventKind(Enum):
    """Normalized event kinds consumed by experiment callbacks."""

    # Direct CAN events
    ON_PLATE = auto()
    LOADED = auto()
    PELLET_TAKEN = auto()
    FEED_SKIPPED = auto()
    NO_FEED_PRESENTED = auto()  # empty plate raised to the top (no-feed cycle)
    FAULT = auto()
    SEEKING = auto()
    LOWERING = auto()
    LOADING = auto()
    DWELLING = auto()  # holding at the drop position, M1 idle (no-feed cycle)
    RAISING = auto()
    DOME_OPEN_WARNING = auto()
    PRESENCE_CHANGED = auto()
    PG_CHANGED = auto()
    HEARTBEAT = auto()
    PRESENCE_CAL_RESULT = auto()  # response to a CalibratePresence broadcast

    # Derived by the engine / also CanEvent.DomeOpened
    DOME_OPENED = auto()
    DOME_CLOSED = auto()
    NODE_ONLINE = auto()
    NODE_OFFLINE = auto()
    NODE_RECOVERED = auto()  # operator cleared a node's fault (see recover_node)

    # Base-station / session
    BNC_IN = auto()
    SESSION_START = auto()
    SESSION_END = auto()
    TIMER = auto()


# Map CanEvent → EventKind for the direct (non-InputChanged) events.
_CAN_EVENT_TO_KIND: Dict[CanEvent, EventKind] = {
    CanEvent.OnPlate: EventKind.ON_PLATE,
    CanEvent.Loaded: EventKind.LOADED,
    CanEvent.DomeOpened: EventKind.DOME_OPENED,
    CanEvent.PelletTaken: EventKind.PELLET_TAKEN,
    CanEvent.FeedSkipped: EventKind.FEED_SKIPPED,
    CanEvent.NoFeedPresented: EventKind.NO_FEED_PRESENTED,
    CanEvent.Fault: EventKind.FAULT,
    CanEvent.Seeking: EventKind.SEEKING,
    CanEvent.Lowering: EventKind.LOWERING,
    CanEvent.Loading: EventKind.LOADING,
    CanEvent.Dwelling: EventKind.DWELLING,
    CanEvent.Raising: EventKind.RAISING,
    CanEvent.DomeOpenWarning: EventKind.DOME_OPEN_WARNING,
    CanEvent.PresenceCalResult: EventKind.PRESENCE_CAL_RESULT,
}


@dataclass
class NodeEvent:
    """One normalized experiment event."""

    kind: EventKind
    node_id: int = 0  # 0 = session / base-station (BNC, SESSION_*)
    timestamp: float = 0.0
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _NodeTrack:
    """Per-node edge-tracking state used to derive higher-level events."""

    online: bool = False
    last_heartbeat: Optional[float] = None
    dome_open: bool = False
    presence: bool = False
    pellet: bool = False
    load_position: bool = False


class EventNormalizer:
    """
    Stateful adapter: CAN frames → list[NodeEvent].

    Tracks per-node dome / presence / online status so it can emit derived
    events (DOME_OPENED/CLOSED, NODE_ONLINE/OFFLINE).
    """

    def __init__(self, online_timeout_s: float = DEFAULT_OFFLINE_TIMEOUT_S) -> None:
        self._tracks: Dict[int, _NodeTrack] = {}
        self._online_timeout_s = online_timeout_s
        # Own one by default so a headless runner still reports session-scoped
        # numbers; the GUI replaces it with the ledger backing the log rows.
        self._pellets = PelletLedger()

    def set_online_timeout(self, seconds: float) -> None:
        """Update the silence window used by check_staleness / NODE_OFFLINE."""
        self._online_timeout_s = float(seconds)

    def set_pellet_ledger(self, ledger: PelletLedger) -> None:
        """
        Share the base station's ledger so callbacks and log rows agree.

        Both this normalizer and the GUI log path witness every frame into the
        shared ledger, in whichever order the dispatch happens to run. That is
        safe because the ledger advances by the *delta* in the node's counter:
        a second witness of the same frame sees a delta of zero and changes
        nothing. Reading the tally is therefore correct either way round.
        """
        self._pellets = ledger

    @property
    def pellets(self) -> PelletLedger:
        return self._pellets

    def _track(self, node_id: int) -> _NodeTrack:
        if node_id not in self._tracks:
            self._tracks[node_id] = _NodeTrack()
        return self._tracks[node_id]

    def frame_to_events(self, msg: Any, now: float) -> List[NodeEvent]:
        """
        Convert a python-can Message into zero or more NodeEvents.

        ``msg`` is expected to have ``.arbitration_id`` and ``.data``.
        """
        arb_id = int(msg.arbitration_id)
        data = bytes(msg.data)
        kind = classify_frame(arb_id)

        if kind == "HEARTBEAT":
            return self._from_heartbeat(arb_id, data, now)
        if kind == "EVENT":
            return self._from_event(arb_id, data, now)
        return []

    def check_staleness(self, now: float) -> List[NodeEvent]:
        """Emit NODE_OFFLINE for nodes that have gone silent."""
        out: List[NodeEvent] = []
        for node_id, track in self._tracks.items():
            if not track.online or track.last_heartbeat is None:
                continue
            if now - track.last_heartbeat > self._online_timeout_s:
                track.online = False
                out.append(
                    NodeEvent(
                        kind=EventKind.NODE_OFFLINE,
                        node_id=node_id,
                        timestamp=now,
                    )
                )
        return out

    def inject_bnc_in(
        self,
        channel: int,
        edge: str,
        now: float,
        high: bool = True,
    ) -> NodeEvent:
        """Build a BNC_IN event (called by the runner when GPIO fires)."""
        return NodeEvent(
            kind=EventKind.BNC_IN,
            node_id=0,
            timestamp=now,
            data={"channel": channel, "edge": edge, "high": high},
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _from_heartbeat(self, arb_id: int, data: bytes, now: float) -> List[NodeEvent]:
        node_id = node_id_from_hb_id(arb_id)
        if node_id is None:
            return []
        hb = parse_heartbeat(data)
        if hb is None:
            return []

        track = self._track(node_id)
        out: List[NodeEvent] = []

        # Recovery path: a heartbeat increments nothing, so any advance it
        # reveals is a frame this run never saw. Folding it in keeps the
        # session count right across a reconnect.
        self._pellets.witness_heartbeat(
            node_id, hb.pellets_presented, hb.pellets_taken
        )

        if not track.online:
            track.online = True
            out.append(
                NodeEvent(
                    kind=EventKind.NODE_ONLINE,
                    node_id=node_id,
                    timestamp=now,
                )
            )
        track.last_heartbeat = now

        # Derive sensor / mouse-presence edges from heartbeat snapshot (recovery path).
        out.extend(
            self._sensor_edges(
                node_id,
                track,
                hb.pellet,
                hb.load_position,
                hb.dome_open,
                now,
            )
        )
        if hb.mouse_presence != track.presence:
            track.presence = hb.mouse_presence
            out.append(
                NodeEvent(
                    kind=EventKind.PRESENCE_CHANGED,
                    node_id=node_id,
                    timestamp=now,
                    data={"active": hb.mouse_presence, "source": "heartbeat"},
                )
            )

        out.append(
            NodeEvent(
                kind=EventKind.HEARTBEAT,
                node_id=node_id,
                timestamp=now,
                data={
                    "dispense_state": hb.dispense_state,
                    "mouse_presence": hb.mouse_presence,
                    "pellet": hb.pellet,
                    "load_position": hb.load_position,
                    "dome_open": hb.dome_open,
                    "fault_code": hb.fault_code,
                    "session_pellets": self._pellets.presented(node_id),
                    "session_taken": self._pellets.taken(node_id),
                },
            )
        )
        return out

    def _from_event(self, arb_id: int, data: bytes, now: float) -> List[NodeEvent]:
        node_id = node_id_from_event_id(arb_id)
        if node_id is None:
            return []
        payload = parse_event(data)
        if payload is None:
            return []

        # InputChanged → PRESENCE_CHANGED / PG_CHANGED (+ derived dome edges)
        if payload.event == CanEvent.InputChanged:
            return self._from_input_changed(node_id, payload, now)

        # Pong is identity-only; not an experiment event.
        if payload.event == CanEvent.Pong:
            return []

        kind = _CAN_EVENT_TO_KIND.get(payload.event)
        if kind is None:
            return []

        event_data: Dict[str, Any] = {}
        if payload.event == CanEvent.Fault:
            fault = parse_fault_code(payload)
            event_data["fault_code"] = fault if fault is not None else ServiceStatus.Ok
            if payload.raw_extra:
                event_data["raw_extra"] = bytes(payload.raw_extra)
        elif payload.event == CanEvent.PresenceCalResult:
            # Not a pellet count — must not fall into the generic raw_extra
            # count fallback below, or ok/threshold get misread as a count.
            cal = parse_presence_cal(payload)
            if cal is not None:
                event_data["ok"] = cal.ok
                event_data["threshold"] = cal.threshold
                event_data["samples"] = cal.samples
        else:
            ctx = parse_event_context(payload)
            if ctx is not None:
                # Session-scoped counts, not the node's power-on counter.
                # Callbacks that ask "which pellet is this?" get an answer that
                # starts at 1 for every run.
                self._pellets.witness_event(node_id, payload.event, ctx["pellet_count"])
                tally = self._pellets.tally(node_id)
                event_data["session_pellets"] = tally.presented
                event_data["session_taken"] = tally.taken
                if "pellet_present" in ctx:
                    event_data["pellet_present"] = ctx["pellet_present"]
                if "dome_open" in ctx:
                    event_data["dome_open"] = ctx["dome_open"]
            elif payload.raw_extra:
                event_data["raw_extra"] = bytes(payload.raw_extra)

        return [
            NodeEvent(
                kind=kind,
                node_id=node_id,
                timestamp=now,
                data=event_data,
            )
        ]

    def _from_input_changed(
        self,
        node_id: int,
        payload,
        now: float,
    ) -> List[NodeEvent]:
        ic = parse_input_changed(payload)
        if ic is None:
            return []

        track = self._track(node_id)
        out: List[NodeEvent] = []

        if ic.input_id == InputId.MousePresence:
            track.presence = ic.active
            out.append(
                NodeEvent(
                    kind=EventKind.PRESENCE_CHANGED,
                    node_id=node_id,
                    timestamp=now,
                    data={"active": ic.active, "source": "event"},
                )
            )
            return out

        # Photogate change
        if ic.input_id == InputId.Pellet:
            track.pellet = ic.active
            gate = "pellet"
        elif ic.input_id == InputId.LoadPosition:
            track.load_position = ic.active
            gate = "load_position"
        elif ic.input_id == InputId.Dome:
            prev = track.dome_open
            track.dome_open = ic.active
            gate = "dome"
            if ic.active and not prev:
                out.append(
                    NodeEvent(
                        kind=EventKind.DOME_OPENED,
                        node_id=node_id,
                        timestamp=now,
                    )
                )
            elif not ic.active and prev:
                out.append(
                    NodeEvent(
                        kind=EventKind.DOME_CLOSED,
                        node_id=node_id,
                        timestamp=now,
                    )
                )
        else:
            return out

        out.append(
            NodeEvent(
                kind=EventKind.PG_CHANGED,
                node_id=node_id,
                timestamp=now,
                data={"gate": gate, "active": ic.active, "source": "event"},
            )
        )
        return out

    def _sensor_edges(
        self,
        node_id: int,
        track: _NodeTrack,
        pellet: bool,
        load_position: bool,
        dome_open: bool,
        now: float,
    ) -> List[NodeEvent]:
        """Emit PG_CHANGED / DOME_* when heartbeat sensor bits differ from track."""
        out: List[NodeEvent] = []
        if pellet != track.pellet:
            track.pellet = pellet
            out.append(
                NodeEvent(
                    kind=EventKind.PG_CHANGED,
                    node_id=node_id,
                    timestamp=now,
                    data={"gate": "pellet", "active": pellet, "source": "heartbeat"},
                )
            )
        if load_position != track.load_position:
            track.load_position = load_position
            out.append(
                NodeEvent(
                    kind=EventKind.PG_CHANGED,
                    node_id=node_id,
                    timestamp=now,
                    data={
                        "gate": "load_position",
                        "active": load_position,
                        "source": "heartbeat",
                    },
                )
            )
        if dome_open != track.dome_open:
            prev = track.dome_open
            track.dome_open = dome_open
            out.append(
                NodeEvent(
                    kind=EventKind.PG_CHANGED,
                    node_id=node_id,
                    timestamp=now,
                    data={"gate": "dome", "active": dome_open, "source": "heartbeat"},
                )
            )
            if dome_open and not prev:
                out.append(
                    NodeEvent(
                        kind=EventKind.DOME_OPENED,
                        node_id=node_id,
                        timestamp=now,
                    )
                )
            elif not dome_open and prev:
                out.append(
                    NodeEvent(
                        kind=EventKind.DOME_CLOSED,
                        node_id=node_id,
                        timestamp=now,
                    )
                )
        return out


def frame_to_events(
    msg: Any,
    now: float,
    normalizer: Optional[EventNormalizer] = None,
) -> List[NodeEvent]:
    """
    Convenience wrapper around EventNormalizer.frame_to_events.

    Prefer keeping a long-lived EventNormalizer so derived edges work.
    """
    if normalizer is None:
        normalizer = EventNormalizer()
    return normalizer.frame_to_events(msg, now)
