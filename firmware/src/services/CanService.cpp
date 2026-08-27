#include "CanService.h"

namespace sfm {

CanService::CanService()
    : nodeId_(0),
      started_(false),
      heartbeatIntervalMs_(kDefaultHeartbeatIntervalMs),
      lastHeartbeatMs_(0),
      commandCb_(nullptr),
      discoveryCb_(nullptr),
      peerEventCb_(nullptr)
{}

// ---------------------------------------------------------------------------
ServiceStatus CanService::begin(uint8_t nodeId) {
    nodeId_ = nodeId;

    // General driver configuration
    twai_general_config_t gConfig = TWAI_GENERAL_CONFIG_DEFAULT(
        (gpio_num_t)PIN_CAN_TX,
        (gpio_num_t)PIN_CAN_RX,
        TWAI_MODE_NORMAL
    );
    // Remove the default alert flags to avoid alert flooding on quiet bus
    gConfig.alerts_enabled = TWAI_ALERT_NONE;

    // 250 kbps timing (standard for CAN in lab-bench topologies)
    twai_timing_config_t tConfig = TWAI_TIMING_CONFIG_250KBITS();

    // Accept all frames (software filtering in dispatchRx)
    twai_filter_config_t fConfig = TWAI_FILTER_CONFIG_ACCEPT_ALL();

    if (twai_driver_install(&gConfig, &tConfig, &fConfig) != ESP_OK) {
        return ServiceStatus::NotInitialized;
    }
    if (twai_start() != ESP_OK) {
        twai_driver_uninstall();
        return ServiceStatus::NotInitialized;
    }

    started_ = true;
    lastHeartbeatMs_ = millis();
    return ServiceStatus::Ok;
}

// ---------------------------------------------------------------------------
void CanService::end() {
    if (!started_) return;
    twai_stop();
    twai_driver_uninstall();
    started_ = false;
}

// ---------------------------------------------------------------------------
void CanService::update() {
    if (!started_) return;

    // Drain up to 8 frames per update() call to avoid monopolising the loop
    for (int i = 0; i < 8; i++) {
        twai_message_t msg;
        if (twai_receive(&msg, 0) == ESP_OK) {
            dispatchRx(msg);
        } else {
            break;
        }
    }
}

// ---------------------------------------------------------------------------
void CanService::sendHeartbeat(const HeartbeatPayload &p) {
    if (!started_) return;

    twai_message_t msg = {};
    msg.identifier     = CAN_STATUS_BASE + nodeId_;
    msg.data_length_code = 8;
    msg.data[0] = p.dispenseState;
    msg.data[1] = p.pelletCountLo;
    msg.data[2] = p.pelletCountHi;
    msg.data[3] = p.presence;
    msg.data[4] = p.sensorBits;
    msg.data[5] = p.faultCode;
    msg.data[6] = p.takenCountLo;
    msg.data[7] = p.takenCountHi;

    txMessage(msg);
    lastHeartbeatMs_ = millis();
}

// ---------------------------------------------------------------------------
void CanService::sendEvent(CanEvent ev, const uint8_t *extra, uint8_t extraLen) {
    if (!started_) return;

    twai_message_t msg = {};
    msg.identifier       = CAN_EVENT_BASE + nodeId_;
    msg.data_length_code = min((uint8_t)(1 + extraLen), (uint8_t)8);
    msg.data[0]          = static_cast<uint8_t>(ev);
    for (uint8_t i = 0; i < extraLen && (1 + i) < 8; i++) {
        msg.data[1 + i] = extra[i];
    }

    txMessage(msg);
}

// ---------------------------------------------------------------------------
void CanService::sendDiscovery(uint32_t frameId, const uint8_t *payload, uint8_t len) {
    if (!started_) return;

    twai_message_t msg = {};
    msg.identifier       = frameId;
    msg.data_length_code = min(len, (uint8_t)8);
    for (uint8_t i = 0; i < msg.data_length_code; i++) {
        msg.data[i] = payload[i];
    }

    txMessage(msg);
}

// ---------------------------------------------------------------------------
bool CanService::heartbeatDue() {
    if ((millis() - lastHeartbeatMs_) >= heartbeatIntervalMs_) {
        lastHeartbeatMs_ = millis();
        return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// Private
// ---------------------------------------------------------------------------

bool CanService::txMessage(const twai_message_t &msg) {
    // Non-blocking: timeout=0 — if TX queue is full, frame is dropped.
    return twai_transmit(&msg, 0) == ESP_OK;
}

void CanService::dispatchRx(const twai_message_t &msg) {
    uint32_t id = msg.identifier;

    // Command frames addressed to this node or the broadcast address
    bool isBroadcast = (id == CAN_CMD_BROADCAST);
    bool isMyCmd     = (nodeId_ > 0) && (id == (CAN_CMD_BASE + nodeId_));

    if ((isBroadcast || isMyCmd) && msg.data_length_code >= 1) {
        CanCmd cmd = static_cast<CanCmd>(msg.data[0]);
        if (commandCb_) {
            commandCb_(cmd, msg.data + 1, msg.data_length_code - 1);
        }
        return;
    }

    // Event frames from other nodes (0x300 + srcId). The hardware filter accepts
    // every frame, so these already arrive here — surfacing them lets a node act on
    // a peer's phase change (see DispenserService::notifyPeerRaise) without a
    // base-station round trip. TWAI_MODE_NORMAL does not loop back our own frames,
    // but the srcId check makes that explicit rather than implied.
    if (id > CAN_EVENT_BASE && id < (CAN_EVENT_BASE + 0x100) &&
        msg.data_length_code >= 1) {
        uint8_t srcId = static_cast<uint8_t>(id - CAN_EVENT_BASE);
        if (srcId != nodeId_ && peerEventCb_) {
            peerEventCb_(srcId, static_cast<CanEvent>(msg.data[0]),
                         msg.data + 1, msg.data_length_code - 1);
        }
        return;
    }

    // Discovery frames (ANNOUNCE / ASSIGN / ACK / REJOIN)
    bool isDiscovery = (id == CAN_ID_ANNOUNCE || id == CAN_ID_ASSIGN ||
                        id == CAN_ID_ACK       || id == CAN_ID_REJOIN);
    if (isDiscovery && discoveryCb_) {
        discoveryCb_(id, msg.data, msg.data_length_code);
    }
}

} // namespace sfm
