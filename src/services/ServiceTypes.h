#pragma once

#include <Arduino.h>

namespace vfm {

// ---------------------------------------------------------------------------
// General service status (returned from begin() and error paths)
// ---------------------------------------------------------------------------
enum class ServiceStatus : uint8_t {
    Ok = 0,
    NotInitialized,
    Timeout,
    Jam,
    InvalidData,
    PelletLost,   // pellet left the plate during raise
};

// ---------------------------------------------------------------------------
// Dispenser state machine states
// ---------------------------------------------------------------------------
enum class DispenseState : uint8_t {
    Idle = 0,
    Lowering    = 1, // M2 down to the load sensor, then grabSteps_ past it (empty plate only)
    Feeding     = 2, // M1 until pellet presence asserts
    Raising     = 3, // M2 up by raiseSteps_ from the pellet-drop position
    Presented   = 4, // Pellet at top; ends on PelletTaken → Idle
    SeekingAway = 5, // M2 up by seekAwaySteps_ to clear the load sensor (before approach)
    Fault       = 6, // sticky until recover()
};

// ---------------------------------------------------------------------------
// Dispenser events – one event is latched per transition.
// Read with DispenserService::takeEvent(); returns None if no new event.
// ---------------------------------------------------------------------------
enum class DispenseEvent : uint8_t {
    None = 0,
    PelletLoaded,     // pellet presence asserted; raise starting
    PelletPresented,  // actuator reached top (increments pelletCount)
    DomeOpened,       // dome lifted while Presented
    Fault,            // Timeout / Jam / PelletLost (see faultCode())
    DomeOpenWarning,  // dome open continuously > kDomeOpenWarnMs
    PelletTaken,      // pellet presence cleared while Presented → Idle
    FeedSkipped,      // Dispense with plate already occupied
};

// ---------------------------------------------------------------------------
// CAN command codes  (byte[0] of every command frame)
// Base -> Node on ID: 0x100 + nodeId   (0x100 = broadcast to all nodes)
// ---------------------------------------------------------------------------
enum class CanCmd : uint8_t {
    Ping      = 0x01,
    Dispense  = 0x02,
    Recover   = 0x03, // stop motion, clear sticky Fault, return to Idle
    AssignId  = 0x04, // payload byte[1] = new nodeId
    SetConfig = 0x05, // payload TBD
    ReqStatus = 0x06,
    ClearId   = 0x07, // clear NVS id; re-enter discovery (broadcast-friendly)
};

// ---------------------------------------------------------------------------
// CAN event codes  (byte[0] of every event frame)
// Node -> Base on ID: 0x300 + nodeId
// ---------------------------------------------------------------------------
enum class CanEvent : uint8_t {
    PelletLoaded    = 0x01, // pellet on plate; raise starting
    PelletPresented = 0x02,
    DomeOpened      = 0x03, // dome lift; extra: count LE16 + pellet_present
    Fault           = 0x04, // payload byte[1] = ServiceStatus
    Pong            = 0x05,
    InputChanged    = 0x06, // payload: InputId(1), active(0/1)
    Lowering        = 0x07, // M2 toward load position (incl. SeekingAway)
    Loading         = 0x08, // M1 running (Feeding state)
    Raising         = 0x09, // M2 raising plate
    DomeOpenWarning = 0x0A, // dome open > kDomeOpenWarnMs
    PelletTaken     = 0x0B, // extra: count LE16 + dome_open
    FeedSkipped     = 0x0C, // plate occupied on Dispense; lower/feed skipped
};

// Input IDs carried by CanEvent::InputChanged.
// Wire values: 1 = pellet sensor, 2 = load position, 3 = dome, 4 = animal presence.
enum class InputId : uint8_t {
    PG1      = 0x01, // pellet presence on plate
    PG2      = 0x02, // load position
    PG3      = 0x03, // dome open
    Presence = 0x04, // animal presence detection sensor
};

// ---------------------------------------------------------------------------
// SetConfig sub-types (byte[0] of the SetConfig command payload)
// ---------------------------------------------------------------------------
enum class ConfigType : uint8_t {
    HeartbeatInterval = 0x01, // value = uint16 LE, heartbeat interval in ms
};

// ---------------------------------------------------------------------------
// Discovery frame IDs  (used by NodeIdentity during boot)
// ---------------------------------------------------------------------------
constexpr uint32_t CAN_ID_ANNOUNCE = 0x080; // node -> base: MAC(6)
constexpr uint32_t CAN_ID_ASSIGN   = 0x081; // base -> node: MAC(6) + id(1)
constexpr uint32_t CAN_ID_ACK      = 0x082; // node -> base: MAC(6) + id(1)
constexpr uint32_t CAN_ID_REJOIN   = 0x083; // node -> base: MAC(6) + id(1)

// ---------------------------------------------------------------------------
// CAN ID layout
// ---------------------------------------------------------------------------
constexpr uint32_t CAN_CMD_BASE       = 0x100; // 0x100 + nodeId
constexpr uint32_t CAN_CMD_BROADCAST  = 0x100; // nodeId == 0 -> all nodes
constexpr uint32_t CAN_STATUS_BASE    = 0x200; // 0x200 + nodeId
constexpr uint32_t CAN_EVENT_BASE     = 0x300; // 0x300 + nodeId

// ---------------------------------------------------------------------------
// NodeIdentity discovery states
// ---------------------------------------------------------------------------
enum class DiscoveryState : uint8_t {
    WaitAEI,    // Waiting for AEI pin to go HIGH
    CheckNVS,   // AEI is HIGH – check NVS for saved id
    Announce,   // No saved id – sending ANNOUNCE and waiting for ASSIGN
    WaitAssign, // ANNOUNCE sent, waiting for base ASSIGN frame
    Rejoin,     // Saved id found – sending REJOIN
    Enabled,    // Identity resolved – AEO driven HIGH, normal operation
};

} // namespace vfm
