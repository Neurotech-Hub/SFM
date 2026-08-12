#include "DispenserService.h"

namespace vfm {

// ---------------------------------------------------------------------------
// AccelStepper HALF4WIRE: (A1, A3, A2, A4) = Orange, Pink, Yellow, Blue
// M2 direction: +speed = UP (forward), -speed = DOWN (reverse)
//
// CRITICAL: use setSpeed()+runSpeed() only. Never AccelStepper::stop()/move()/
// run() — stop() issues move() and corrupts constant-speed mode.
// ---------------------------------------------------------------------------
DispenserService::DispenserService()
    : motor1_(AccelStepper::HALF4WIRE, PIN_M1_A1, PIN_M1_A3, PIN_M1_A2, PIN_M1_A4),
      motor2_(AccelStepper::HALF4WIRE, PIN_M2_A1, PIN_M2_A3, PIN_M2_A2, PIN_M2_A4),
      state_(DispenseState::Idle),
      pendingEvent_(DispenseEvent::None),
      pelletCount_(0),
      takenCount_(0),
      lastFault_(ServiceStatus::Ok),
      motionStartMs_(0),
      motor2Target_(0),
      pg3WasOpen_(false),
      grabPhase_(false),
      noFeed_(false),
      phaseStartPos_(0),
      belowLoad_(false),
      approachRetried_(false),
      seekUntilClear_(false),
      raiseStartMs_(0),
      pg3OpenSinceMs_(0),
      pelletClearSinceMs_(0),
      pelletSeenSinceMs_(0),
      dwellStartMs_(0),
      dwellMs_(kDefaultNoFeedDwellMs),
      domeWarnLatched_(false),
      lastDomeOpenedWithPellet_(false),
      lastTakenWithDomeOpen_(false),
      motorSpeed_(kDefaultMotorSpeed),
      feedSpeedScale_(kDefaultFeedSpeedScale),
      feedBurstSteps_(kDefaultFeedBurstSteps),
      feedPauseMs_(kDefaultFeedPauseMs),
      feedBurstLeft_(0),
      feedPauseUntilMs_(0),
      lowerSteps_(kDefaultLowerSteps),
      seekAwaySteps_(kDefaultSeekAwaySteps),
      grabSteps_(kDefaultGrabSteps),
      raiseSteps_(kDefaultRaiseSteps),
      lowerTimeoutMs_(kDefaultLowerTimeoutMs),
      feedTimeoutMs_(kDefaultFeedTimeoutMs),
      raiseTimeoutMs_(kDefaultRaiseTimeoutMs),
      pg1State_(false), pg2State_(false), pg3State_(false),
      pg1Raw_(false),   pg2Raw_(false),   pg3Raw_(false),
      pg1LastChangeMs_(0), pg2LastChangeMs_(0), pg3LastChangeMs_(0)
{}

ServiceStatus DispenserService::begin() {
    pinMode(PIN_PG1, INPUT_PULLUP);
    pinMode(PIN_PG2, INPUT_PULLUP);
    pinMode(PIN_PG3, INPUT_PULLDOWN);

    motor1_.setMaxSpeed(motorSpeed_ * 2.0f);
    motor2_.setMaxSpeed(motorSpeed_ * 2.0f);
    haltMotors();

    uint32_t now = millis();
    pg1Raw_ = (digitalRead(PIN_PG1) == LOW);
    pg2Raw_ = (digitalRead(PIN_PG2) == LOW);
    pg3Raw_ = (digitalRead(PIN_PG3) == HIGH);
    pg1State_ = pg1Raw_;
    pg2State_ = pg2Raw_;
    pg3State_ = pg3Raw_;
    pg1LastChangeMs_ = pg2LastChangeMs_ = pg3LastChangeMs_ = now;
    pg3WasOpen_ = pg3State_;
    pg3OpenSinceMs_ = pg3State_ ? now : 0;
    pelletClearSinceMs_ = 0;
    pelletSeenSinceMs_ = 0;
    domeWarnLatched_ = false;
    grabPhase_ = false;
    approachRetried_ = false;
    phaseStartPos_ = motor2_.currentPosition();
    // Height is unknown at boot. An asserted load sensor proves the load position; clear is

    belowLoad_ = pg2State_;
    lastFault_ = ServiceStatus::Ok;

    return ServiceStatus::Ok;
}

void DispenserService::update() {
    updatePhotogates();

    if (state_ != DispenseState::Fault) {
        checkDomeOpenWarning();
    }

    switch (state_) {
        case DispenseState::Idle:
        case DispenseState::Fault:
            break;

        case DispenseState::Seeking:
            // Cap at seekAwaySteps_. When entered from the load sensor (pg2
            // asserted), also stop as soon as the beam clears — whichever first.
            // Fixed-only seeks (seekUntilClear_ false) are for a known drop
            // position where the sensor is already clear.
            if (phaseTimedOut(lowerTimeoutMs_)) {
                faultNow(ServiceStatus::ActuatorTimeout);
                break;
            }
            {
                const long traveled =
                    motor2_.currentPosition() - phaseStartPos_;
                const bool hitCap = traveled >= seekAwaySteps_;
                const bool cleared = seekUntilClear_ && !pg2State_;
                if (hitCap || cleared) {
                    startApproachPg2();
                    setState(DispenseState::Lowering);
                } else {
                    motor2_.runSpeed();
                }
            }
            break;

        case DispenseState::Lowering:
            if (!grabPhase_) {
                // Approach: down until the load sensor asserts. Whichever budget
                // runs out first means the same thing — the load sensor was never reached —
                // so both route through the retry rather than straight to Fault.
                if (pg2State_) {
                    startGrabDescent(); // no halt; grab branch runs this same tick
                } else if (phaseTimedOut(lowerTimeoutMs_) ||
                           labs(motor2_.currentPosition() - phaseStartPos_) >= lowerSteps_) {
                    // Approach missed the load sensor (beam still clear). Only
                    // raise to seek when we already know the plate is at drop
                    // depth.
                    if (approachRetried_ || !belowLoad_) {
                        faultNow(ServiceStatus::ActuatorTimeout);
                    } else {
                        approachRetried_ = true;
                        startSeekAwayFromPg2(false);
                        setState(DispenseState::Seeking);
                    }
                    break;
                }
            } else if (phaseTimedOut(lowerTimeoutMs_)) {
                faultNow(ServiceStatus::ActuatorTimeout);
                break;
            }
            if (grabPhase_) {
                // Grab descent: fixed travel past the load sensor to the drop position.
                // The load sensor is deliberately ignored here (same as ActuatorCalTest 'd <n>').
                if (labs(motor2_.currentPosition() - phaseStartPos_) >= grabSteps_) {
                    haltMotors();
                    if (noFeed_) {
                        // No-feed cycle: M1 never runs. Hold here for the
                        // commanded dwell so the acoustic/vibration signature
                        // matches a fed dispense, then raise identically.
                        startDwell();
                        setState(DispenseState::Dwelling);
                    } else {
                        startFeed();
                        setState(DispenseState::Loading);
                    }
                } else {
                    motor2_.runSpeed();
                }
            } else {
                motor2_.runSpeed();
            }
            break;

        case DispenseState::Dwelling:
            // Motors already halted by the grab-descent hand-off. Nothing is
            // being loaded, so no PG1 guard and no feed budget apply here —
            // dwellMs_ is itself the bound (clamped at command time).
            if ((millis() - dwellStartMs_) >= dwellMs_) {
                startRaise(raiseSteps_); // identical travel to a fed cycle
                setState(DispenseState::Raising);
            }
            break;

        case DispenseState::Loading:
            if (phaseTimedOut(feedTimeoutMs_)) {
                faultNow(ServiceStatus::FeedTimeout);
                break;
            }

            // Stop M1 the instant the raw beam breaks — do not wait for the
            // 100 ms debounce, and do not keep stepping into a second pellet.
            if (pg1Raw_) {
                stopFeedMotor();
            }

            if (pg1State_) {
                if (pelletSeenSinceMs_ == 0) {
                    // Debounced sighting: hold and confirm. Motor already
                    // stopped on the raw edge above when the beam first broke.
                    pelletSeenSinceMs_ = millis();
                    stopFeedMotor();
                } else if ((millis() - pelletSeenSinceMs_) >= kPelletLoadConfirmMs) {
                    // Held for the full window: a pellet is genuinely on the plate,
                    // not a fragment tumbling past the beam.
                    haltMotors();
                    pelletSeenSinceMs_ = 0;
                    setEvent(DispenseEvent::OnPlate);
                    startRaise(raiseSteps_); // from the drop position
                    setState(DispenseState::Raising);
                }
            } else {
                if (pelletSeenSinceMs_ != 0) {
                    // The sighting did not hold — nothing settled on the plate.
                    // Resume the run-pause pattern within the same feed budget.
                    pelletSeenSinceMs_ = 0;
                    beginFeedBurst();
                }
                // Stay quiet while the raw beam is still flickering high so we
                // do not re-energise into a pellet that has not yet debounced.
                if (!pg1Raw_) {
                    updateFeedMotor();
                }
            }
            break;

        case DispenseState::Raising:
            if (!pg2State_) {
                // Beam cleared → plate is above the load sensor again.
                belowLoad_ = false;
            }
            // Motion guard: still valid on a no-feed raise. The plate must
            // clear the load sensor whether or not it carries a pellet.
            if (pg2State_ &&
                (millis() - raiseStartMs_) >= kLoadClearOnRaiseMs) {
                faultNow(ServiceStatus::Jam);
                break;
            }
            // Pellet guard: only meaningful when a pellet was loaded. A
            // no-feed raise ALWAYS has PG1 clear, so running this would
            // fault every cycle.
            if (!noFeed_) {
                if (!pg1State_) {
                    if (pelletClearSinceMs_ == 0) {
                        pelletClearSinceMs_ = millis();
                    } else if ((millis() - pelletClearSinceMs_) >= kPelletLostMs) {
                        faultNow(ServiceStatus::PelletLost);
                        break;
                    }
                } else {
                    pelletClearSinceMs_ = 0;
                }
            }
            if (phaseTimedOut(raiseTimeoutMs_)) {
                faultNow(ServiceStatus::ActuatorTimeout);
                break;
            }
            if (motor2_.currentPosition() >= motor2Target_) {
                haltMotors();
                if (noFeed_) {
                    // No pellet was delivered: do NOT emit Loaded and do NOT
                    // touch pelletCount_, so the base station's `pellets`
                    // counter (and end_after(pellets=...)) does not move for
                    // an unrewarded cycle.
                    setEvent(DispenseEvent::NoFeedPresented);
                } else {
                    setEvent(DispenseEvent::Loaded);
                    pelletCount_++;
                }
                pg3WasOpen_ = pg3State_;
                pelletClearSinceMs_ = 0;
                belowLoad_ = false; // the raise completed; plate is above the load sensor
                setState(DispenseState::Loaded);
            } else {
                motor2_.runSpeed();
            }
            break;

        case DispenseState::Loaded:
            if (pg3State_ && !pg3WasOpen_) {
                lastDomeOpenedWithPellet_ = pg1State_; // false on a no-feed cycle
                setEvent(DispenseEvent::DomeOpened);
            }
            pg3WasOpen_ = pg3State_;

            if (noFeed_) {
                // Empty plate: there is no pellet to take, so the
                // presence-clear timer must not run and PelletTaken must
                // never fire. Dome bouts are still reported above (and
                // DomeOpenWarning still runs — checkDomeOpenWarning() is
                // outside this switch). The cycle ends on the next
                // Dispense/DispenseNoFeed (both accept Loaded) or Recover.
                break;
            }

            if (!pg1State_) {
                if (pelletClearSinceMs_ == 0) {
                    pelletClearSinceMs_ = millis();
                } else if ((millis() - pelletClearSinceMs_) >= kPelletTakenConfirmMs) {
                    if (pendingEvent_ == DispenseEvent::None ||
                        pendingEvent_ == DispenseEvent::DomeOpened) {
                        lastTakenWithDomeOpen_ = pg3State_;
                        takenCount_++;
                        setEvent(DispenseEvent::PelletTaken);
                        haltMotors();
                        setState(DispenseState::Idle);
                        pelletClearSinceMs_ = 0;
                    }
                }
            } else {
                pelletClearSinceMs_ = 0;
            }
            break;
    }
}

bool DispenserService::dispense() {
    if (state_ != DispenseState::Idle && state_ != DispenseState::Loaded) {
        return false;
    }

    haltMotors();
    pelletClearSinceMs_ = 0;
    noFeed_ = false; // clear any stale flag from a preceding no-feed cycle

    // Occupancy first — pellet sensor is on the plate at all times.
    if (pg1State_) {
        beginOccupiedDispense();
        return true;
    }

    beginLoweringPhase();
    return true;
}

bool DispenserService::dispenseNoFeed(uint16_t dwellMs) {
    if (state_ != DispenseState::Idle && state_ != DispenseState::Loaded) {
        return false;
    }

    haltMotors();
    pelletClearSinceMs_ = 0;

    // Occupancy first, same as dispense(). A real pellet is already on the
    // plate: present it honestly as a normal FeedSkipped cycle rather than
    // silently discarding it — noFeed_ MUST be cleared here, otherwise the
    // Raising/Loaded no-feed branches would run with a real pellet
    // aboard and disable both PelletLost and PelletTaken for it.
    if (pg1State_) {
        noFeed_ = false;
        beginOccupiedDispense();
        return true;
    }

    noFeed_  = true;
    dwellMs_ = constrain(dwellMs, kNoFeedDwellMinMs, kNoFeedDwellMaxMs);
    beginLoweringPhase();
    return true;
}

void DispenserService::beginOccupiedDispense() {
    setEvent(DispenseEvent::FeedSkipped);

    if (pg2State_) {
        long steps = raiseSteps_ - grabSteps_;
        startRaise(steps > 0 ? steps : raiseSteps_);
        setState(DispenseState::Raising);
        return;
    }
    if (belowLoad_) {
        startRaise(raiseSteps_);
        setState(DispenseState::Raising);
        return;
    }
    // Already elevated: stay/return to Loaded without motion.
    pg3WasOpen_ = pg3State_;
    setState(DispenseState::Loaded);
}

void DispenserService::recover() {
    haltMotors();
    lastFault_ = ServiceStatus::Ok;
    pelletClearSinceMs_ = 0;
    pelletSeenSinceMs_ = 0;
    grabPhase_ = false;
    noFeed_ = false;
    // Asserted load sensor ⇒ at load. Clear does not prove elevated: the drop
    // position also reads clear, so preserve belowLoad_ when the beam is open.
    // (PelletLost mid-raise already cleared belowLoad_ when the sensor opened.)
    if (pg2State_) {
        belowLoad_ = true;
    }
    setState(DispenseState::Idle);
}

DispenseEvent DispenserService::takeEvent() {
    DispenseEvent ev = pendingEvent_;
    pendingEvent_ = DispenseEvent::None;
    return ev;
}

// ---------------------------------------------------------------------------
void DispenserService::beginLoweringPhase() {
    motionStartMs_ = millis();
    grabPhase_ = false;
    approachRetried_ = false;

    // Seek-up only when safe:
    //   - load sensor asserted → at load position; raise until clear or cap
    //   - belowLoad_ known → at drop depth (sensor already clear); fixed raise
    // A clear sensor with unknown height means approach down. Never invent a
    // seek from "maybe below" — that is what smashed the stop after PelletLost.
    if (pg2State_) {
        startSeekAwayFromPg2(true);
        setState(DispenseState::Seeking);
    } else if (belowLoad_) {
        startSeekAwayFromPg2(false);
        setState(DispenseState::Seeking);
    } else {
        startApproachPg2();
        setState(DispenseState::Lowering);
    }
}

void DispenserService::startSeekAwayFromPg2(bool untilClear) {
    seekUntilClear_ = untilClear;
    motor2_.enableOutputs();
    phaseStartPos_ = motor2_.currentPosition();
    motionStartMs_ = millis();
    motor2_.setSpeed(motorSpeed_); // UP
}

void DispenserService::startApproachPg2() {
    motor2_.enableOutputs();
    phaseStartPos_ = motor2_.currentPosition();
    motionStartMs_ = millis();
    motor2_.setSpeed(-motorSpeed_); // DOWN
}

// Continuation of the approach: load sensor asserted; keep going DOWN by grabSteps_
// to the height at which M1 can drop a pellet onto the plate. Nothing is done to
// the motor here — it is already energised and already running at -motorSpeed_,
// and this is the same physical move. Only the measurement datum moves.
void DispenserService::startGrabDescent() {
    if (grabPhase_) return;
    grabPhase_ = true;
    belowLoad_ = true;
    phaseStartPos_ = motor2_.currentPosition();
    motionStartMs_ = millis();
}

void DispenserService::startFeed() {
    motionStartMs_ = millis();
    pelletSeenSinceMs_ = 0;
    beginFeedBurst();
}

// One burst of feedBurstSteps_ at feed speed, then a feedPauseMs_ coil-off pause
// (see StepperMotorTest continuous mode). Uses setSpeed()+runSpeed() only.
void DispenserService::beginFeedBurst() {
    feedBurstLeft_    = feedBurstSteps_;
    feedPauseUntilMs_ = 0;
    motor1_.enableOutputs();
    motor1_.setCurrentPosition(0);
    motor1_.setSpeed(motorSpeed_ * feedSpeedScale_);
}

void DispenserService::stopFeedMotor() {
    motor1_.setSpeed(0);
    motor1_.disableOutputs();
    feedBurstLeft_    = 0;
    feedPauseUntilMs_ = 0;
}

void DispenserService::updateFeedMotor() {
    if (feedPauseUntilMs_ != 0) {
        if ((int32_t)(millis() - feedPauseUntilMs_) >= 0) {
            beginFeedBurst();
        }
        return;
    }

    if (feedBurstLeft_ <= 0) {
        beginFeedBurst();
        return;
    }

    if (motor1_.runSpeed()) {
        feedBurstLeft_--;
        if (feedBurstLeft_ == 0) {
            motor1_.setSpeed(0);
            motor1_.disableOutputs();
            feedPauseUntilMs_ = millis() + feedPauseMs_;
        }
    }
}

void DispenserService::startDwell() {
    dwellStartMs_      = millis();
    motionStartMs_     = dwellStartMs_;
    pelletSeenSinceMs_ = 0;
}

void DispenserService::startRaise(long steps) {
    motor2_.enableOutputs();
    phaseStartPos_ = motor2_.currentPosition();
    motor2Target_  = phaseStartPos_ + steps;
    motionStartMs_ = millis();
    raiseStartMs_ = millis();
    pelletClearSinceMs_ = 0;
    grabPhase_ = false;
    motor2_.setSpeed(motorSpeed_); // UP
}

void DispenserService::updatePhotogates() {
    uint32_t now = millis();

    bool raw1 = (digitalRead(PIN_PG1) == LOW);
    if (raw1 != pg1Raw_) { pg1Raw_ = raw1; pg1LastChangeMs_ = now; }
    if ((now - pg1LastChangeMs_) >= kSensorDebounceMs) {
        pg1State_ = pg1Raw_;
    }

    bool raw2 = (digitalRead(PIN_PG2) == LOW);
    if (raw2 != pg2Raw_) { pg2Raw_ = raw2; pg2LastChangeMs_ = now; }
    if ((now - pg2LastChangeMs_) >= kSensorDebounceMs) pg2State_ = pg2Raw_;

    bool raw3 = (digitalRead(PIN_PG3) == HIGH);
    if (raw3 != pg3Raw_) { pg3Raw_ = raw3; pg3LastChangeMs_ = now; }
    if ((now - pg3LastChangeMs_) >= kSensorDebounceMs) {
        bool prev = pg3State_;
        pg3State_ = pg3Raw_;
        if (pg3State_ && !prev) {
            pg3OpenSinceMs_ = now;
            domeWarnLatched_ = false;
        } else if (!pg3State_) {
            pg3OpenSinceMs_ = 0;
            domeWarnLatched_ = false;
        }
    }
}

void DispenserService::checkDomeOpenWarning() {
    if (!pg3State_ || pg3OpenSinceMs_ == 0 || domeWarnLatched_) return;
    if ((millis() - pg3OpenSinceMs_) < kDomeOpenWarnMs) return;
    if (pendingEvent_ != DispenseEvent::None) return;
    setEvent(DispenseEvent::DomeOpenWarning);
    domeWarnLatched_ = true;
}

void DispenserService::setState(DispenseState next) { state_ = next; }

void DispenserService::setEvent(DispenseEvent ev) { pendingEvent_ = ev; }

void DispenserService::haltMotors() {
    motor1_.setSpeed(0);
    motor2_.setSpeed(0);
    motor1_.disableOutputs();
    motor2_.disableOutputs();
    feedBurstLeft_    = 0;
    feedPauseUntilMs_ = 0;
}

void DispenserService::faultNow(ServiceStatus code) {
    haltMotors();
    grabPhase_ = false;
    noFeed_ = false;
    pelletSeenSinceMs_ = 0;
    lastFault_ = code;
    setEvent(DispenseEvent::Fault);
    setState(DispenseState::Fault);
}

bool DispenserService::phaseTimedOut(uint32_t timeoutMs) const {
    return (millis() - motionStartMs_) >= timeoutMs;
}

} // namespace vfm
