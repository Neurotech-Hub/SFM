"""
node_registry.py — Per-node state tracking and three-layer identity mapping.

Maintains the mapping:
  MAC (hardware UUID) → CAN Node ID (bus address) → User Label (GUI only)

NodeState is updated from heartbeat frames and event frames received off the
CAN bus.  Staleness detection marks nodes offline when heartbeats stop arriving.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .protocol import (
    CanEvent,
    DispenseState,
    HeartbeatPayload,
    InputId,
    ServiceStatus,
    format_mac,
)

# Offline detection scales with the configured heartbeat interval (GUI default
# 60s). A node is marked OFFLINE after this many missed-beat multiples.
DEFAULT_HEARTBEAT_INTERVAL_S: float = 60.0
HEARTBEAT_OFFLINE_MULTIPLIER: float = 3.0
DEFAULT_OFFLINE_TIMEOUT_S: float = (
    DEFAULT_HEARTBEAT_INTERVAL_S * HEARTBEAT_OFFLINE_MULTIPLIER
)


def offline_timeout_for_heartbeat(hb_interval_s: float) -> float:
    """Seconds without a heartbeat before a node is considered offline."""
    return max(float(hb_interval_s), 0.1) * HEARTBEAT_OFFLINE_MULTIPLIER


@dataclass
class NodeState:
    """Live state for one SFM node."""

    node_id: int
    label: str                              # user-editable (GUI-only)

    # Identity
    mac: Optional[bytes] = None             # 6-byte MAC from discovery
    discovery_state: str = "Pending"        # "Pending", "Announced", "Enabled"

    # Dispenser / sensor state (from heartbeat)
    dispense_state: DispenseState = DispenseState.Idle
    presence: bool = False
    pellet: bool = False                    # pellet on plate
    load_position: bool = False             # actuator at load position
    dome_open: bool = False                 # dome opened
    fault_code: ServiceStatus = ServiceStatus.Ok
    dome_open_warning: bool = False         # dome sensor open >30 s

    # True while this node is holding an EMPTY plate at the top from a
    # DispenseNoFeed (no-feed / mimic) cycle. Firmware deliberately reports
    # DispenseState.Loaded for that state too (DomeOpened is only emitted from
    # Loaded, and dome bouts on the empty plate are the behavioral measure),
    # so the wire state alone cannot distinguish "empty" from "actually
    # loaded". Event-driven: heartbeats may only CLEAR this flag, never set
    # it, so a missed event can never leave a stale EMPTY reading.
    presented_empty: bool = False

    # Result of the most recent presence recalibration, if any (None = never
    # calibrated this session; the node's own NVS-stored value is unknown to
    # the base station until a CalibratePresence broadcast reports back).
    presence_threshold: Optional[int] = None
    presence_cal_ok: Optional[bool] = None

    # Last-known presence-detection multiplier (threshold = mean + factor *
    # stdDev), set from a ConfigApplied ack after the Developer Menu broadcasts
    # a new value. None = never reported this session.
    presence_factor: Optional[float] = None

    # Connectivity
    last_heartbeat_time: Optional[float] = None
    online: bool = False

    # Derived convenience
    @property
    def mac_str(self) -> str:
        return format_mac(self.mac) if self.mac else "—"

    @property
    def heartbeat_age_s(self) -> Optional[float]:
        if self.last_heartbeat_time is None:
            return None
        return time.time() - self.last_heartbeat_time

    @property
    def sensor_bits(self) -> int:
        return (
            (int(self.pellet) << 0)
            | (int(self.load_position) << 1)
            | (int(self.dome_open) << 2)
        )

    @property
    def status_label(self) -> str:
        if not self.online:
            return "OFFLINE"
        if self.dispense_state == DispenseState.Loaded and self.presented_empty:
            return "EMPTY"
        return self.dispense_state.name.upper()

    @property
    def status_color(self) -> tuple[int, int, int, int]:
        """RGBA color for the status indicator (0–255 each)."""
        if not self.online:
            return (120, 120, 120, 255)   # grey
        s = self.dispense_state
        if s == DispenseState.Fault:
            return (220, 50, 50, 255)     # red
        if s == DispenseState.Idle:
            return (60, 200, 80, 255)     # green
        if s == DispenseState.Loaded:
            if self.presented_empty:
                return (220, 200, 50, 255)  # yellow — EMPTY (motion only, no pellet)
            return (50, 200, 220, 255)    # cyan — real pellet loaded
        if s == DispenseState.Seeking:
            return (60, 130, 220, 255)    # blue (homing)
        # Lowering / Loading / Raising
        return (60, 130, 220, 255)        # blue


class NodeRegistry:
    """
    Registry of all expected nodes.

    Pre-creates `num_nodes` slots on init (labels "Node 1"…"Node N").
    Nodes are populated with MAC and discovery state as discovery proceeds.
    """

    def __init__(self, num_nodes: int) -> None:
        assert 1 <= num_nodes <= 254, "num_nodes must be 1–254"
        self._nodes: Dict[int, NodeState] = {
            i: NodeState(node_id=i, label=f"Node {i}")
            for i in range(1, num_nodes + 1)
        }
        self._offline_timeout = DEFAULT_OFFLINE_TIMEOUT_S

    # ------------------------------------------------------------------
    # Registry access
    # ------------------------------------------------------------------

    def get(self, node_id: int) -> Optional[NodeState]:
        return self._nodes.get(node_id)

    def all_nodes(self) -> List[NodeState]:
        return list(self._nodes.values())

    def num_nodes(self) -> int:
        return len(self._nodes)

    # ------------------------------------------------------------------
    # Updates from CAN frames
    # ------------------------------------------------------------------

    def update_from_heartbeat(self, node_id: int, hb: HeartbeatPayload) -> None:
        """Apply a decoded heartbeat payload to the node's state."""
        node = self._get_or_create(node_id)
        node.dispense_state = hb.dispense_state
        node.presence = hb.mouse_presence
        node.pellet = hb.pellet
        node.load_position = hb.load_position
        node.dome_open = hb.dome_open
        node.fault_code = hb.fault_code
        node.last_heartbeat_time = time.time()
        node.online = True
        if node.discovery_state == "Pending":
            # Node is heartbeating even without formal discovery (e.g. manual ID)
            node.discovery_state = "Enabled"
        # Self-healing backstop for `presented_empty`: a real pellet, or any
        # state other than Loaded, is proof the EMPTY reading is stale (a
        # missed NoFeedPresented/Dwelling event, a dropped frame, etc). Never
        # SET it here — only NoFeedPresented does that.
        if hb.pellet or hb.dispense_state != DispenseState.Loaded:
            node.presented_empty = False

    def update_from_event(
        self,
        node_id: int,
        event: CanEvent,
        fault_code: Optional[ServiceStatus] = None,
    ) -> None:
        """Update node state based on a received event."""
        node = self._get_or_create(node_id)
        node.online = True
        # Mirror dispense state transitions from events for better responsiveness
        # (the next heartbeat will confirm the actual state anyway)
        state_map = {
            CanEvent.Seeking:         DispenseState.Seeking,
            CanEvent.Lowering:        DispenseState.Lowering,
            CanEvent.Loading:         DispenseState.Loading,
            CanEvent.OnPlate:         DispenseState.Loading,
            CanEvent.FeedSkipped:     DispenseState.Raising,
            CanEvent.Dwelling:        DispenseState.Dwelling,
            CanEvent.Raising:         DispenseState.Raising,
            CanEvent.Loaded:          DispenseState.Loaded,
            CanEvent.NoFeedPresented: DispenseState.Loaded,
            CanEvent.DomeOpened:      DispenseState.Loaded,
            CanEvent.PelletTaken:     DispenseState.Idle,
            CanEvent.Fault:           DispenseState.Fault,
        }
        if event in state_map:
            node.dispense_state = state_map[event]
            # Only NoFeedPresented means "empty plate at the top". Every
            # other transition in state_map is either a new cycle starting
            # or proof a real pellet is involved — except DomeOpened, which
            # deliberately falls through untouched: a dome bout on the empty
            # plate is the behavioral measure and must not clear the flag.
            if event == CanEvent.NoFeedPresented:
                node.presented_empty = True
            elif event != CanEvent.DomeOpened:
                node.presented_empty = False
        if event == CanEvent.Fault and fault_code is not None:
            node.fault_code = fault_code
        if event == CanEvent.DomeOpenWarning:
            node.dome_open_warning = True

    def update_from_input(self, node_id: int, input_id: InputId, active: bool) -> None:
        """Apply an immediate InputChanged event without waiting for heartbeat."""
        node = self._get_or_create(node_id)
        node.online = True
        if input_id == InputId.Pellet:
            node.pellet = active
            if active:
                node.presented_empty = False
        elif input_id == InputId.LoadPosition:
            node.load_position = active
        elif input_id == InputId.Dome:
            node.dome_open = active
            if not active:
                node.dome_open_warning = False
        elif input_id == InputId.MousePresence:
            node.presence = active

    def clear_fault(self, node_id: int) -> None:
        node = self._get_or_create(node_id)
        node.fault_code = ServiceStatus.Ok
        if node.dispense_state == DispenseState.Fault:
            node.dispense_state = DispenseState.Idle
        node.dome_open_warning = False
        node.presented_empty = False

    def register_node(self, node_id: int, mac: bytes, source: str = "ANNOUNCE") -> None:
        """Register a node's MAC address from discovery."""
        node = self._get_or_create(node_id)
        node.mac = mac
        node.discovery_state = "Enabled"

    def clear_identities(self) -> None:
        """
        Drop session MAC / discovery / online state for every slot.

        Used by Re-discover before ClearId + fresh assignment so tiles show
        Pending and Ping/Pong cannot resurrect stale MAC↔ID bindings.
        """
        for node in self._nodes.values():
            node.mac = None
            node.discovery_state = "Pending"
            node.online = False
            node.last_heartbeat_time = None

    # ------------------------------------------------------------------
    # User-facing operations
    # ------------------------------------------------------------------

    def set_label(self, node_id: int, label: str) -> None:
        """Rename a node's user label (GUI-only, never touches CAN)."""
        node = self._get_or_create(node_id)
        node.label = label.strip() or f"Node {node_id}"

    # ------------------------------------------------------------------
    # Staleness
    # ------------------------------------------------------------------

    def set_offline_timeout(self, seconds: float) -> None:
        self._offline_timeout = seconds

    def check_staleness(self) -> List[int]:
        """
        Mark nodes offline if their last heartbeat is older than the timeout.

        Returns list of node IDs that were just marked offline.
        """
        now = time.time()
        newly_offline = []
        for node in self._nodes.values():
            if node.online and node.last_heartbeat_time is not None:
                if (now - node.last_heartbeat_time) > self._offline_timeout:
                    node.online = False
                    node.presented_empty = False
                    newly_offline.append(node.node_id)
        return newly_offline

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create(self, node_id: int) -> NodeState:
        """Return existing node or create a new slot (handles nodes outside expected range)."""
        if node_id not in self._nodes:
            self._nodes[node_id] = NodeState(node_id=node_id, label=f"Node {node_id}")
        return self._nodes[node_id]
