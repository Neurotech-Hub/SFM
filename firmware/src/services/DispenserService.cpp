#include "DispenserService.h"

namespace sfm {

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
      eventQ_{},
      eventHead_(0),
      eventTail_(0),
      eventCount_(0),
      pelletCount_(0),
      takenCount_(0),
      lastFault_(ServiceStatus::Ok),
      motionStartMs_(0),
      motor2Target_(0),
      pg3WasOpen_(false),
      grabPhase_(false),
      noFeed_(false),
      skipFeed_(false),
      peerRaiseSeen_(false),
      phaseStartPos_(0),
      belowLoad_(false),
      approachRetried_(false),
      seekUntilClear_(false),
      seekBreakThenClear_(false),
      seekSawBreak_(false),
      dropPos_(0),
      dropPosKnown_(false),
      edgePos_(0),
      edgeLatched_(false),
      pg2RawRose_(false),
      pg2RawFell_(false),
      raiseSawBeam_(false),
      raiseLeftBeam_(false),
      raiseDomeOpen_(false),
      retractRelatch_(false),
      earlyTaken_(false),
      pelletLostDuringRaise_(false),
      reloadCount_(0),
      lastReloadReason_(0),
      lastReloadAttempt_(0),
      raiseStartMs_(0),
      pg3OpenSinceMs_(0),
      pg3ClosedSinceMs_(0),
      pelletClearSinceMs_(0),
      pelletSeenSinceMs_(0),
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
    pg3ClosedSinceMs_ = pg3State_ ? 0 : now;
    pelletClearSinceMs_ = 0;
    pelletSeenSinceMs_ = 0;
    domeWarnLatched_ = false;
    grabPhase_ = false;
    approachRetried_ = false;
    phaseStartPos_ = motor2_.currentPosition();
    // Height is unknown at boot. An asserted load sensor proves the load
    // position; a clear beam does not (the drop position also reads clear).
    belowLoad_ = pg2State_;
    dropPosKnown_ = false;
    edgeLatched_ = false;
    lastFault_ = ServiceStatus::Ok;

    return ServiceStatus::Ok;
}

