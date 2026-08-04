#include "VFM.h"



namespace vfm {



VFM::VFM()

    : can_(),

      identity_(can_),

      btnHoldMs_(3000),

      btnPressStartMs_(0),

      btnWasPressed_(false),

      btnArmed_(false)

{}



// ---------------------------------------------------------------------------

bool VFM::begin() {

    bool ok = true;



    // 1. LEDs first – visual feedback during boot

    leds_.begin();

    leds_.setLed9BlinkMs(500); // LED 9 fast blink = booting



    // 2. NodeIdentity: configure pins, read MAC, restore NVS id

    if (identity_.begin() != ServiceStatus::Ok) {

        ok = false;

    }



    // 3. CAN bus: start TWAI driver

    uint8_t savedId = identity_.nodeId(); // may be 0 if NVS empty

    if (can_.begin(savedId) != ServiceStatus::Ok) {

        ok = false;

    }



    // 4. Register CanService command handler

    can_.onCommand([this](CanCmd cmd, const uint8_t *payload, uint8_t len) {

        switch (cmd) {

            case CanCmd::Dispense:

                dispenser_.dispense();

                break;

            case CanCmd::Recover:

                dispenser_.recover();

                break;

            case CanCmd::AssignId:

                if (len >= 1 && payload[0] > 0) {

                    identity_.assignId(payload[0]);

                    can_.setNodeId(payload[0]);

                }

                break;

            case CanCmd::Ping: {
                can_.sendEvent(CanEvent::Pong, identity_.mac(), 6);
                blinkStatusLedForPing();
                break;
            }

            case CanCmd::ReqStatus:

                sendHeartbeatNow();

                break;

            case CanCmd::SetConfig:

                if (len >= 1 && static_cast<ConfigType>(payload[0]) == ConfigType::HeartbeatInterval) {

                    if (len >= 3) {

                        uint16_t ms = static_cast<uint16_t>(payload[1]) |
                                      (static_cast<uint16_t>(payload[2]) << 8);

                        can_.setHeartbeatIntervalMs(ms);

                    }

                }

                break;

            case CanCmd::ClearId:

                // Wipe NVS ID, drop AEO, and wait for the base to re-drive
                // AEO HIGH so this node ANNOUNCE's for a fresh assignment.
                identity_.clearId();

                break;

            default:

                break;

        }

    });



    // 5. Dispenser hardware

    if (dispenser_.begin() != ServiceStatus::Ok) {

        ok = false;

    }



    // 6. Presence pad (threshold restored from NVS) and BTN (active LOW:
    //    click = recalibrate presence, long hold = clear NVS ID)

    presence_.begin();

    pinMode(PIN_BTN, INPUT_PULLUP);

    // Seed the edge-reporting snapshots from real inputs so startup levels do
    // not generate false InputChanged events.
    reportedPellet_       = dispenser_.pelletOnPlate();
    reportedLoadPosition_ = dispenser_.atLoadPosition();
    reportedDomeOpen_     = dispenser_.domeOpen();
    reportedPresence_ = presence_.present();
    lastReportedDispenseState_ = dispenser_.state();



    // 7. Start discovery FSM (requires CAN to be up)

    identity_.startDiscovery();



    if (ok) {

        leds_.setLed9BlinkMs(1000);       // LED 9 slow blink = waiting for discovery

        leds_.setStatusLedBlinkMs(1000);  // status LED slow blink = waiting for discovery

    }

    return ok;

}



// ---------------------------------------------------------------------------

void VFM::update() {

    can_.update();       // pump RX first so callbacks (discovery, commands) fire

    identity_.update();  // then act on any received discovery frames

    dispenser_.update();

    leds_.update();

    presence_.update();

    updateButton();

    handlePresenceEvents();

    handleInputEvents();

    // Milestone events (OnPlate / Loaded / Dome / Taken / Fault) first, then
    // phase-entry events (Lowering / Loading / Raising) so a same-tick
    // load→Raising transition logs as Loaded then Raising.
    handleDispenserEvents();

    handleDispensePhaseEvents();

    sendHeartbeatIfDue();

    updatePingBlink();



    // Once discovery completes, turn the status LED off — unless a Ping blink is
    // currently active, which takes precedence so the node stays visually
    // identifiable for its full blink duration.

    if (identity_.isEnabled() && !pingBlinkActive_) {

        leds_.setStatusLedBlinkMs(0);

        leds_.setStatusLed(false);

    }



    updateSensorLeds();



    // Status LED solid ON while in Fault state — always wins over a Ping blink.

    if (dispenser_.state() == DispenseState::Fault) {

        pingBlinkActive_  = false;

        pingBlinkUntilMs_ = 0;

        leds_.setStatusLedBlinkMs(0);

        leds_.setStatusLed(true);

    }

}



// ---------------------------------------------------------------------------

// Private

// ---------------------------------------------------------------------------



void VFM::handleDispenserEvents() {

    DispenseEvent ev = dispenser_.takeEvent();

    if (ev == DispenseEvent::None) return;



    CanEvent canEv;

    switch (ev) {

        case DispenseEvent::OnPlate: canEv = CanEvent::OnPlate; break;

        case DispenseEvent::Loaded:  canEv = CanEvent::Loaded;  break;

        case DispenseEvent::DomeOpened:      canEv = CanEvent::DomeOpened;      break;

        case DispenseEvent::PelletTaken:     canEv = CanEvent::PelletTaken;     break;

        case DispenseEvent::FeedSkipped:     canEv = CanEvent::FeedSkipped;     break;

        case DispenseEvent::DomeOpenWarning: canEv = CanEvent::DomeOpenWarning; break;

        case DispenseEvent::Fault:

            canEv = CanEvent::Fault;

            leds_.setStatusLedBlinkMs(0);

            leds_.setStatusLed(true);       // status LED solid ON = fault

            break;

        default: return;

    }



    // Clear status LED when returning to normal operation after a fault

    if (ev == DispenseEvent::OnPlate || ev == DispenseEvent::Loaded ||

        ev == DispenseEvent::DomeOpened || ev == DispenseEvent::PelletTaken ||

        ev == DispenseEvent::FeedSkipped) {

        leds_.setStatusLed(false);

    }



    if (ev == DispenseEvent::Fault) {

        uint8_t extra[1] = { static_cast<uint8_t>(dispenser_.faultCode()) };

        can_.sendEvent(canEv, extra, 1);

        return;

    }



    // count LE16 (+ optional context byte for DomeOpened / PelletTaken)

    uint8_t extra[3];

    uint32_t count = dispenser_.pelletCount();

    extra[0] = static_cast<uint8_t>(count & 0xFF);

    extra[1] = static_cast<uint8_t>((count >> 8) & 0xFF);

    if (ev == DispenseEvent::DomeOpened) {

        extra[2] = dispenser_.lastDomeOpenedWithPellet() ? 1 : 0;

        can_.sendEvent(canEv, extra, 3);

        return;

    }

    if (ev == DispenseEvent::PelletTaken) {

        // PelletTaken count uses takenCount in the cycle doc sense of
        // Loaded count still carried as LE16; context = dome open.
        count = dispenser_.takenCount();
        extra[0] = static_cast<uint8_t>(count & 0xFF);
        extra[1] = static_cast<uint8_t>((count >> 8) & 0xFF);
        extra[2] = dispenser_.lastTakenWithDomeOpen() ? 1 : 0;

        can_.sendEvent(canEv, extra, 3);

        return;

    }

    can_.sendEvent(canEv, extra, 2);

}


// ---------------------------------------------------------------------------
// Publish dispenser phase entries in real time (not waiting for heartbeat):
//   Seeking  — M2 clearing the load sensor before approach
//   Lowering — M2 approaching the load position
//   Loading  — M1 loading a pellet
//   Raising  — M2 raising after load / FeedSkipped
// OnPlate and Loaded milestones are sent by handleDispenserEvents().
// ---------------------------------------------------------------------------

void VFM::handleDispensePhaseEvents() {

    if (!identity_.isEnabled() || can_.nodeId() == 0) return;

    DispenseState s = dispenser_.state();
    if (s == lastReportedDispenseState_) return;

    lastReportedDispenseState_ = s;

    switch (s) {
        case DispenseState::Seeking:
            sendPhaseEvent(CanEvent::Seeking);
            break;

        case DispenseState::Lowering:
            sendPhaseEvent(CanEvent::Lowering);
            break;

        case DispenseState::Loading:
            sendPhaseEvent(CanEvent::Loading);
            break;

        case DispenseState::Raising:
            sendPhaseEvent(CanEvent::Raising);
            break;

        default:
            break;
    }
}


void VFM::sendPhaseEvent(CanEvent ev) {

    uint8_t extra[2];
    uint32_t count = dispenser_.pelletCount();
    extra[0] = static_cast<uint8_t>(count & 0xFF);
    extra[1] = static_cast<uint8_t>((count >> 8) & 0xFF);
    can_.sendEvent(ev, extra, 2);
}


// ---------------------------------------------------------------------------
// Publish every debounced input edge immediately. Heartbeats remain the
// periodic state snapshot/recovery mechanism; these events are the real-time
// path used by the GUI event log and circular input indicators.
// ---------------------------------------------------------------------------

void VFM::handleInputEvents() {

    // Do not publish operational events until this node has a valid CAN ID.
    if (!identity_.isEnabled() || can_.nodeId() == 0) return;

    bool pellet = dispenser_.pelletOnPlate();
    bool loadPosition = dispenser_.atLoadPosition();
    bool domeOpen = dispenser_.domeOpen();

    if (pellet != reportedPellet_) {
        reportedPellet_ = pellet;
        sendInputChanged(InputId::Pellet, pellet);
    }
    if (loadPosition != reportedLoadPosition_) {
        reportedLoadPosition_ = loadPosition;
        sendInputChanged(InputId::LoadPosition, loadPosition);
    }
    if (domeOpen != reportedDomeOpen_) {
        reportedDomeOpen_ = domeOpen;
        sendInputChanged(InputId::Dome, domeOpen);
    }
    bool mousePresence = presence_.present();
    if (mousePresence != reportedPresence_) {
        reportedPresence_ = mousePresence;
        sendInputChanged(InputId::MousePresence, mousePresence);
    }
}


// ---------------------------------------------------------------------------
// Presence calibration owns LED 9 for the duration of the capture: solid ON
// while sampling, then the same confirm flash used for an NVS ID clear so the
// bench sees one pattern for "stored something".
// ---------------------------------------------------------------------------

void VFM::handlePresenceEvents() {

    PresenceEvent ev = presence_.takeEvent();
    if (ev == PresenceEvent::None) return;

    switch (ev) {
        case PresenceEvent::CalibrationStarted:
            leds_.setLed9BlinkMs(0);
            leds_.setLed9(true);
            break;

        case PresenceEvent::CalibrationDone:
            leds_.setLed9(false);
            leds_.flashConfirm();
            break;

        case PresenceEvent::CalibrationFailed:
            leds_.setLed9(false);
            break;

        default:
            break;
    }

    pendingPresenceEvent_ = ev;
}


PresenceEvent VFM::takePresenceEvent() {

    PresenceEvent ev = pendingPresenceEvent_;
    pendingPresenceEvent_ = PresenceEvent::None;
    return ev;
}


void VFM::sendInputChanged(InputId input, bool active) {

    uint8_t payload[2] = {
        static_cast<uint8_t>(input),
        static_cast<uint8_t>(active ? 1 : 0)
    };
    can_.sendEvent(CanEvent::InputChanged, payload, 2);
}



static HeartbeatPayload buildHeartbeat(const DispenserService &d, bool presence) {

    HeartbeatPayload p = {};

    p.dispenseState  = static_cast<uint8_t>(d.state());

    uint32_t count   = d.pelletCount();

    p.pelletCountLo  = static_cast<uint8_t>(count & 0xFF);

    p.pelletCountHi  = static_cast<uint8_t>((count >> 8) & 0xFF);

    p.presence       = presence ? 1 : 0;

    p.sensorBits     = (d.pelletOnPlate() ? 0x01 : 0) |

                       (d.atLoadPosition() ? 0x02 : 0) |

                       (d.domeOpen() ? 0x04 : 0);

    p.faultCode      = static_cast<uint8_t>(d.faultCode());

    uint32_t taken     = d.takenCount();

    p.takenCountLo   = static_cast<uint8_t>(taken & 0xFF);

    p.takenCountHi   = static_cast<uint8_t>((taken >> 8) & 0xFF);

    return p;

}



void VFM::sendHeartbeatIfDue() {

    if (!can_.heartbeatDue()) return;

    can_.sendHeartbeat(buildHeartbeat(dispenser_, presence_.present()));

}



void VFM::sendHeartbeatNow() {

    can_.sendHeartbeat(buildHeartbeat(dispenser_, presence_.present()));

}



void VFM::blinkStatusLedForPing() {

    // Don't interrupt a solid fault indication with a blink.
    if (dispenser_.state() == DispenseState::Fault) return;

    pingBlinkActive_  = true;
    pingBlinkUntilMs_ = millis() + kPingBlinkMs;
    leds_.setStatusLedBlinkMs(kPingBlinkPeriodMs);

}



void VFM::updatePingBlink() {

    if (!pingBlinkActive_) return;

    if ((int32_t)(millis() - pingBlinkUntilMs_) >= 0) {
        pingBlinkActive_  = false;
        pingBlinkUntilMs_ = 0;
        leds_.setStatusLedBlinkMs(0);
        leds_.setStatusLed(false);
    }

}



// Live sensor mirrors: LED 10 = pellet present, LED 9 = dome open. Lit means
// asserted. Both read the debounced states from DispenserService, so the LEDs
// show what the firmware acts on rather than the raw pin.
//
// LED 10 has no other owner and mirrors unconditionally. LED 9 is shared with
// the boot / discovery blink, the button-hold warning, and the presence
// calibration capture, all of which keep it until they are done.

void VFM::updateSensorLeds() {

    leds_.setLed10(dispenser_.pelletOnPlate());

    if (identity_.isEnabled() && !btnArmed_ && !presence_.calibrating()) {

        leds_.setLed9BlinkMs(0);

        leds_.setLed9(dispenser_.domeOpen());

    }

}



// ---------------------------------------------------------------------------

// Button: PIN_BTN is INPUT_PULLUP; button press drives it LOW.

//

// Behaviour:

//   - Short click (kBtnClickMinMs .. btnHoldMs_) → recalibrate the presence pad.

//   - Press and hold for btnHoldMs_ → LED 9 blinks rapidly as visual warning.

//   - Release after hold threshold → NVS ID cleared; status/LED9/LED10 flash 3x.

//   - Release under kBtnClickMinMs  → no action (contact bounce ignored).

// ---------------------------------------------------------------------------

void VFM::updateButton() {

    bool pressed = (digitalRead(PIN_BTN) == LOW);



    if (pressed) {

        if (!btnWasPressed_) {

            // Leading edge: record press start

            btnPressStartMs_ = millis();

            btnWasPressed_   = true;

            btnArmed_        = false;

        }



        uint32_t heldMs = millis() - btnPressStartMs_;



        if (!btnArmed_ && heldMs >= btnHoldMs_) {

            // Hold threshold reached – arm and start rapid blink as warning

            btnArmed_ = true;

            leds_.setLed9BlinkMs(100);

        }

    } else {

        if (btnWasPressed_) {

            // Trailing edge

            uint32_t heldMs = millis() - btnPressStartMs_;

            if (btnArmed_) {

                // Held long enough – clear NVS ID and confirm visually

                identity_.clearId();

                leds_.setLed9BlinkMs(0);

                flashLedsClear();

            } else if (heldMs >= kBtnClickMinMs) {

                // A click is the recalibrate gesture. The hold branch above
                // already consumed this press, so the two never both fire.

                presence_.startCalibration();

            }

            btnWasPressed_ = false;

            btnArmed_      = false;

        }

    }

}



// Three rapid flashes on status, LED 9, and LED 10 to confirm NVS ID was cleared.

// This is the one intentional blocking call in the library; it runs for

// ~600 ms total and only fires on a deliberate 3-second button hold.

void VFM::flashLedsClear() {
    // Shared confirm pattern lives in LedService (also used by MousePresenceTest).
    leds_.flashConfirm();
}



} // namespace vfm


