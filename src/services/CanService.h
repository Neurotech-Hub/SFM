#pragma once

#include <Arduino.h>
#include <functional>
#include "driver/twai.h"
#include "ServiceTypes.h"
#include "../hardware/VFMPins.h"

namespace vfm {

// Heartbeat interval when no explicit SetConfig has been applied (milliseconds).
// Runtime-configurable via CanCmd::SetConfig / ConfigType::HeartbeatInterval
// (base-station GUI typically pushes 60s; not persisted to NVS).
constexpr uint32_t kDefaultHeartbeatIntervalMs = 5000;
// Reject near-zero intervals that would flood the bus.
constexpr uint32_t kMinHeartbeatIntervalMs = 100;

// ---------------------------------------------------------------------------
// Heartbeat payload packed into 8 bytes:
//   byte 0: DispenseState
//   byte 1: pelletCount (low byte) — Loaded milestones
//   byte 2: pelletCount (high byte)
//   byte 3: mouse presence (capacitive pad, 0/1)
//   byte 4: sensor bits
//           [2:dome open | 1:load position | 0:pellet on plate]
//   byte 5: fault code (ServiceStatus)
//   byte 6: takenCount (low byte) — pellets taken
//   byte 7: takenCount (high byte)
// ---------------------------------------------------------------------------
struct HeartbeatPayload {
    uint8_t dispenseState;
    uint8_t pelletCountLo;
    uint8_t pelletCountHi;
    uint8_t presence;
    uint8_t sensorBits;
    uint8_t faultCode;
    uint8_t takenCountLo;
    uint8_t takenCountHi;
};

// Callback type for received commands. Called from update() (no ISR context).
using CommandCallback = std::function<void(CanCmd cmd, const uint8_t *payload, uint8_t len)>;

// Callback for raw discovery frames (IDs 0x080-0x083).
// Receives the full TWAI frame identifier, payload pointer, and DLC.
using DiscoveryCallback = std::function<void(uint32_t frameId, const uint8_t *payload, uint8_t len)>;

// Callback for event frames emitted by OTHER nodes (0x300 + srcId, srcId != this node).
// Every node's TWAI filter is ACCEPT_ALL, so peer events already land in the RX queue —
// this exposes them so one node can react to another within a single CAN frame time
// (~0.5 ms at 250 kbps) instead of round-tripping through the base station.
// payload/len EXCLUDE the event byte, matching CommandCallback.
using PeerEventCallback =
    std::function<void(uint8_t srcId, CanEvent ev, const uint8_t *payload, uint8_t len)>;

// ---------------------------------------------------------------------------
class CanService {
public:
    CanService();

    // begin() installs the TWAI driver at 250 kbps.
    // nodeId: 1-based; 0 means "unassigned" (will still receive broadcasts).
    ServiceStatus begin(uint8_t nodeId = 0);

    // Shut down the TWAI driver cleanly.
    void end();

    // Non-blocking update: drain the TWAI RX queue and dispatch callbacks.
    void update();

    // --- Transmit helpers ---
    // Send a heartbeat frame (0x200 + nodeId) with the supplied payload.
    void sendHeartbeat(const HeartbeatPayload &p);

    // Send an event frame (0x300 + nodeId) immediately.
    void sendEvent(CanEvent ev, const uint8_t *extra = nullptr, uint8_t extraLen = 0);

    // Send a discovery frame (used by NodeIdentity).
    void sendDiscovery(uint32_t frameId, const uint8_t *payload, uint8_t len);

    // --- Callbacks ---
    void onCommand(CommandCallback cb)     { commandCb_   = cb; }
    void onDiscovery(DiscoveryCallback cb) { discoveryCb_ = cb; }
    void onPeerEvent(PeerEventCallback cb) { peerEventCb_ = cb; }

    // --- Heartbeat throttle ---
    // Returns true if heartbeat interval has elapsed; resets the timer.
    bool heartbeatDue();

    void setHeartbeatIntervalMs(uint32_t ms) { heartbeatIntervalMs_ = ms; }
    uint32_t heartbeatIntervalMs() const     { return heartbeatIntervalMs_; }

    // --- Node ID management ---
    void    setNodeId(uint8_t id) { nodeId_ = id; }
    uint8_t nodeId()        const { return nodeId_; }

    bool isStarted() const { return started_; }

private:
    uint8_t          nodeId_;
    bool             started_;
    uint32_t         heartbeatIntervalMs_;
    uint32_t         lastHeartbeatMs_;
    CommandCallback  commandCb_;
    DiscoveryCallback discoveryCb_;
    PeerEventCallback peerEventCb_;

    // Transmit a pre-built TWAI message; returns true on success.
    bool txMessage(const twai_message_t &msg);

    // Dispatch a received frame to the appropriate handler.
    void dispatchRx(const twai_message_t &msg);
};

} // namespace vfm
