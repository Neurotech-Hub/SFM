#pragma once

#include <Arduino.h>
#include <AccelStepper.h>
#include "ServiceTypes.h"
#include "../hardware/VFMPins.h"

namespace vfm {

// ---------------------------------------------------------------------------
// Tunable defaults (override before begin() via setters)
// ---------------------------------------------------------------------------
// 28BYJ-48 half-step ≈ 4096 steps/rev.
// The load sensor (PG2) is not the drop height: M2 descends kDefaultGrabSteps
// further before M1 turns, and the raise is measured from that drop position.
constexpr float    kDefaultMotorSpeed      = 500.0f; // steps/s
// Approach budget. Worst case is an approach that starts from a seek-away taken
// at presentation height: (kDefaultRaiseSteps - kDefaultGrabSteps) +
// kDefaultSeekAwaySteps ≈ 1900 steps. 2048 left almost no margin for that path.
constexpr long     kDefaultLowerSteps      = 3072;   // max approach budget toward PG2
constexpr long     kDefaultSeekAwaySteps   = 800;    // M2 up to clear PG2 before approach
constexpr long     kDefaultGrabSteps       = 320;    // M2 down past PG2 to the drop position
constexpr long     kDefaultRaiseSteps      = 1420;   // M2 up travel from the drop position
constexpr long     kDefaultFeedMaxSteps    = 4096;   // M1 max steps before timeout
constexpr uint32_t kDefaultLowerTimeoutMs  = 8000;   // M2 lower / seek-away
constexpr uint32_t kDefaultFeedTimeoutMs   = 30000;  // M1 pellet load (30 s)
constexpr uint32_t kDefaultRaiseTimeoutMs  = 8000;   // M2 raise (step target)
constexpr uint32_t kSensorDebounceMs       = 20;
// Delivery / jam / warning timers
constexpr uint32_t kPelletTakenConfirmMs   = 200;    // presence clear → PelletTaken
constexpr uint32_t kPelletLostMs           = 500;    // presence clear during raise → PelletLost
constexpr uint32_t kLoadClearOnRaiseMs     = 5000;   // load sensor must clear after raise start
constexpr uint32_t kDomeOpenWarnMs         = 30000;  // dome open → DomeOpenWarning

// ---------------------------------------------------------------------------
class DispenserService {
public:
    DispenserService();

    ServiceStatus begin();
    void update();

    // Start a dispense cycle from Idle or Presented.
    // Occupancy is checked first: occupied → FeedSkipped (+ raise if needed).
    bool dispense();

    // Abort any phase, de-energise, return to Idle. Clears sticky Fault.
    void abort();

    DispenseState state() const { return state_; }
    uint32_t      pelletCount() const { return pelletCount_; }
    uint32_t      takenCount() const { return takenCount_; }
    DispenseEvent takeEvent();

    ServiceStatus faultCode() const { return lastFault_; }

    // Context latched with the last DomeOpened / PelletTaken event.
    bool lastDomeOpenedWithPellet() const { return lastDomeOpenedWithPellet_; }
    bool lastTakenWithDomeOpen() const { return lastTakenWithDomeOpen_; }

    // Sensors: PG1/PG2 beam break = pin LOW. PG3 dome open = pin HIGH.
    bool pg1() const { return pg1State_; } // pellet present on plate
    bool pg2() const { return pg2State_; } // at load position
    bool pg3() const { return pg3State_; } // dome open

    void setMotorSpeed(float stepsPerSec) {
        motorSpeed_ = stepsPerSec;
        motor1_.setMaxSpeed(motorSpeed_ * 2.0f);
        motor2_.setMaxSpeed(motorSpeed_ * 2.0f);
    }
    void setLowerSteps(long steps)            { lowerSteps_ = steps; }
    void setSeekAwaySteps(long steps)         { seekAwaySteps_ = steps; }
    void setGrabSteps(long steps)             { grabSteps_ = steps; }
    void setRaiseSteps(long steps)            { raiseSteps_ = steps; }
    void setFeedMaxSteps(long steps)          { feedMaxSteps_ = steps; }
    void setLowerTimeoutMs(uint32_t ms)       { lowerTimeoutMs_ = ms; }
    void setFeedTimeoutMs(uint32_t ms)        { feedTimeoutMs_ = ms; }
    void setRaiseTimeoutMs(uint32_t ms)       { raiseTimeoutMs_ = ms; }

    void setMotionTimeoutMs(uint32_t ms) {
        lowerTimeoutMs_ = feedTimeoutMs_ = raiseTimeoutMs_ = ms;
    }

private:
    AccelStepper motor1_;
    AccelStepper motor2_;

    DispenseState state_;
    DispenseEvent pendingEvent_;
    uint32_t      pelletCount_;
    uint32_t      takenCount_;
    ServiceStatus lastFault_;

    uint32_t motionStartMs_;
    long     motor2Target_;
    bool     pg3WasOpen_;
    bool     grabPhase_; // Lowering sub-phase: descending past PG2 to the drop position

    // M2 travel is measured against the position latched at each phase start,
    // never by re-zeroing the stepper mid-motion: AccelStepper derives the coil
    // pattern from currentPosition() & 0x7, so setCurrentPosition() on a moving
    // motor jumps the commutation phase and the actuator loses steps.
    long     phaseStartPos_; // M2 position at the start of the current phase
    long     feedStartPos_;  // M1 position at the start of the feed

    // True when the actuator is at or below the load sensor, where a downward
    // approach can never find PG2. PG2 itself cannot answer this: at the drop
    // position the flag has already passed out of the beam and reads clear.
    bool     belowLoad_;
    bool     approachRetried_; // one seek-away retry per dispense cycle

    uint32_t raiseStartMs_;
    uint32_t pg3OpenSinceMs_;
    uint32_t pelletClearSinceMs_; // presence clear timer (raise or presented)
    bool     domeWarnLatched_;
    bool     lastDomeOpenedWithPellet_;
    bool     lastTakenWithDomeOpen_;

    float    motorSpeed_;
    long     lowerSteps_;
    long     seekAwaySteps_;
    long     grabSteps_;
    long     raiseSteps_;
    long     feedMaxSteps_;
    uint32_t lowerTimeoutMs_;
    uint32_t feedTimeoutMs_;
    uint32_t raiseTimeoutMs_;

    bool     pg1State_, pg2State_, pg3State_;
    bool     pg1Raw_, pg2Raw_, pg3Raw_;
    uint32_t pg1LastChangeMs_, pg2LastChangeMs_, pg3LastChangeMs_;

    void updatePhotogates();
    void checkDomeOpenWarning();
    void setState(DispenseState next);
    void setEvent(DispenseEvent ev);
    void haltMotors();
    void faultNow(ServiceStatus code);
    bool phaseTimedOut(uint32_t timeoutMs) const;

    void startSeekAwayFromPg2();
    void startApproachPg2();
    void startGrabDescent();
    void startRaise(long steps);
    void startFeed();
    void beginLoweringPhase();
    void beginOccupiedDispense();
};

} // namespace vfm