void DispenserService::update() {
    const bool prevPg2Raw = pg2Raw_;
    updatePhotogates();
    pg2RawRose_ = pg2Raw_ && !prevPg2Raw;
    pg2RawFell_ = !pg2Raw_ && prevPg2Raw;

    if (state_ != DispenseState::Fault) {
        checkDomeOpenWarning();
    }

    switch (state_) {
        case DispenseState::Idle:
        case DispenseState::Fault:
            break;

        case DispenseState::Seeking:
            // On the load sensor: up until the beam clears or seekAwaySteps_.
            // Below the sensor (beam already clear): up until the beam breaks
            // and then clears, still capped at seekAwaySteps_. Hitting the cap
            // without finishing that sequence faults — a blind extra raise is
            // what drove the plate into the stop.
            if (phaseTimedOut(lowerTimeoutMs_)) {
                faultNow(ServiceStatus::ActuatorTimeout);
                break;
            }
            {
                const long traveled =
                    motor2_.currentPosition() - phaseStartPos_;
                const bool hitCap = traveled >= seekAwaySteps_;
                if (seekBreakThenClear_) {
                    if (pg2State_) seekSawBreak_ = true;
                    const bool sequenceDone = seekSawBreak_ && !pg2State_;
                    if (sequenceDone) {
                        startApproachPg2();
                        setState(DispenseState::Lowering);
                    } else if (hitCap) {
                        faultNow(ServiceStatus::ActuatorTimeout);
                    } else {
                        motor2_.runSpeed();
                    }
                } else {
                    const bool cleared = seekUntilClear_ && !pg2State_;
                    if (hitCap || cleared) {
                        startApproachPg2();
                        setState(DispenseState::Lowering);
                    } else {
                        motor2_.runSpeed();
                    }
                }
            }
            break;

        case DispenseState::Lowering:
            if (!grabPhase_) {
                // Datum is the raw break, not the debounced one. A raw reopen
                // before the debounce confirms is a flicker: discard it.
                if (pg2RawRose_) {
                    edgePos_ = motor2_.currentPosition();
                    edgeLatched_ = true;
                }
                if (pg2RawFell_ && !pg2State_) {
                    edgeLatched_ = false;
                }
                // Approach: down until the load sensor asserts. Whichever budget
                // runs out first means the same thing — the load sensor was never
                // reached — so both route through the retry rather than straight
                // to Fault.
                if (pg2State_) {
                    if (!edgeLatched_) {
                        edgePos_ = motor2_.currentPosition();
                    }
                    dropPos_ = edgePos_ - grabSteps_;
                    dropPosKnown_ = true;
                    edgeLatched_ = false;
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
                        startSeekBreakThenClear();
                        setState(DispenseState::Seeking);
                    }
                    break;
                }
            } else if (phaseTimedOut(lowerTimeoutMs_)) {
                faultNow(ServiceStatus::ActuatorTimeout);
                break;
            }
            if (grabPhase_) {
                // Grab descent: fixed travel to the drop position latched at
                // the raw break. The load sensor is ignored here.
                if (motor2_.currentPosition() <= dropPos_) {
                    haltMotors();
                    if (noFeed_) {
                        // No-feed cycle: M1 never runs. Hold here until a peer
                        // node starts raising, then raise identically — so the
                        // motion signature matches a fed dispense and both
                        // plates reach the top at the same moment.
                        startDwell();
                        setState(DispenseState::Dwelling);
                    } else if (skipFeed_) {
                        // Occupied plate, now at a known drop position.
                        skipFeed_ = false;
                        requestRaise();
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
            // Motors already halted by the grab-descent hand-off. The raise is
            // triggered by a peer node's Raising event (notifyPeerRaise), never
            // by a local timer: a fed arm's hold is however long M1 takes plus
            // kPelletLoadConfirmMs, which no fixed dwell can match, and a plate
            // that rises at a different moment is exactly the cue a no-feed
            // cycle exists to remove.
            //
            // No timeout here. Nothing is loaded, so there is nothing to time
            // out, and a fed node that never raises means the trial is already
            // lost — hold until Recover rather than present an empty plate out
            // of sync. The base station clears a node stuck here (see
            // kit.synchronized_cycle).
            if (peerRaiseSeen_) {
                requestRaise(); // same travel as a fed cycle, after the dome check
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
                    requestRaise();
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

        case DispenseState::DomeHold:
            // Motors are already off. No timeout: a dome left open keeps
            // reporting DomeOpenWarning and the plate stays down.
            if (domeSettled()) {
                beginRaiseOrReload();
            }
            break;

        case DispenseState::Retracting:
            if (phaseTimedOut(lowerTimeoutMs_) ||
                labs(motor2_.currentPosition() - phaseStartPos_) >= lowerSteps_) {
                faultNow(ServiceStatus::ActuatorTimeout);
                break;
            }
            if (retractRelatch_) {
                // Plate is above the load sensor. Re-latch the raw break on
                // the way down so the next raise starts from a fresh datum.
                if (pg2RawRose_) {
                    edgePos_ = motor2_.currentPosition();
                    edgeLatched_ = true;
                }
                if (pg2RawFell_ && !pg2State_) {
                    edgeLatched_ = false;
                }
                if (!pg2State_) {
                    motor2_.runSpeed();
                    break;
                }
                if (!edgeLatched_) {
                    edgePos_ = motor2_.currentPosition();
                }
                dropPos_ = edgePos_ - grabSteps_;
                dropPosKnown_ = true;
                edgeLatched_ = false;
                retractRelatch_ = false;
                belowLoad_ = true;
            }
            if (!dropPosKnown_) {
                faultNow(ServiceStatus::ActuatorTimeout);
                break;
            }
            // Pellet sensor and dome are ignored until we are back at the drop.
            if (motor2_.currentPosition() <= dropPos_) {
                haltMotors();
                belowLoad_ = true;
                requestRaise();
            } else {
                motor2_.runSpeed();
            }
            break;

        case DispenseState::Raising:
            if (pg2State_) {
                raiseSawBeam_ = true;
            } else if (raiseSawBeam_) {
                // Beam asserted and then cleared: the plate is above the
                // sensor. A clear beam at the drop position does not count —
                // that is still below the sensor.
                raiseLeftBeam_ = true;
                belowLoad_ = false;
            }
            // Motion guard: still valid on a no-feed raise. The plate must
            // clear the load sensor whether or not it carries a pellet.
            if (pg2State_ &&
                (millis() - raiseStartMs_) >= kLoadClearOnRaiseMs) {
                faultNow(ServiceStatus::Jam);
                break;
            }
            {
                const long progress = motor2_.currentPosition() - dropPos_;
                const bool committed = progress >= raiseCommitSteps();
                const bool domeEdge = pg3State_ && !raiseDomeOpen_;
                raiseDomeOpen_ = pg3State_;

                // Dome opened before the commit point: bring the pellet back
                // down. Fed and no-feed cycles both do this.
                if (!earlyTaken_ && domeEdge && !committed) {
                    const bool pelletGone =
                        !noFeed_ && !pg1State_ && pelletClearSinceMs_ != 0 &&
                        (millis() - pelletClearSinceMs_) >= kPelletLostMs;
                    startRetract(pelletGone);
                    break;
                }

                // Pellet guard: only meaningful when a pellet was loaded. A
                // no-feed raise ALWAYS has PG1 clear, so running this would
                // fault every cycle. Once an early take is latched the plate
                // just finishes the travel.
                if (!noFeed_ && !earlyTaken_) {
                    if (!pg1State_) {
                        if (pelletClearSinceMs_ == 0) {
                            pelletClearSinceMs_ = millis();
                        } else if ((millis() - pelletClearSinceMs_) >= kPelletLostMs) {
                            if (!committed || !pg3State_) {
                                startRetract(true);
                                break;
                            }
                            // At or past the commit point, dome open: the mouse
                            // took the pellet. Count it when the plate arrives
                            // so the node is Idle before the base dispenses again.
                            earlyTaken_ = true;
                            lastTakenWithDomeOpen_ = true;
                            pelletClearSinceMs_ = 0;
                        }
                    } else {
                        pelletClearSinceMs_ = 0;
                    }
                }
            }
            if (phaseTimedOut(raiseTimeoutMs_)) {
                faultNow(ServiceStatus::ActuatorTimeout);
                break;
            }
            if (motor2_.currentPosition() >= motor2Target_) {
                haltMotors();
                belowLoad_ = false;
                if (earlyTaken_) {
                    pelletCount_++;
                    setEvent(DispenseEvent::Loaded);
                    takenCount_++;
                    setEvent(DispenseEvent::PelletTaken);
                    pelletClearSinceMs_ = 0;
                    setState(DispenseState::Idle);
                } else if (noFeed_) {
                    // No pellet was delivered: do NOT emit Loaded and do NOT
                    // touch pelletCount_, so the base station's `pellets`
                    // counter (and end_after(pellets=...)) does not move for
                    // an unrewarded cycle.
                    setEvent(DispenseEvent::NoFeedPresented);
                    setState(DispenseState::Loaded);
                } else {
                    setEvent(DispenseEvent::Loaded);
                    pelletCount_++;
                    pg3WasOpen_ = pg3State_;
                    pelletClearSinceMs_ = 0;
                    setState(DispenseState::Loaded);
                }
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
                    if (eventQueueHasRoom()) {
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
    skipFeed_ = false;
    peerRaiseSeen_ = false;
    reloadCount_ = 0;
    pelletLostDuringRaise_ = false;
    earlyTaken_ = false;
    edgeLatched_ = false;

    // Occupancy first — pellet sensor is on the plate at all times.
    if (pg1State_) {
        beginOccupiedDispense();
        return true;
    }

    beginLoweringPhase();
    return true;
}

bool DispenserService::dispenseNoFeed() {
    if (state_ != DispenseState::Idle && state_ != DispenseState::Loaded) {
        return false;
    }

    haltMotors();
    pelletClearSinceMs_ = 0;
    skipFeed_ = false;
    // Cleared before noFeed_ is set, so a peer raise latched during a previous
    // cycle can never short-circuit this one's hold.
    peerRaiseSeen_ = false;
    reloadCount_ = 0;
    pelletLostDuringRaise_ = false;
    earlyTaken_ = false;
    edgeLatched_ = false;

    // Occupancy first, same as dispense(). A real pellet is already on the
    // plate: present it honestly as a normal FeedSkipped cycle rather than
    // silently discarding it — noFeed_ MUST be cleared here, otherwise the
    // Raising/Loaded no-feed branches would run with a real pellet
    // aboard and disable both the pellet guard and PelletTaken for it.
    if (pg1State_) {
        noFeed_ = false;
        beginOccupiedDispense();
        return true;
    }

    noFeed_ = true;
    beginLoweringPhase();
    return true;
}

void DispenserService::notifyPeerRaise() {
    // Latch regardless of the current phase. A fed node whose plate is already
    // occupied takes beginOccupiedDispense and raises almost immediately —
    // possibly before this node has finished lowering — so the flag must
    // survive until Dwelling reads it. Only a no-feed cycle cares; a fed node
    // ignores its neighbours entirely.
    if (noFeed_) peerRaiseSeen_ = true;
}

void DispenserService::beginOccupiedDispense() {
    setEvent(DispenseEvent::FeedSkipped);

    // Already at presentation height: nothing to home. PG2 clear and not
    // below the sensor is the only reading that means "up".
    if (!pg2State_ && !belowLoad_) {
        pg3WasOpen_ = pg3State_;
        setState(DispenseState::Loaded);
        return;
    }

    // Anywhere else, home to the drop position (M1 stays off) and raise
    // raiseSteps_ from that known datum. No shortened raise from "somewhere
    // in the beam".
    skipFeed_ = true;
    beginLoweringPhase();
}

void DispenserService::recover() {
    haltMotors();
    lastFault_ = ServiceStatus::Ok;
    pelletClearSinceMs_ = 0;
    pelletSeenSinceMs_ = 0;
    grabPhase_ = false;
    noFeed_ = false;
    skipFeed_ = false;
    peerRaiseSeen_ = false;
    earlyTaken_ = false;
    pelletLostDuringRaise_ = false;
    reloadCount_ = 0;
    edgeLatched_ = false;
    dropPosKnown_ = false;
    seekBreakThenClear_ = false;
    retractRelatch_ = false;
    // Asserted load sensor ⇒ at load. Clear does not prove elevated: the drop
    // position also reads clear, so preserve belowLoad_ when the beam is open.
    // belowLoad_ is cleared only once a raise has passed up through the beam.
    if (pg2State_) {
        belowLoad_ = true;
    }
    setState(DispenseState::Idle);
}

DispenseEvent DispenserService::takeEvent() {
    if (eventCount_ == 0) return DispenseEvent::None;
    DispenseEvent ev = eventQ_[eventHead_];
    eventHead_ = static_cast<uint8_t>((eventHead_ + 1) % kEventQueueCap);
    eventCount_--;
    return ev;
}

// ---------------------------------------------------------------------------
void DispenserService::beginLoweringPhase() {
    motionStartMs_ = millis();
    grabPhase_ = false;
    approachRetried_ = false;
    edgeLatched_ = false;

    // Seek-up only when safe:
    //   - load sensor asserted → at load position; raise until clear or cap
    //   - belowLoad_ known → at drop depth (sensor already clear); up through
    //     the beam (break, then clear), then approach back down
    // A clear sensor with unknown height means approach down. Never invent a
    // seek from "maybe below" — that is what smashed the stop after PelletLost.
    if (pg2State_) {
        startSeekAwayFromPg2(true);
        setState(DispenseState::Seeking);
    } else if (belowLoad_) {
        startSeekBreakThenClear();
        setState(DispenseState::Seeking);
    } else {
        startApproachPg2();
        setState(DispenseState::Lowering);
    }
}

void DispenserService::startSeekAwayFromPg2(bool untilClear) {
    seekUntilClear_ = untilClear;
    seekBreakThenClear_ = false;
    seekSawBreak_ = false;
    motor2_.enableOutputs();
    phaseStartPos_ = motor2_.currentPosition();
    motionStartMs_ = millis();
    motor2_.setSpeed(motorSpeed_); // UP
}

void DispenserService::startSeekBreakThenClear() {
    seekUntilClear_ = false;
    seekBreakThenClear_ = true;
    seekSawBreak_ = false;
    motor2_.enableOutputs();
    phaseStartPos_ = motor2_.currentPosition();
    motionStartMs_ = millis();
    motor2_.setSpeed(motorSpeed_); // UP
}

void DispenserService::startApproachPg2() {
    motor2_.enableOutputs();
    phaseStartPos_ = motor2_.currentPosition();
    motionStartMs_ = millis();
    edgeLatched_ = false;
    grabPhase_ = false;
    motor2_.setSpeed(-motorSpeed_); // DOWN
}

// Continuation of the approach: load sensor asserted; keep going DOWN to
// dropPos_ (raw break edge minus grabSteps_). Nothing is done to the motor
// here — it is already energised and already running at -motorSpeed_, and
// this is the same physical move.
void DispenserService::startGrabDescent() {
    if (grabPhase_) return;
    grabPhase_ = true;
    belowLoad_ = true;
    phaseStartPos_ = motor2_.currentPosition();
    motionStartMs_ = millis();
}

void DispenserService::requestRaise() {
    if (!dropPosKnown_) {
        faultNow(ServiceStatus::ActuatorTimeout);
        return;
    }
    if (!domeSettled()) {
        haltMotors();
        setState(DispenseState::DomeHold);
        return;
    }
    beginRaiseOrReload();
}

void DispenserService::beginRaiseOrReload() {
    // Dome is closed and has stayed closed for kDomeCloseSettleMs.
    if (noFeed_ || pg1State_) {
        startRaise();
        setState(DispenseState::Raising);
        return;
    }
    if (reloadCount_ < kMaxPelletReloads) {
        reloadCount_++;
        lastReloadReason_ = pelletLostDuringRaise_
            ? static_cast<uint8_t>(PelletReloadReason::LostDuringRaise)
            : static_cast<uint8_t>(PelletReloadReason::MissingAfterRetract);
        lastReloadAttempt_ = reloadCount_;
        pelletLostDuringRaise_ = false;
        setEvent(DispenseEvent::PelletReload);
        startFeed();
        setState(DispenseState::Loading);
        return;
    }
    faultNow(ServiceStatus::PelletLost);
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
    motionStartMs_     = millis();
    pelletSeenSinceMs_ = 0;
}

void DispenserService::startRaise() {
    motor2_.enableOutputs();
    phaseStartPos_ = motor2_.currentPosition();
    motor2Target_  = dropPos_ + raiseSteps_;
    motionStartMs_ = millis();
    raiseStartMs_ = millis();
    pelletClearSinceMs_ = 0;
    grabPhase_ = false;
    raiseSawBeam_ = pg2State_;
    raiseLeftBeam_ = false;
    raiseDomeOpen_ = pg3State_;
    earlyTaken_ = false;
    pelletLostDuringRaise_ = false;
    motor2_.setSpeed(motorSpeed_); // UP
}

void DispenserService::startRetract(bool pelletLost) {
    if (pelletLost) pelletLostDuringRaise_ = true;
    retractRelatch_ = raiseLeftBeam_;
    edgeLatched_ = false;
    earlyTaken_ = false;
    grabPhase_ = false;
    motor1_.setSpeed(0);
    motor1_.disableOutputs();
    motor2_.enableOutputs();
    phaseStartPos_ = motor2_.currentPosition();
    motionStartMs_ = millis();
    motor2_.setSpeed(-motorSpeed_); // DOWN
    setState(DispenseState::Retracting);
}

bool DispenserService::domeSettled() const {
    if (pg3State_ || pg3ClosedSinceMs_ == 0) return false;
    return (millis() - pg3ClosedSinceMs_) >= kDomeCloseSettleMs;
}

bool DispenserService::eventQueueHasRoom() const {
    return eventCount_ < kEventQueueCap;
}

long DispenserService::raiseCommitSteps() const {
    return (raiseSteps_ * static_cast<long>(kRaiseCommitPct)) / 100;
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
            pg3ClosedSinceMs_ = 0;
            domeWarnLatched_ = false;
        } else if (!pg3State_ && prev) {
            pg3OpenSinceMs_ = 0;
            pg3ClosedSinceMs_ = now;
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
    if (!eventQueueHasRoom()) return;
    setEvent(DispenseEvent::DomeOpenWarning);
    domeWarnLatched_ = true;
}

void DispenserService::setState(DispenseState next) { state_ = next; }

void DispenserService::setEvent(DispenseEvent ev) {
    if (ev == DispenseEvent::None || eventCount_ >= kEventQueueCap) return;
    eventQ_[eventTail_] = ev;
    eventTail_ = static_cast<uint8_t>((eventTail_ + 1) % kEventQueueCap);
    eventCount_++;
}

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
    skipFeed_ = false;
    earlyTaken_ = false;
    pelletLostDuringRaise_ = false;
    pelletSeenSinceMs_ = 0;
    edgeLatched_ = false;
    dropPosKnown_ = false;
    seekBreakThenClear_ = false;
    retractRelatch_ = false;
    lastFault_ = code;
    // A fault must not be dropped behind a full warning queue.
    eventHead_ = 0;
    eventTail_ = 0;
    eventCount_ = 0;
    setEvent(DispenseEvent::Fault);
    setState(DispenseState::Fault);
}

bool DispenserService::phaseTimedOut(uint32_t timeoutMs) const {
    return (millis() - motionStartMs_) >= timeoutMs;
}

} // namespace sfm
