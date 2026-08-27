#!/usr/bin/env python3
"""
node_simulator.py — Simulates N SFM nodes on a SocketCAN interface.

Run alongside the GUI on vcan0 for hardware-free development and testing.

Usage:
    python node_simulator.py                        # 3 nodes on vcan0
    python node_simulator.py --interface vcan0 -n 3
    python node_simulator.py --fault-rate 0.1       # 10% chance of fault per dispense

Each simulated node:
  - Runs the discovery protocol (ANNOUNCE → waits for ASSIGN → sends ACK)
    OR immediately uses a pre-assigned ID with REJOIN if --skip-discovery
  - Sends heartbeats at 1 Hz
  - Responds to Ping with Pong
  - On Dispense: occupancy-first sequence —
    empty → Lowering → Loading → OnPlate → Raising → Loaded → DomeOpened → PelletTaken → Idle
    occupied → FeedSkipped → Raising → Loaded (then take as above)
  - On Recover: returns to Idle immediately

Press Ctrl+C to stop.

vcan0 setup (one-time on the Pi):
    sudo modprobe vcan
    sudo ip link add dev vcan0 type vcan
    sudo ip link set up vcan0
"""

from __future__ import annotations

import argparse
import random
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Optional

try:
    import can
except ImportError:
    print("python-can is required: pip install python-can", file=sys.stderr)
    sys.exit(1)

# Import protocol helpers from the base_station package if available,
# otherwise define the bare minimum here so the simulator is standalone.
try:
    from base_station.protocol import (
        CanCmd,
        CanEvent,
        DispenseState,
        InputId,
        ServiceStatus,
        HeartbeatPayload,
        CAN_CMD_BASE,
        CAN_CMD_BROADCAST,
        CAN_ID_ANNOUNCE,
        CAN_ID_ASSIGN,
        CAN_ID_ACK,
        CAN_ID_REJOIN,
        CAN_STATUS_BASE,
        CAN_EVENT_BASE,
        build_heartbeat_frame,
        build_event_frame,
        parse_discovery,
    )
except ImportError:
    # Fallback minimal definitions — keeps the simulator usable even when
    # run from outside the package directory.
    from enum import IntEnum

    class CanCmd(IntEnum):
        Ping=0x01; Dispense=0x02; Recover=0x03; AssignId=0x04; SetConfig=0x05
        ReqStatus=0x06; ClearId=0x07; DispenseNoFeed=0x08; CalibratePresence=0x09
        SyncFlash=0x0A

    class CanEvent(IntEnum):
        OnPlate=0x01; Loaded=0x02; DomeOpened=0x03; Fault=0x04
        Pong=0x05; InputChanged=0x06; Lowering=0x07; Loading=0x08; Raising=0x09
        DomeOpenWarning=0x0A; PelletTaken=0x0B; FeedSkipped=0x0C; Seeking=0x0D
        NoFeedPresented=0x0E; Dwelling=0x0F; PresenceCalResult=0x10

    class InputId(IntEnum):
        Pellet=0x01; LoadPosition=0x02; Dome=0x03; MousePresence=0x04

    class DispenseState(IntEnum):
        Idle=0; Lowering=1; Loading=2; Raising=3; Loaded=4; Seeking=5
        Fault=6; Dwelling=7

    class ServiceStatus(IntEnum):
        Ok=0; NotInitialized=1; Jam=2; InvalidData=3; PelletLost=4
        FeedTimeout=5; ActuatorTimeout=6

    from dataclasses import dataclass as _dataclass

    @_dataclass
    class HeartbeatPayload:
        dispense_state: "DispenseState"
        mouse_presence: bool
        pellet: bool
        load_position: bool
        dome_open: bool
        fault_code: "ServiceStatus"
        pellets_presented: int = 0
        pellets_taken: int = 0

    CAN_CMD_BASE=0x100; CAN_CMD_BROADCAST=0x100; CAN_ID_ANNOUNCE=0x080
    CAN_ID_ASSIGN=0x081; CAN_ID_ACK=0x082; CAN_ID_REJOIN=0x083
    CAN_STATUS_BASE=0x200; CAN_EVENT_BASE=0x300

    def build_heartbeat_frame(node_id, hb):
        arb_id = CAN_STATUS_BASE + node_id
        sensor_bits = (
            (int(hb.pellet) << 0)
            | (int(hb.load_position) << 1)
            | (int(hb.dome_open) << 2)
        )
        presented = int(getattr(hb, "pellets_presented", 0))
        taken = int(getattr(hb, "pellets_taken", 0))
        data = bytes([
            int(hb.dispense_state),
            presented & 0xFF, (presented >> 8) & 0xFF,
            int(hb.mouse_presence), sensor_bits, int(hb.fault_code),
            taken & 0xFF, (taken >> 8) & 0xFF,
        ])
        return arb_id, data

    def build_event_frame(node_id, event, extra=b""):
        return CAN_EVENT_BASE + node_id, bytes([int(event)]) + extra

    def parse_discovery(frame_id, data):
        if frame_id == CAN_ID_ANNOUNCE and len(data) >= 6:
            return {"frame_id": frame_id, "mac": bytes(data[:6]), "node_id": None}
        elif frame_id in (CAN_ID_ASSIGN, CAN_ID_ACK, CAN_ID_REJOIN) and len(data) >= 7:
            return {"frame_id": frame_id, "mac": bytes(data[:6]), "node_id": data[6]}
        return None


