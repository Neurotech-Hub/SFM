#pragma once

#include <Arduino.h>

namespace sfm {

// ---------------------------------------------------------------------------
// Persistent storage (NVS) — one namespace shared by every service that
// stores a value. Keys live next to the service that owns them.
// ---------------------------------------------------------------------------
constexpr char kNvsNamespace[] = "sfm";

// ---------------------------------------------------------------------------
// Debounce applied to every sensor input before any logic or reporting acts on
// it: pellet, load position, dome, and animal presence all use this window, so
// one bout produces one trigger event and one clear event.
// ---------------------------------------------------------------------------
constexpr uint32_t kSensorDebounceMs = 100;

// ---------------------------------------------------------------------------
// General service status (returned from begin() and error paths)
// ---------------------------------------------------------------------------
enum class ServiceStatus : uint8_t {
    Ok = 0,
    NotInitialized,
    Jam,
    InvalidData,
    PelletLost,       // pellet left the plate during raise
    FeedTimeout,      // M1: no pellet confirmed — hopper empty / need refill
    ActuatorTimeout,  // M2: never reached load/raise target — sensor or motor
};

// ---------------------------------------------------------------------------
// Dispenser state machine states
//
// User-facing cycle (events mirror these phases):
//   Seeking (conditional) → Lowering → Loading → Raising → Loaded
// Loaded means the plate is at the top and ready for the mouse to take.
// ---------------------------------------------------------------------------
enum class DispenseState : uint8_t {
    Idle     = 0,
    Lowering = 1, // M2 down to the load sensor, then grabSteps_ past it (empty plate only)
    Loading  = 2, // M1 until the pellet sensor asserts
    Raising  = 3, // M2 up by raiseSteps_ from the pellet-drop position
    Loaded   = 4, // plate at top, ready for the mouse; ends on PelletTaken → Idle
    Seeking  = 5, // M2 up until load sensor clears or seekAwaySteps_ (before approach)
    Fault    = 6, // sticky until recover()
    Dwelling = 7, // no-feed: holding at the drop position, M1 idle, waiting for a
                  // peer node's Raising event (no timer — held until Recover)
};

// ---------------------------------------------------------------------------
// Dispenser events – one event is latched per transition.
// Read with DispenserService::takeEvent(); returns None if no new event.
// ---------------------------------------------------------------------------
enum class DispenseEvent : uint8_t {
    None = 0,
    OnPlate,          // pellet sensor confirmed during Loading; raise starting
    Loaded,           // plate reached the top — ready for the mouse (increments pelletCount)
    DomeOpened,       // dome lifted while Loaded
    Fault,            // FeedTimeout / ActuatorTimeout / Jam / PelletLost (see faultCode())
    DomeOpenWarning,  // dome open continuously > kDomeOpenWarnMs
    PelletTaken,      // pellet sensor cleared while Loaded → Idle
    FeedSkipped,      // Dispense with plate already occupied
    NoFeedPresented,  // no-feed raise complete; empty plate at the top (pelletCount NOT incremented)
};

// ---------------------------------------------------------------------------
// CAN command codes  (byte[0] of every command frame)
// Base -> Node on ID: 0x100 + nodeId   (0x100 = broadcast to all nodes)
// ---------------------------------------------------------------------------
enum class CanCmd : uint8_t {
    Ping              = 0x01,
    Dispense          = 0x02,
    Recover           = 0x03, // stop motion, clear sticky Fault, return to Idle
    AssignId          = 0x04, // payload byte[1] = new nodeId
    SetConfig         = 0x05, // payload TBD
    ReqStatus         = 0x06,
    ClearId           = 0x07, // clear NVS id; re-enter discovery (broadcast-friendly)
    DispenseNoFeed    = 0x08, // full dispense motion, M1 never runs; no payload (the
                              // raise is triggered by a peer node's Raising event).
                              // A trailing payload from an older base station is ignored.
    CalibratePresence = 0x09, // recalibrate the presence pad; cage MUST be empty for ~5s
    SyncFlash         = 0x0A, // status LED solid ON for N ms (camera sync); payload = duration ms LE16 (optional, default 500)
};

// ---------------------------------------------------------------------------
// CAN event codes  (byte[0] of every event frame)
// Node -> Base on ID: 0x300 + nodeId
// ---------------------------------------------------------------------------
enum class CanEvent : uint8_t {
    OnPlate           = 0x01, // pellet on plate during Loading; raise starting
    Loaded            = 0x02, // plate at top — ready for the mouse
    DomeOpened        = 0x03, // dome lift; extra: count LE16 + pellet_on_plate
    Fault             = 0x04, // payload byte[1] = ServiceStatus
    Pong              = 0x05,
    InputChanged      = 0x06, // payload: InputId(1), active(0/1)
    Lowering          = 0x07, // M2 toward load position
    Loading           = 0x08, // M1 running (Loading state)
    Raising           = 0x09, // M2 raising plate
    DomeOpenWarning   = 0x0A, // dome open > kDomeOpenWarnMs
    PelletTaken       = 0x0B, // extra: count LE16 + dome_open
    FeedSkipped       = 0x0C, // plate occupied on Dispense; lower/load skipped
    Seeking           = 0x0D, // M2 clearing the load sensor before Lowering (clear or step cap)
    NoFeedPresented   = 0x0E, // no-feed raise complete; extra: count LE16 (NOT incremented)
    Dwelling          = 0x0F, // phase: holding at the drop position, M1 idle
    PresenceCalResult = 0x10, // extra: ok(1), threshold LE32, samples LE16
    ConfigApplied     = 0x11, // extra: configType(1), ok(1), value LE32 (uint32 or float32 bit pattern, per configType)
};

// Input IDs carried by CanEvent::InputChanged.
// Wire values: 1 = pellet sensor, 2 = load position, 3 = dome, 4 = mouse presence.
enum class InputId : uint8_t {
    Pellet        = 0x01, // pellet sensor — pellet on plate
    LoadPosition  = 0x02, // load-position sensor
    Dome          = 0x03, // dome open/close sensor
    MousePresence = 0x04, // capacitive mouse-presence pad
};

// ---------------------------------------------------------------------------
// SetConfig sub-types (byte[0] of the SetConfig command payload)
// ---------------------------------------------------------------------------
enum class ConfigType : uint8_t {
    HeartbeatInterval = 0x01, // value = uint16 LE, heartbeat interval in ms
    PresenceFactor    = 0x02, // value = float32 LE; threshold = mean + factor * stdDev
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

} // namespace sfm
