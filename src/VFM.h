#pragma once

// ---------------------------------------------------------------------------
// VFM – Spatial Foraging Platform module library
//
// Facade class that aggregates all services. Typical sketch usage:
//
//   #include <VFM.h>
//   vfm::VFM vfm;
//
//   void setup() {
//       vfm.begin();            // init all services + start discovery
//   }
//   void loop() {
//       vfm.update();           // drive FSMs, pump CAN, heartbeat
//   }
// ---------------------------------------------------------------------------

#include "hardware/VFMPins.h"
#include "services/ServiceTypes.h"
#include "services/DispenserService.h"
#include "services/CanService.h"
#include "services/NodeIdentity.h"
#include "services/LedService.h"
#include "services/PresenceService.h"

namespace vfm {

class VFM {
public:
    VFM();

    // Initialise all services in the correct order.
    // Returns false if any critical service fails to initialise.
    bool begin();

    // Non-blocking main-loop update.
    // Call every loop iteration; order matters: identity -> CAN -> dispenser -> leds.
    void update();

    // --- Service accessors ---
    DispenserService &dispenser()   { return dispenser_; }
    CanService       &can()         { return can_; }
    NodeIdentity     &identity()    { return identity_; }
    LedService       &leds()        { return leds_; }
    PresenceService  &presence()    { return presence_; }

    // Mouse presence on the capacitive pad, debounced by kSensorDebounceMs.
    bool     mousePresent() const       { return presence_.present(); }
    bool     presenceDetected() const   { return mousePresent(); } // compatibility alias
    uint32_t presenceRaw() const       { return presence_.raw(); }
    uint32_t presenceThreshold() const { return presence_.threshold(); }

    // Recalibrate the presence pad against its idle noise (same as a short
    // press of PIN_BTN). The pad must stay clear for kPresenceCalMs.
    bool startPresenceCalibration()   { return presence_.startCalibration(); }
    bool presenceCalibrating() const  { return presence_.calibrating(); }

    // Calibration notifications, re-latched after VFM has driven the LEDs so a
    // sketch can report the result. Returns None when there is nothing new.
    PresenceEvent takePresenceEvent();

    // Force an immediate heartbeat regardless of the heartbeat timer.
    void sendHeartbeatNow();

    // Duration PIN_BTN must be held continuously to trigger an NVS ID clear.
    void setButtonHoldMs(uint32_t ms) { btnHoldMs_ = ms; }

    // Briefly blink the status LED (e.g. in response to a received Ping) so
    // the physical node can be located on the bench. Non-blocking.
    void blinkStatusLedForPing();

private:
    DispenserService dispenser_;
    CanService       can_;
    NodeIdentity     identity_;
    LedService       leds_;
    PresenceService  presence_;

    // Last input states published through CanEvent::InputChanged. These let
    // the GUI react immediately instead of waiting for the next heartbeat.
    bool reportedPellet_       = false;
    bool reportedLoadPosition_ = false;
    bool reportedDomeOpen_     = false;
    bool reportedPresence_ = false;

    // Last dispenser FSM state published as a phase event (Seeking / Lowering /
    // Loading / Raising). Heartbeats still carry the full state snapshot.
    DispenseState lastReportedDispenseState_ = DispenseState::Idle;

    // Button (PIN_BTN, active LOW): short click recalibrates presence, long
    // hold clears the NVS node ID.
    static constexpr uint32_t kBtnClickMinMs = 50; // shorter = contact bounce
    uint32_t btnHoldMs_       = 1000; // required hold duration
    uint32_t btnPressStartMs_ = 0;    // millis() when button first went LOW
    bool     btnWasPressed_   = false;
    bool     btnArmed_        = false; // true once hold threshold reached

    PresenceEvent pendingPresenceEvent_ = PresenceEvent::None;

    // Status LED blink triggered by a received Ping (visual "which node" aid)
    static constexpr uint32_t kPingBlinkMs         = 1500; // total blink duration
    static constexpr uint32_t kPingBlinkPeriodMs   = 150;  // blink toggle period
    uint32_t pingBlinkUntilMs_ = 0;    // millis() deadline; 0 = not blinking
    bool     pingBlinkActive_  = false;

    void handleDispenserEvents();
    void handleDispensePhaseEvents();
    void handleInputEvents();
    void sendInputChanged(InputId input, bool active);
    void sendPhaseEvent(CanEvent ev);
    void sendHeartbeatIfDue();
    void handlePresenceEvents();
    void updateButton();
    void updatePingBlink();
    void updateSensorLeds();          // LED 9 = dome open, LED 10 = pellet present
    void flashLedsClear();            // visual confirmation of NVS clear
};

} // namespace vfm