# ---------------------------------------------------------------------------
# Simulated node state machine
# ---------------------------------------------------------------------------

class SimNodePhase(Enum):
    WaitAssign   = auto()  # sent ANNOUNCE, waiting for ASSIGN
    Enabled      = auto()  # has a node ID, running normally
    Dispensing   = auto()  # mid-dispense sequence


@dataclass
class SimNode:
    index: int                   # 0-based index for generating unique MACs
    node_id: Optional[int] = None
    phase: SimNodePhase = SimNodePhase.WaitAssign

    # Dispenser state
    dispense_state: DispenseState = DispenseState.Idle
    mouse_presence: bool = False
    pellet: bool = False
    load_position: bool = False
    dome_open: bool = False
    fault_code: ServiceStatus = ServiceStatus.Ok
    pellets_presented: int = 0
    pellets_taken: int = 0
    dome_open_since: Optional[float] = None
    dome_warn_sent: bool = False
    no_feed: bool = False    # current/last cycle is a no-feed dispense
    # Latched when another node starts raising. Mirrors firmware's
    # DispenserService::peerRaiseSeen_: set regardless of phase (an occupied fed
    # node raises before this one finishes lowering), consumed only at step 10.
    peer_raise_seen: bool = False
    cal_until: Optional[float] = None  # presence calibration in progress until this time
    presence_threshold: int = 35000    # mirrors firmware kDefaultPresenceThreshold
    presence_factor: float = 3.0       # mirrors firmware kDefaultPresenceFactor

    # Timing
    last_heartbeat: float = field(default_factory=time.time)
    dispense_step_time: float = 0.0
    dispense_step: int = 0
    hb_interval: float = 5.0  # per-node heartbeat interval (s), configurable via SetConfig

    @property
    def mac(self) -> bytes:
        """Generate a deterministic fake MAC from the index."""
        return bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, self.index + 1])

    def hb_payload(self) -> HeartbeatPayload:
        return HeartbeatPayload(
            dispense_state=self.dispense_state,
            mouse_presence=self.mouse_presence,
            pellet=self.pellet,
            load_position=self.load_position,
            dome_open=self.dome_open,
            fault_code=self.fault_code,
            pellets_presented=self.pellets_presented,
            pellets_taken=self.pellets_taken,
        )


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class NodeSimulator:
    """Runs N simulated SFM nodes on a SocketCAN interface."""

    # Dispense sequence timings (seconds after command received)
    LOWERING_DELAY = 1.0
    RAISING_DELAY  = 2.0
    TAKEN_DELAY_MIN = 3.0
    TAKEN_DELAY_MAX = 5.0
    HB_INTERVAL     = 5.0  # default node heartbeat interval (s)
    DOME_WARN_DELAY = 3.0  # shorter than firmware 30s for sim demos
    CONFIG_HEARTBEAT_INTERVAL = 0x01
    CONFIG_PRESENCE_FACTOR    = 0x02
    PRESENCE_FACTOR_MIN = 0.1  # mirrors firmware kMinPresenceFactor
    PRESENCE_FACTOR_MAX = 100.0  # mirrors firmware kMaxPresenceFactor
    PRESENCE_CAL_S  = 5.0  # mirrors firmware kPresenceCalMs
    PRESENCE_LINGER_S = 1.0  # presence clears slightly after the dome closes

    def __init__(
        self,
        interface: str,
        num_nodes: int,
        bitrate: int,
        fault_rate: float = 0.0,
        skip_discovery: bool = False,
    ) -> None:
        self._interface = interface
        self._num_nodes = num_nodes
        self._bitrate = bitrate
        self._fault_rate = fault_rate
        self._skip_discovery = skip_discovery
        self._bus: Optional[can.BusABC] = None
        self._nodes: Dict[int, SimNode] = {}   # index → SimNode
        self._running = False

    def start(self) -> None:
        self._bus = can.interface.Bus(
            channel=self._interface,
            interface="socketcan",
            bitrate=self._bitrate,
        )
        self._running = True

        for i in range(self._num_nodes):
            node = SimNode(index=i, hb_interval=self.HB_INTERVAL)
            self._nodes[i] = node

        # Stagger announce/rejoin slightly so the base station can handle them
        # one at a time (as the real daisy-chain does sequentially).
        if self._skip_discovery:
            for i, node in self._nodes.items():
                node.node_id = i + 1
                node.phase = SimNodePhase.Enabled
                self._send_rejoin(node)
                time.sleep(0.1)
        else:
            # Only announce the first node; subsequent nodes announce after
            # the base station confirms the previous one (simulating AEI chain).
            # For simplicity in the simulator, we use a small delay between
            # announces and wait for ASSIGN before the next one announces.
            threading.Thread(
                target=self._sequential_announce,
                daemon=True,
            ).start()

        # Main loop in this thread
        self._run_loop()

    def stop(self) -> None:
        self._running = False
        if self._bus:
            self._bus.shutdown()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sequential_announce(self) -> None:
        """Announce nodes one by one, waiting for ASSIGN before the next."""
        for i, node in sorted(self._nodes.items()):
            self._send_announce(node)
            # Wait until this node gets its ID (ASSIGN received in _run_loop)
            deadline = time.time() + 10.0
            while self._running and node.node_id is None and time.time() < deadline:
                time.sleep(0.05)
            if node.node_id is None:
                print(f"  [SIM] Node {i+1} timed out waiting for ASSIGN", flush=True)
            time.sleep(0.1)  # brief gap before next announce

    def _run_loop(self) -> None:
        """Main receive + heartbeat loop."""
        while self._running:
            # Receive frames
            msg = self._bus.recv(timeout=0.05)
            if msg is not None:
                self._handle_rx(msg)

            # Heartbeats + dispense step advances + dome-open warning
            now = time.time()
            for node in self._nodes.values():
                if node.phase == SimNodePhase.Enabled or node.phase == SimNodePhase.Dispensing:
                    if now - node.last_heartbeat >= node.hb_interval:
                        self._send_heartbeat(node)
                        node.last_heartbeat = now
                    if node.phase == SimNodePhase.Dispensing:
                        self._advance_dispense(node, now)
                    self._check_dome_open_warning(node, now)
                    self._advance_calibration(node, now)

    def _handle_rx(self, msg: can.Message) -> None:
        arb_id = msg.arbitration_id
        data   = bytes(msg.data)

        # Discovery: ASSIGN frame
        if arb_id == CAN_ID_ASSIGN:
            info = parse_discovery(arb_id, data)
            if info:
                for node in self._nodes.values():
                    if node.mac == info["mac"]:
                        node.node_id = info["node_id"]
                        node.phase   = SimNodePhase.Enabled
                        self._send_ack(node)
                        print(f"  [SIM] Node {node.index+1} assigned CAN ID {node.node_id}", flush=True)
                        break
            return

        # Command frames (broadcast or per-node)
        is_broadcast = (arb_id == CAN_CMD_BROADCAST)
        for node in self._nodes.values():
            if node.node_id is None:
                continue
            is_my_cmd = (arb_id == CAN_CMD_BASE + node.node_id)
            if not (is_broadcast or is_my_cmd):
                continue
            if not data:
                continue
            try:
                cmd = CanCmd(data[0])
            except ValueError:
                continue

            if cmd == CanCmd.Ping:
                # Pong carries the node's MAC — mirrors real firmware so the
                # GUI can confirm/refresh its MAC<->ID mapping from a live node.
                self._send_event(node, CanEvent.Pong, node.mac)
                print(f"  [SIM] Node {node.node_id}: status LED blink (Ping)", flush=True)

            elif cmd == CanCmd.Dispense:
                # Idle or Loaded (re-dispense / occupied skip)
                if node.dispense_state in (DispenseState.Idle, DispenseState.Loaded):
                    node.no_feed = False
                    node.peer_raise_seen = False
                    node.phase = SimNodePhase.Dispensing
                    node.dispense_step_time = time.time()
                    node.dome_open = False
                    node.dome_open_since = None
                    node.dome_warn_sent = False
                    if node.pellet:
                        self._begin_occupied(node)
                    else:
                        node.load_position = False
                        node.dispense_state = DispenseState.Lowering
                        node.dispense_step = 0
                        self._send_event(node, CanEvent.Lowering)
                        print(
                            f"  [SIM] Node {node.node_id}: Dispense started (Lowering)",
                            flush=True,
                        )

            elif cmd == CanCmd.DispenseNoFeed:
                # Idle or Loaded (re-dispense / occupied skip)
                if node.dispense_state in (DispenseState.Idle, DispenseState.Loaded):
                    node.phase = SimNodePhase.Dispensing
                    node.dispense_step_time = time.time()
                    node.dome_open = False
                    node.dome_open_since = None
                    node.dome_warn_sent = False
                    # No payload: the raise is triggered by a peer's Raising
                    # event. Any trailing bytes from an older base station are
                    # ignored, matching firmware.
                    node.peer_raise_seen = False
                    if node.pellet:
                        # Occupied → identical to Dispense: FeedSkipped, real pellet.
                        node.no_feed = False
                        self._begin_occupied(node)
                    else:
                        node.no_feed = True
                        node.load_position = False
                        node.dispense_state = DispenseState.Lowering
                        node.dispense_step = 0
                        self._send_event(node, CanEvent.Lowering)
                        print(
                            f"  [SIM] Node {node.node_id}: DispenseNoFeed started (Lowering)",
                            flush=True,
                        )

            elif cmd == CanCmd.Recover:
                node.dispense_state = DispenseState.Idle
                node.phase          = SimNodePhase.Enabled
                node.fault_code     = ServiceStatus.Ok
                node.no_feed        = False
                node.peer_raise_seen = False
                node.pellet = node.load_position = node.dome_open = False
                node.dome_open_since = None
                node.dome_warn_sent = False
                print(f"  [SIM] Node {node.node_id}: Recovered", flush=True)

            elif cmd == CanCmd.CalibratePresence:
                if node.cal_until is None:  # ignore if one is already running
                    node.cal_until = time.time() + self.PRESENCE_CAL_S
                    print(f"  [SIM] Node {node.node_id}: presence calibration started", flush=True)

            elif cmd == CanCmd.ReqStatus:
                self._send_heartbeat(node)

            elif cmd == CanCmd.SyncFlash:
                ms = (data[1] | (data[2] << 8)) if len(data) >= 3 else 500
                ms = max(50, min(ms, 5000))
                print(f"  [SIM] Node {node.node_id}: status LED solid ON {ms}ms (SyncFlash)", flush=True)

            elif cmd == CanCmd.SetConfig and len(data) >= 2:
                config_type = data[1]
                ok = False
                raw_value = 0
                if config_type == self.CONFIG_HEARTBEAT_INTERVAL and len(data) >= 4:
                    ms = data[2] | (data[3] << 8)
                    node.hb_interval = ms / 1000.0
                    raw_value = ms
                    ok = True
                    print(f"  [SIM] Node {node.node_id}: heartbeat interval set to {node.hb_interval:.2f}s", flush=True)
                elif config_type == self.CONFIG_PRESENCE_FACTOR and len(data) >= 6:
                    factor = struct.unpack("<f", bytes(data[2:6]))[0]
                    factor = max(self.PRESENCE_FACTOR_MIN, min(factor, self.PRESENCE_FACTOR_MAX))
                    node.presence_factor = factor
                    raw_value = struct.unpack("<I", struct.pack("<f", factor))[0]
                    ok = True
                    print(f"  [SIM] Node {node.node_id}: presence factor set to {factor:.2f}", flush=True)
                extra = (
                    bytes([config_type, 1 if ok else 0])
                    + raw_value.to_bytes(4, "little")
                )
                self._send_event(node, CanEvent.ConfigApplied, extra)

            elif cmd == CanCmd.AssignId and len(data) >= 2:
                old_id = node.node_id
                node.node_id = data[1]
                print(f"  [SIM] Node {node.index+1}: ID changed {old_id} → {node.node_id}", flush=True)

            elif cmd == CanCmd.ClearId:
                print(f"  [SIM] Node {node.node_id}: ClearId — NVS cleared, awaiting re-ASSIGN", flush=True)
                node.node_id = None
                node.phase = SimNodePhase.WaitAssign
                node.dispense_state = DispenseState.Idle
                node.pellet = node.load_position = node.dome_open = False
                # Re-announce so the base can re-assign (simulates WaitAEI→Announce)
                self._send_announce(node)

    def _begin_occupied(self, node: SimNode) -> None:
        """Occupied plate → FeedSkipped (+ raise if needed). Shared by
        Dispense and DispenseNoFeed — an occupied plate is always presented
        honestly with its real pellet, never silently swapped for empty."""
        count_extra = bytes([
            node.pellets_presented & 0xFF,
            (node.pellets_presented >> 8) & 0xFF,
        ])
        self._send_event(node, CanEvent.FeedSkipped, count_extra)
        if node.load_position:
            node.dispense_state = DispenseState.Raising
            self._send_event(node, CanEvent.Raising, count_extra)
            node.dispense_step = 3  # jump to raise travel
            print(
                f"  [SIM] Node {node.node_id}: FeedSkipped → Raising",
                flush=True,
            )
        else:
            node.dispense_state = DispenseState.Loaded
            self._send_event(node, CanEvent.Loaded, count_extra)
            taken_delay = random.uniform(
                self.TAKEN_DELAY_MIN, self.TAKEN_DELAY_MAX
            )
            node._taken_delay = taken_delay
            node.dispense_step = 4
            print(
                f"  [SIM] Node {node.node_id}: FeedSkipped (already elevated)",
                flush=True,
            )

    def _advance_dispense(self, node: SimNode, now: float) -> None:
        elapsed = now - node.dispense_step_time

        def _count_extra(count: int = None) -> bytes:
            c = node.pellets_presented if count is None else count
            return bytes([c & 0xFF, (c >> 8) & 0xFF])

        # Step 0 → load position reached. No-feed forks here: Dwelling instead
        # of Loading — M1 never runs, so no fault injection on this branch
        # either (fault injection models an M1/hopper problem, which doesn't
        # apply).
        if node.dispense_step == 0 and elapsed >= self.LOWERING_DELAY:
            if node.no_feed:
                node.load_position = True
                self._send_input_changed(node, InputId.LoadPosition, True)
                node.dispense_state = DispenseState.Dwelling
                self._send_event(node, CanEvent.Dwelling, _count_extra())
                node.dispense_step      = 10
                node.dispense_step_time = now
                return
            if self._fault_rate > 0 and random.random() < self._fault_rate:
                node.dispense_state = DispenseState.Fault
                node.fault_code = (
                    ServiceStatus.FeedTimeout
                    if random.random() < 0.5
                    else ServiceStatus.ActuatorTimeout
                )
                node.phase = SimNodePhase.Enabled
                self._send_event(node, CanEvent.Fault, bytes([int(node.fault_code)]))
                print(f"  [SIM] Node {node.node_id}: FAULT {node.fault_code.name}", flush=True)
                return
            node.load_position = True
            self._send_input_changed(node, InputId.LoadPosition, True)
            node.dispense_state = DispenseState.Loading
            self._send_event(node, CanEvent.Loading, _count_extra())
            node.dispense_step      = 1
            node.dispense_step_time = now

        # Step 1 → 2: pellet sensor asserts → OnPlate + Raising same tick
        elif node.dispense_step == 1 and elapsed >= 0.5:
            node.pellet = True
            self._send_input_changed(node, InputId.Pellet, True)
            self._send_event(node, CanEvent.OnPlate, _count_extra())
            node.dispense_state = DispenseState.Raising
            self._send_event(node, CanEvent.Raising, _count_extra())
            node.dispense_step      = 3
            node.dispense_step_time = now

        # Step 3 → Loaded after raise travel (pellet sensor stays latched)
        elif node.dispense_step == 3 and elapsed >= self.RAISING_DELAY:
            node.load_position = False
            node.pellets_presented += 1
            node.dispense_state = DispenseState.Loaded
            self._send_event(node, CanEvent.Loaded, _count_extra())
            taken_delay = random.uniform(self.TAKEN_DELAY_MIN, self.TAKEN_DELAY_MAX)
            node._taken_delay = taken_delay
            node.dispense_step      = 4
            node.dispense_step_time = now

        # Step 4 → animal arrives (presence asserts) then DomeOpened (dome lift
        # while pellet present).
        elif node.dispense_step == 4 and elapsed >= getattr(node, "_taken_delay", self.TAKEN_DELAY_MAX):
            node.mouse_presence = True
            self._send_input_changed(node, InputId.MousePresence, True)
            node.dome_open = True
            node.dome_open_since = now
            node.dome_warn_sent = False
            self._send_input_changed(node, InputId.Dome, True)
            extra = _count_extra() + bytes([1 if node.pellet else 0])
            self._send_event(node, CanEvent.DomeOpened, extra)
            node.dispense_step = 5
            node.dispense_step_time = now

        # Step 5 → pellet sensor clear → PelletTaken → Idle
        elif node.dispense_step == 5 and elapsed >= 0.4:
            if node.pellet:
                node.pellet = False
                self._send_input_changed(node, InputId.Pellet, False)
            node.pellets_taken += 1
            taken_extra = bytes([
                node.pellets_taken & 0xFF,
                (node.pellets_taken >> 8) & 0xFF,
                1 if node.dome_open else 0,
            ])
            self._send_event(node, CanEvent.PelletTaken, taken_extra)
            if node.dome_open:
                self._send_input_changed(node, InputId.Dome, False)
                node.dome_open = False
                node.dome_open_since = None
                node.dome_warn_sent = False
            node.dispense_state = DispenseState.Idle
            node.phase = SimNodePhase.Enabled
            node.dispense_step = 6
            node.dispense_step_time = now
            print(
                f"  [SIM] Node {node.node_id}: PelletTaken → Idle "
                f"(taken={node.pellets_taken})",
                flush=True,
            )

        # Step 6 → animal lingers briefly, then presence clears.
        elif node.dispense_step == 6 and elapsed >= self.PRESENCE_LINGER_S:
            node.mouse_presence = False
            self._send_input_changed(node, InputId.MousePresence, False)
            node.dispense_step = 7

        # Step 10 → a peer started raising → Raising (M1 never ran; pellet stays
        # clear). No timeout: with no peer to follow the node holds here until
        # Recover, same as firmware.
        elif node.dispense_step == 10 and node.peer_raise_seen:
            node.dispense_state = DispenseState.Raising
            self._send_event(node, CanEvent.Raising, _count_extra())
            node.dispense_step      = 11
            node.dispense_step_time = now

        # Step 11 → raise travel done → NoFeedPresented. Counter NOT incremented,
        # no pellet ever set on the plate.
        elif node.dispense_step == 11 and elapsed >= self.RAISING_DELAY:
            node.load_position = False
            node.dispense_state = DispenseState.Loaded
            self._send_event(node, CanEvent.NoFeedPresented, _count_extra())
            node._taken_delay = random.uniform(self.TAKEN_DELAY_MIN, self.TAKEN_DELAY_MAX)
            node.dispense_step      = 12
            node.dispense_step_time = now

        # Step 12 → animal arrives (presence asserts) then dome bout on the
        # empty plate (pellet_present = 0)
        elif node.dispense_step == 12 and elapsed >= getattr(node, "_taken_delay", self.TAKEN_DELAY_MAX):
            node.mouse_presence = True
            self._send_input_changed(node, InputId.MousePresence, True)
            node.dome_open = True
            node.dome_open_since = now
            node.dome_warn_sent = False
            self._send_input_changed(node, InputId.Dome, True)
            extra = _count_extra() + bytes([0])  # pellet_present always false
            self._send_event(node, CanEvent.DomeOpened, extra)
            node.dispense_step      = 13
            node.dispense_step_time = now

        # Step 13 → dome closes. NO PelletTaken — the node stays Loaded +
        # no_feed, exactly as the firmware does, until the next dispense command.
        elif node.dispense_step == 13 and elapsed >= 0.4:
            node.dome_open = False
            node.dome_open_since = None
            node.dome_warn_sent = False
            self._send_input_changed(node, InputId.Dome, False)
            node.phase = SimNodePhase.Enabled
            node.dispense_step = 14
            node.dispense_step_time = now
            print(
                f"  [SIM] Node {node.node_id}: NoFeedPresented dome closed "
                f"(no PelletTaken — plate is empty)",
                flush=True,
            )

        # Step 14 → animal lingers briefly, then presence clears.
        elif node.dispense_step == 14 and elapsed >= self.PRESENCE_LINGER_S:
            node.mouse_presence = False
            self._send_input_changed(node, InputId.MousePresence, False)
            node.dispense_step = 15

    def _advance_calibration(self, node: SimNode, now: float) -> None:
        """Model the real failure mode: a pad occupied during the 5s capture
        yields a threshold above real presence readings -> ok=0, so the GUI's
        failure path is exercisable without hardware."""
        if node.cal_until is None or now < node.cal_until:
            return
        node.cal_until = None
        ok = not node.mouse_presence
        if ok:
            node.presence_threshold = 35000 + random.randint(200, 900)
            samples = random.randint(180, 200)
        else:
            samples = 0
        extra = (
            bytes([1 if ok else 0])
            + node.presence_threshold.to_bytes(4, "little")
            + samples.to_bytes(2, "little")
        )
        self._send_event(node, CanEvent.PresenceCalResult, extra)
        print(
            f"  [SIM] Node {node.node_id}: presence calibration "
            f"{'OK' if ok else 'FAILED'} threshold={node.presence_threshold}",
            flush=True,
        )

    def _check_dome_open_warning(self, node: SimNode, now: float) -> None:
        """Emit one-shot DomeOpenWarning after continuous dome opening (sim delay)."""
        if not node.dome_open:
            node.dome_open_since = None
            node.dome_warn_sent = False
            return
        if node.dome_open_since is None:
            node.dome_open_since = now
            return
        if node.dome_warn_sent:
            return
        if (now - node.dome_open_since) < self.DOME_WARN_DELAY:
            return
        node.dome_warn_sent = True
        self._send_event(node, CanEvent.DomeOpenWarning)
        print(f"  [SIM] Node {node.node_id}: DomeOpenWarning", flush=True)

    # ------------------------------------------------------------------
    # Frame senders
    # ------------------------------------------------------------------

    def _send_announce(self, node: SimNode) -> None:
        msg = can.Message(
            arbitration_id=CAN_ID_ANNOUNCE,
            data=node.mac,
            is_extended_id=False,
        )
        self._bus.send(msg)
        print(f"  [SIM] Node {node.index+1}: ANNOUNCE MAC={node.mac.hex(':')}", flush=True)

    def _send_ack(self, node: SimNode) -> None:
        data = bytes(node.mac) + bytes([node.node_id])
        msg  = can.Message(arbitration_id=CAN_ID_ACK, data=data, is_extended_id=False)
        self._bus.send(msg)

    def _send_rejoin(self, node: SimNode) -> None:
        data = bytes(node.mac) + bytes([node.node_id])
        msg  = can.Message(arbitration_id=CAN_ID_REJOIN, data=data, is_extended_id=False)
        self._bus.send(msg)
        print(f"  [SIM] Node {node.index+1}: REJOIN id={node.node_id}", flush=True)

    def _send_heartbeat(self, node: SimNode) -> None:
        arb_id, data = build_heartbeat_frame(node.node_id, node.hb_payload())
        msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)
        self._bus.send(msg)

    def _send_event(self, node: SimNode, event: CanEvent, extra: bytes = b"") -> None:
        arb_id, data = build_event_frame(node.node_id, event, extra)
        msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)
        self._bus.send(msg)
        print(f"  [SIM] Node {node.node_id}: → {event.name}", flush=True)

        # Peer sync: on real hardware every node's TWAI filter accepts all
        # frames, so a Raising event reaches its neighbours over the bus. One
        # process owns every simulated node here, so deliver it directly —
        # the observable behaviour (and the frames on the wire) match.
        if event == CanEvent.Raising:
            for peer in self._nodes.values():
                if peer is not node and peer.no_feed:
                    peer.peer_raise_seen = True

    def _send_input_changed(self, node: SimNode, input_id: InputId, active: bool) -> None:
        self._send_event(node, CanEvent.InputChanged, bytes([int(input_id), int(active)]))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SFM Node Simulator — fake SFM nodes on a SocketCAN interface",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--interface", "-i", default="vcan0",
                        help="SocketCAN interface")
    parser.add_argument("--bitrate", "-b", type=int, default=250_000,
                        help="CAN bitrate (ignored for vcan)")
    parser.add_argument("--nodes", "-n", type=int, default=3,
                        help="Number of nodes to simulate")
    parser.add_argument("--fault-rate", type=float, default=0.0, metavar="RATE",
                        help="Probability (0.0–1.0) of fault per dispense")
    parser.add_argument("--skip-discovery", action="store_true",
                        help="Use REJOIN instead of ANNOUNCE (nodes appear pre-assigned)")
    args = parser.parse_args()

    sim = NodeSimulator(
        interface=args.interface,
        num_nodes=args.nodes,
        bitrate=args.bitrate,
        fault_rate=args.fault_rate,
        skip_discovery=args.skip_discovery,
    )

    print(f"SFM Node Simulator — {args.nodes} node(s) on {args.interface}")
    print("Press Ctrl+C to stop.\n")
    try:
        sim.start()
    except KeyboardInterrupt:
        print("\nStopping simulator.")
        sim.stop()


if __name__ == "__main__":
    main()
