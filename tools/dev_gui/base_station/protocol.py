"""
protocol.py — VFM CAN protocol constants and frame helpers.

Python mirror of src/services/ServiceTypes.h and src/services/CanService.h.
All CAN ID arithmetic and payload encoding/decoding lives here so every other
module imports a single source of truth.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations (mirror of ServiceTypes.h)
# ---------------------------------------------------------------------------

class CanCmd(IntEnum):
    """Commands sent from base station to a node (CAN ID 0x100 + nodeId)."""
    Ping           = 0x01
    Dispense       = 0x02
    Recover        = 0x03  # stop motion, clear sticky Fault, return to Idle
    AssignId       = 0x04  # payload byte[0] = new nodeId
    SetConfig      = 0x05  # payload TBD
    ReqStatus      = 0x06
    ClearId        = 0x07  # clear NVS id; node re-enters discovery
    DispenseNoFeed = 0x08  # dispense motion with no pellet; payload = dwell ms LE16 (optional)


# Friendly one-line purpose text for COMMAND log rows.
CAN_CMD_PURPOSE = {
    CanCmd.Ping: "identify / request MAC",
    CanCmd.Dispense: "load & present pellet",
    CanCmd.Recover: "stop motion & clear fault",
    CanCmd.AssignId: "set node ID",
    CanCmd.SetConfig: "set config",
    CanCmd.ReqStatus: "request heartbeat now",
    CanCmd.ClearId: "wipe stored ID",
    CanCmd.DispenseNoFeed: "run dispense motion, deliver no pellet",
}

# Dwell time (ms) the node holds at the drop position on a no-feed dispense.
DEFAULT_NO_FEED_DWELL_MS = 6000
NO_FEED_DWELL_MIN_MS = 500
NO_FEED_DWELL_MAX_MS = 60000


class CanEvent(IntEnum):
    """Events sent from a node to the base station (CAN ID 0x300 + nodeId)."""
    OnPlate         = 0x01  # pellet sensor confirmed during Loading; raise starting
    Loaded          = 0x02  # plate at top; ready for the mouse
    DomeOpened      = 0x03  # raw_extra: count LE16 + pellet_present
    Fault           = 0x04  # raw_extra[0] = ServiceStatus
    Pong            = 0x05
    InputChanged    = 0x06
    Lowering        = 0x07  # M2 toward load position
    Loading         = 0x08  # M1 loading a pellet
    Raising         = 0x09  # M2 raising plate
    DomeOpenWarning = 0x0A  # dome open >30 s (non-sticky warning)
    PelletTaken     = 0x0B  # raw_extra: count LE16 + dome_open
    FeedSkipped     = 0x0C  # plate occupied on Dispense
    Seeking         = 0x0D  # M2 clearing the load sensor before Lowering (clear or step cap)
    NoFeedPresented = 0x0E  # no-feed raise complete; raw_extra: count LE16 (NOT incremented)
    Dwelling        = 0x0F  # phase: holding at the drop position, M1 idle


# Friendly event-log labels for dispense phases (CanEvent.name may differ).
CAN_EVENT_DISPLAY_NAME = {
    CanEvent.OnPlate: "OnPlate",
    CanEvent.Loaded: "Loaded",
    CanEvent.Seeking: "Seeking",
    CanEvent.Lowering: "Lowering",
    CanEvent.Loading: "Loading",
    CanEvent.Raising: "Raising",
    CanEvent.DomeOpened: "DomeOpened",
    CanEvent.DomeOpenWarning: "DomeOpenWarning",
    CanEvent.PelletTaken: "PelletTaken",
    CanEvent.FeedSkipped: "FeedSkipped",
    CanEvent.NoFeedPresented: "NoFeedPresented",
    CanEvent.Dwelling: "Dwelling",
}


class InputId(IntEnum):
    """Inputs reported immediately by CanEvent.InputChanged."""
    Pellet        = 0x01  # pellet sensor — pellet on plate
    LoadPosition  = 0x02  # load-position sensor
    Dome          = 0x03  # dome open/close sensor
    MousePresence = 0x04  # capacitive mouse-presence pad


class DispenseState(IntEnum):
    """Dispenser FSM states carried in heartbeat byte 0."""
    Idle          = 0
    Lowering      = 1  # M2 down until load position
    Loading       = 2  # M1 loading pellet
    Raising       = 3  # M2 up by step count
    Loaded        = 4  # Pellet at top; ends on PelletTaken → Idle
    Seeking       = 5  # M2 up until load sensor clears or seekAwaySteps_
    Fault         = 6  # FeedTimeout / ActuatorTimeout / jam / pellet lost
    Dwelling      = 7  # no-feed: holding at the drop position, M1 idle


class ServiceStatus(IntEnum):
    """
    Fault codes carried in heartbeat byte 5 / Fault event extra.

    Mirror of src/services/ServiceTypes.h ServiceStatus. Must match exactly —
    these are wire values, not just labels (see commit 4861a97, which dropped
    the firmware's ``Timeout`` member without updating this mirror; every code
    >= 2 decoded one fault low until this was fixed).
    """
    Ok              = 0
    NotInitialized  = 1
    Jam             = 2
    InvalidData     = 3
    PelletLost      = 4       # pellet left the plate during raise
    FeedTimeout     = 5       # M1: no pellet confirmed — refill hopper
    ActuatorTimeout = 6       # M2: never reached target — sensor or motor


# Short, user-facing explanations for the base-station UI / logs.
SERVICE_STATUS_USER_MESSAGE = {
    ServiceStatus.Ok: "OK",
    ServiceStatus.NotInitialized: "Not initialized",
    ServiceStatus.Jam: "Jam — load sensor still blocked during raise",
    ServiceStatus.InvalidData: "Invalid data",
    ServiceStatus.PelletLost: "Pellet lost from the plate during raise",
    ServiceStatus.FeedTimeout: "Out of pellets — refill the hopper (M1 feed timed out)",
    ServiceStatus.ActuatorTimeout: (
        "Actuator fault — plate did not reach position "
        "(check load sensor or M2 motor)"
    ),
}


def fault_user_message(code: Optional[ServiceStatus]) -> str:
    """Translate a ServiceStatus into simple language for operators."""
    if code is None:
        return "Unknown fault"
    return SERVICE_STATUS_USER_MESSAGE.get(code, code.name)


# ---------------------------------------------------------------------------
# CAN ID constants (mirror of ServiceTypes.h)
# ---------------------------------------------------------------------------

CAN_CMD_BASE      = 0x100  # 0x100 + nodeId  (0x100 alone = broadcast to all)
CAN_CMD_BROADCAST = 0x100  # nodeId == 0 → all nodes
CAN_STATUS_BASE   = 0x200  # 0x200 + nodeId  (heartbeat)
CAN_EVENT_BASE    = 0x300  # 0x300 + nodeId  (events)

# Discovery frame IDs
CAN_ID_ANNOUNCE = 0x080  # node → base: MAC(6)
CAN_ID_ASSIGN   = 0x081  # base → node: MAC(6) + id(1)
CAN_ID_ACK      = 0x082  # node → base: MAC(6) + id(1)
CAN_ID_REJOIN   = 0x083  # node → base: MAC(6) + id(1)

DISCOVERY_IDS = {CAN_ID_ANNOUNCE, CAN_ID_ASSIGN, CAN_ID_ACK, CAN_ID_REJOIN}


# ---------------------------------------------------------------------------
# SetConfig sub-types (mirror of ServiceTypes.h ConfigType)
# ---------------------------------------------------------------------------
# SetConfig payload: [configType(1), value...]
CONFIG_HEARTBEAT_INTERVAL = 0x01  # value = uint16 LE, heartbeat interval in ms


# ---------------------------------------------------------------------------
# Heartbeat payload (mirror of CanService.h HeartbeatPayload)
# ---------------------------------------------------------------------------
# byte 0: DispenseState
# byte 1: pelletCountLo (presented)
# byte 2: pelletCountHi
# byte 3: mouse presence (0/1)
# byte 4: sensor bits  [bit2=dome open | bit1=load position | bit0=pellet]
# byte 5: faultCode (ServiceStatus)
# byte 6: takenCountLo
# byte 7: takenCountHi

@dataclass
class HeartbeatPayload:
    dispense_state: DispenseState
    mouse_presence: bool
    pellet: bool           # bit 0 — pellet present on plate
    load_position: bool    # bit 1 — at load position
    dome_open: bool        # bit 2 — dome open
    fault_code: ServiceStatus
    pellets_presented: int = 0
    pellets_taken: int = 0

    @property
    def sensor_bits(self) -> int:
        return (
            (self.pellet << 0)
            | (self.load_position << 1)
            | (self.dome_open << 2)
        )

    @property
    def dispense_state_str(self) -> str:
        try:
            return self.dispense_state.name
        except ValueError:
            return f"Unknown({self.dispense_state})"


def parse_heartbeat(data: bytes) -> Optional[HeartbeatPayload]:
    """Decode an 8-byte heartbeat payload. Returns None if data is malformed."""
    if len(data) < 6:
        return None
    try:
        state = DispenseState(data[0])
    except ValueError:
        state = DispenseState.Idle

    try:
        fault = ServiceStatus(data[5])
    except ValueError:
        fault = ServiceStatus.Ok

    sensor_bits = data[4]
    presented = data[1] | (data[2] << 8)
    taken = (data[6] | (data[7] << 8)) if len(data) >= 8 else 0
    return HeartbeatPayload(
        dispense_state=state,
        mouse_presence=bool(data[3]),
        pellet=bool(sensor_bits & 0x01),
        load_position=bool(sensor_bits & 0x02),
        dome_open=bool(sensor_bits & 0x04),
        fault_code=fault,
        pellets_presented=presented,
        pellets_taken=taken,
    )


# ---------------------------------------------------------------------------
# Event payload
# ---------------------------------------------------------------------------

@dataclass
class EventPayload:
    event: CanEvent
    raw_extra: bytes  # extra bytes beyond byte 0


@dataclass
class InputChangedPayload:
    input_id: InputId
    active: bool


def parse_event(data: bytes) -> Optional[EventPayload]:
    """Decode an event frame payload. Returns None if data is empty."""
    if not data:
        return None
    try:
        event = CanEvent(data[0])
    except ValueError:
        return None
    return EventPayload(event=event, raw_extra=data[1:])


def parse_fault_code(event: EventPayload) -> Optional[ServiceStatus]:
    """Decode Fault event extra byte[0] as ServiceStatus."""
    if event.event != CanEvent.Fault or len(event.raw_extra) < 1:
        return None
    try:
        return ServiceStatus(event.raw_extra[0])
    except ValueError:
        return None


def parse_event_context(event: EventPayload) -> Optional[dict]:
    """
    Decode count LE16 (+ optional context byte) for milestone events.

    Returns dict with pellet_count and optional pellet_present / dome_open.
    """
    if len(event.raw_extra) < 2:
        return None
    out = {
        "pellet_count": event.raw_extra[0] | (event.raw_extra[1] << 8),
    }
    if event.event == CanEvent.DomeOpened and len(event.raw_extra) >= 3:
        out["pellet_present"] = bool(event.raw_extra[2])
    if event.event == CanEvent.PelletTaken and len(event.raw_extra) >= 3:
        out["dome_open"] = bool(event.raw_extra[2])
    return out


def parse_input_changed(event: EventPayload) -> Optional[InputChangedPayload]:
    """Decode InputChanged extra bytes: inputId(1), active(0/1)."""
    if event.event != CanEvent.InputChanged or len(event.raw_extra) < 2:
        return None
    try:
        input_id = InputId(event.raw_extra[0])
    except ValueError:
        return None
    return InputChangedPayload(input_id=input_id, active=bool(event.raw_extra[1]))


# ---------------------------------------------------------------------------
# Discovery payload helpers
# ---------------------------------------------------------------------------

def parse_discovery(frame_id: int, data: bytes) -> Optional[dict]:
    """
    Decode a discovery frame.

    Returns a dict with keys:
      frame_id, mac (bytes), node_id (int, may be None for ANNOUNCE)
    """
    if frame_id == CAN_ID_ANNOUNCE:
        if len(data) < 6:
            return None
        return {"frame_id": frame_id, "mac": bytes(data[:6]), "node_id": None}
    elif frame_id in (CAN_ID_ASSIGN, CAN_ID_ACK, CAN_ID_REJOIN):
        if len(data) < 7:
            return None
        return {"frame_id": frame_id, "mac": bytes(data[:6]), "node_id": data[6]}
    return None


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------

def build_cmd_frame(node_id: int, cmd: CanCmd, payload: bytes = b"") -> tuple[int, bytes]:
    """
    Build a command frame.

    Returns (arbitration_id, data_bytes).
    node_id == 0 → broadcast (0x100).
    """
    arb_id = CAN_CMD_BASE + node_id  # 0x100 when node_id==0
    data = bytes([cmd.value]) + payload
    return arb_id, data[:8]  # CAN max 8 bytes


def build_setconfig_heartbeat(interval_ms: int) -> bytes:
    """
    Build the payload (after the SetConfig command byte) that sets the
    node's heartbeat emission interval.

    Payload: [CONFIG_HEARTBEAT_INTERVAL, ms_lo, ms_hi]
    """
    interval_ms = max(0, min(int(interval_ms), 0xFFFF))
    return bytes([CONFIG_HEARTBEAT_INTERVAL]) + struct.pack("<H", interval_ms)


def build_dispense_no_feed(dwell_ms: int = DEFAULT_NO_FEED_DWELL_MS) -> bytes:
    """
    Build the payload (after the DispenseNoFeed command byte): dwell time,
    uint16 LE ms. Clamped to [NO_FEED_DWELL_MIN_MS, NO_FEED_DWELL_MAX_MS] —
    clamp rather than reject, so a bad parameter can't brick a trial.
    """
    ms = max(NO_FEED_DWELL_MIN_MS, min(int(dwell_ms), NO_FEED_DWELL_MAX_MS))
    return struct.pack("<H", ms)


def build_assign_frame(mac: bytes, node_id: int) -> tuple[int, bytes]:
    """Build a CAN_ID_ASSIGN frame: MAC(6) + nodeId(1)."""
    assert len(mac) == 6, "MAC must be 6 bytes"
    assert 1 <= node_id <= 254, "nodeId must be 1-254"
    return CAN_ID_ASSIGN, bytes(mac) + bytes([node_id])


def build_heartbeat_frame(node_id: int, hb: HeartbeatPayload) -> tuple[int, bytes]:
    """Build a heartbeat frame (for the simulator)."""
    arb_id = CAN_STATUS_BASE + node_id
    presented = int(getattr(hb, "pellets_presented", 0))
    taken = int(getattr(hb, "pellets_taken", 0))
    data = bytes([
        int(hb.dispense_state),
        presented & 0xFF,
        (presented >> 8) & 0xFF,
        int(hb.mouse_presence),
        hb.sensor_bits,
        int(hb.fault_code),
        taken & 0xFF,
        (taken >> 8) & 0xFF,
    ])
    return arb_id, data


def build_event_frame(node_id: int, event: CanEvent, extra: bytes = b"") -> tuple[int, bytes]:
    """Build an event frame (for the simulator)."""
    arb_id = CAN_EVENT_BASE + node_id
    data = bytes([event.value]) + extra
    return arb_id, data[:8]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def format_mac(mac: bytes) -> str:
    """Format a 6-byte MAC as 'AA:BB:CC:DD:EE:FF'."""
    return ":".join(f"{b:02X}" for b in mac)


def node_id_from_cmd_id(arb_id: int) -> Optional[int]:
    """Extract node ID from a command frame arbitration ID. None if not a cmd frame."""
    if CAN_CMD_BASE <= arb_id <= CAN_CMD_BASE + 254:
        return arb_id - CAN_CMD_BASE
    return None


def node_id_from_hb_id(arb_id: int) -> Optional[int]:
    """Extract node ID from a heartbeat frame arbitration ID."""
    if CAN_STATUS_BASE < arb_id <= CAN_STATUS_BASE + 254:
        return arb_id - CAN_STATUS_BASE
    return None


def node_id_from_event_id(arb_id: int) -> Optional[int]:
    """Extract node ID from an event frame arbitration ID."""
    if CAN_EVENT_BASE < arb_id <= CAN_EVENT_BASE + 254:
        return arb_id - CAN_EVENT_BASE
    return None


def classify_frame(arb_id: int) -> str:
    """
    Classify a received CAN frame by its arbitration ID.

    Returns one of: 'HEARTBEAT', 'EVENT', 'COMMAND', 'DISCOVERY', 'UNKNOWN'.
    """
    if arb_id in DISCOVERY_IDS:
        return "DISCOVERY"
    if CAN_STATUS_BASE < arb_id <= CAN_STATUS_BASE + 254:
        return "HEARTBEAT"
    if CAN_EVENT_BASE < arb_id <= CAN_EVENT_BASE + 254:
        return "EVENT"
    if CAN_CMD_BASE <= arb_id <= CAN_CMD_BASE + 254:
        return "COMMAND"
    return "UNKNOWN"
