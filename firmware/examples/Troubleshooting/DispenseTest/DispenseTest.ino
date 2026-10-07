// DispenseTest – serial-driven bench test for the SFM DispenserService.
//
// Open the Arduino Serial Monitor at 115200 baud.
//
// Commands:
//   d              – one real dispense (waits for a pellet on the plate)
//   n              – one motor cycle with the pellet checkpoint skipped
//   c              – continuous empty-bin cycles for 500 min, 5 min between
//   c <run> <gap>  – same, in minutes (gap may be 0). Example: c 500 5
//   x              – stop the continuous run and de-energise
//   a              – recover: stop motion / clear Fault (also stops a run)
//   s              – print current dispenser state + photogate readings
//   + / -          – motor speed ±100 steps/s
//   r              – print raiseSteps
//   r <n>          – set raiseSteps (e.g. "r 1480")
//   t              – print raise timeout
//   t <ms>         – set raise timeout (library default 8000)
//
// Empty-bin cycles call DispenserService::dispenseNoFeed() and then
// notifyPeerRaise(). That library path runs the full M2 cycle — seek, lower,
// grab descent, raise by raiseSteps — and never waits on the pellet sensor.
// M1 stays off. DispenserService itself is not modified.
//
// The run window is wall-clock time from the start, gaps included.
// Each cycle logs raise duration against the step target, plus when the load
// beam breaks and clears. A short lift shows up as one of:
//   ActuatorTimeout – M2 was stopped before raiseSteps (timeout cut the move)
//   Jam             – load beam still blocked at kLoadClearOnRaiseMs
//   OK, beam never cleared – step count finished, plate did not pass the sensor
//   OK, beam cleared – count finished; a plate that is still short is losing steps
//
// LED mirrors (debounced sensor state):
//   LED 10 = pellet present
//   LED 9  = dome open
//
// Defaults match the previous bench test: speed=300, grab=280, raise=1480,
// seek-away=800, lower budget=3072, feed timeout=30 s.
// raiseSteps is measured from the pellet-drop position (kGrabSteps below the
// load sensor), not from the load sensor itself.

#include <SFM.h>

static constexpr float    kMotorSpeed    = 300.0f;
static constexpr long     kLowerSteps    = 3072;
static constexpr long     kSeekAwaySteps = 800;
static constexpr long     kGrabSteps     = 280;
static constexpr long     kRaiseSteps    = 1480;
static constexpr uint32_t kFeedTimeoutMs = 30000;
static constexpr uint32_t kDefaultRunMin = 500;
static constexpr uint32_t kDefaultGapMin = 5;
static constexpr uint32_t kMaxMinutes    = 10080; // 7 days
static constexpr uint32_t kMinRaiseTimeoutMs = 1000;
static constexpr uint32_t kMaxRaiseTimeoutMs = 120000;

enum class RunMode : uint8_t { Off, Single, Cycling, Gap };

sfm::DispenserService dispenser;
sfm::LedService       leds;
float    currentSpeed       = kMotorSpeed;
long     currentRaiseSteps  = kRaiseSteps;
uint32_t raiseTimeoutMs     = sfm::kDefaultRaiseTimeoutMs;

static char    lineBuf[48];
static uint8_t lineIdx = 0;

static RunMode  runMode       = RunMode::Off;
static bool     traceCycle    = false;
static bool     raiseArmed    = false;
static bool     beamEntered   = false;
static bool     beamCleared   = false;
static bool     dwellNudged   = false;
static uint32_t runDurationMs = kDefaultRunMin * 60000UL;
static uint32_t gapMs         = kDefaultGapMin * 60000UL;
static uint32_t runStartMs    = 0;
static uint32_t gapUntilMs    = 0;
static uint32_t cycleStartMs  = 0;
static uint32_t raiseEnteredMs = 0;
static uint32_t beamEnteredMs = 0;
static uint32_t beamClearedMs = 0;
static uint32_t sessionCycles = 0;
static uint32_t sessionOk     = 0;
static uint32_t sessionFault  = 0;
static sfm::DispenseState seenState     = sfm::DispenseState::Idle;
static sfm::DispenseState faultFromPhase = sfm::DispenseState::Idle;

static const char *stateStr(sfm::DispenseState s) {
    switch (s) {
        case sfm::DispenseState::Idle:     return "Idle";
        case sfm::DispenseState::Seeking:  return "Seeking";
        case sfm::DispenseState::Lowering: return "Lowering";
        case sfm::DispenseState::Loading:  return "Loading";
        case sfm::DispenseState::Dwelling: return "Dwelling";
        case sfm::DispenseState::Raising:  return "Raising";
        case sfm::DispenseState::Loaded:   return "Loaded";
        case sfm::DispenseState::Fault:    return "Fault";
    }
    return "?";
}

static uint32_t expectedRaiseMs() {
    if (currentSpeed < 1.0f) return 0;
    return (uint32_t)((1000.0f * (float)currentRaiseSteps) / currentSpeed);
}

static uint32_t expectedBeamMs() {
    if (currentSpeed < 1.0f) return 0;
    return (uint32_t)((1000.0f * (float)kGrabSteps) / currentSpeed);
}

// A raise that cannot finish inside the library limits is stopped early.
// That looks like "the actuator goes up but not all the way."
static void warnRaiseWindow() {
    uint32_t expect = expectedRaiseMs();
    if (expect + 750 >= raiseTimeoutMs) {
        Serial.print(F("WARNING: expected raise "));
        Serial.print(expect);
        Serial.print(F(" ms meets timeout "));
        Serial.print(raiseTimeoutMs);
        Serial.println(F(" ms. M2 will be stopped short. Use t <ms> to extend it."));
    }
    uint32_t beamMs = expectedBeamMs();
    if (beamMs + 750 >= sfm::kLoadClearOnRaiseMs) {
        Serial.print(F("WARNING: load beam needs about "));
        Serial.print(beamMs);
        Serial.print(F(" ms to clear; jam limit is "));
        Serial.print(sfm::kLoadClearOnRaiseMs);
        Serial.println(F(" ms. The raise will stop in the beam."));
    }
}

static void printHelp() {
    Serial.println(F("Commands:"));
    Serial.println(F("  d              one dispense (waits for a real pellet)"));
    Serial.println(F("  n              one motor cycle, pellet checkpoint skipped"));
    Serial.println(F("  c              continuous empty-bin run, 500 min, 5 min gap"));
    Serial.println(F("  c <run> <gap>  minutes, gap may be 0. Example: c 500 5"));
    Serial.println(F("  x              stop the continuous run"));
    Serial.println(F("  a              recover (also stops a run)"));
    Serial.println(F("  s              status"));
    Serial.println(F("  + / -          speed +/- 100"));
    Serial.println(F("  r [n]          show or set raiseSteps"));
    Serial.println(F("  t [ms]         show or set raise timeout"));
}

static void printStatus() {
    Serial.print(F("[State] "));
    Serial.print(stateStr(dispenser.state()));
    Serial.print(F("  pellet="));        Serial.print(dispenser.pelletOnPlate());
    Serial.print(F(" load_position="));  Serial.print(dispenser.atLoadPosition());
    Serial.print(F(" dome_open="));      Serial.print(dispenser.domeOpen());
    Serial.print(F("  Pellets="));       Serial.print(dispenser.pelletCount());
    Serial.print(F("  raiseSteps="));    Serial.print(currentRaiseSteps);
    Serial.print(F("  speed="));         Serial.print(currentSpeed);
    Serial.print(F("  raiseTimeoutMs="));
    Serial.println(raiseTimeoutMs);

    if (runMode == RunMode::Off) return;

    Serial.print(F("[Run] "));
    if (runMode == RunMode::Gap)         Serial.print(F("gap"));
    else if (runMode == RunMode::Single) Serial.print(F("once"));
    else                                 Serial.print(F("cycle"));
    Serial.print(F("  cycles="));     Serial.print(sessionCycles);
    Serial.print(F("  ok="));          Serial.print(sessionOk);
    Serial.print(F("  fault="));       Serial.print(sessionFault);
    Serial.print(F("  elapsed_s="));
    Serial.print((millis() - runStartMs) / 1000UL);
    Serial.print(F("/"));
    Serial.print(runDurationMs / 1000UL);
    if (runMode == RunMode::Gap) {
        int32_t left = (int32_t)(gapUntilMs - millis());
        if (left < 0) left = 0;
        Serial.print(F("  gap_left_s="));
        Serial.print((uint32_t)left / 1000UL);
    }
    Serial.println();
}

static void printFaultDetail() {
    Serial.print(F("[Event] FAULT – "));
    switch (dispenser.faultCode()) {
        case sfm::ServiceStatus::FeedTimeout:
            Serial.println(F("FeedTimeout (out of pellets / refill hopper)"));
            break;
        case sfm::ServiceStatus::ActuatorTimeout:
            Serial.print(F("ActuatorTimeout during "));
            Serial.println(stateStr(faultFromPhase));
            break;
        case sfm::ServiceStatus::Jam:
            Serial.print(F("Jam (load sensor still blocked after "));
            Serial.print(sfm::kLoadClearOnRaiseMs);
            Serial.println(F(" ms of raise)"));
            break;
        case sfm::ServiceStatus::PelletLost:
            Serial.println(F("PelletLost"));
            break;
        default:
            Serial.println(F("?"));
            break;
    }
}

static void printCycleResult(bool ok) {
    if (runMode == RunMode::Cycling) {
        Serial.print(F("[cycle "));
        Serial.print(sessionCycles);
        Serial.print(F("] "));
    } else {
        Serial.print(F("[once] "));
    }
    Serial.print(ok ? F("OK") : F("FAULT"));
    Serial.print(F("  cycle_ms="));
    Serial.print(millis() - cycleStartMs);

    uint32_t raiseMs = 0;
    if (raiseArmed) {
        raiseMs = millis() - raiseEnteredMs;
        Serial.print(F("  raise_ms="));
        Serial.print(raiseMs);
        Serial.print(F("  expected_ms="));
        Serial.print(expectedRaiseMs());
    } else {
        Serial.print(F("  raise_ms=none"));
    }
    Serial.print(F("  beam_enter_ms="));
    if (beamEntered) Serial.print(beamEnteredMs);
    else             Serial.print(F("none"));
    Serial.print(F("  beam_clear_ms="));
    if (beamCleared) Serial.print(beamClearedMs);
    else             Serial.print(F("none"));
    if (!ok) {
        Serial.print(F("  phase="));
        Serial.print(stateStr(faultFromPhase));
    }
    Serial.println();

    if (!ok) {
        sfm::ServiceStatus code = dispenser.faultCode();
        if (code == sfm::ServiceStatus::ActuatorTimeout &&
            faultFromPhase == sfm::DispenseState::Raising) {
            Serial.println(F("  Read: raise aborted at the timeout. Plate stopped short of raiseSteps."));
        } else if (code == sfm::ServiceStatus::Jam) {
            Serial.println(F("  Read: raise aborted with the load beam still blocked. Plate stopped in the beam."));
        } else if (code == sfm::ServiceStatus::ActuatorTimeout) {
            Serial.println(F("  Read: M2 aborted before its step target on the way to the drop position."));
        }
        return;
    }

    if (!raiseArmed) {
        Serial.println(F("  Read: cycle ended before Raising."));
    } else if (!beamEntered) {
        Serial.println(F("  Read: step target reached, load beam never broke. Plate did not travel up through the sensor."));
    } else if (!beamCleared) {
        Serial.println(F("  Read: step target reached, load beam stayed blocked. Plate did not clear the load sensor."));
    } else {
        Serial.print(F("  Read: "));
        Serial.print(currentRaiseSteps);
        Serial.print(F(" raise steps finished in "));
        Serial.print(raiseMs);
        Serial.println(F(" ms and the load beam cleared."));
        Serial.println(F("  A plate that is still short of the top is losing steps while that count advances."));
    }
}

static void printRunSummary(const __FlashStringHelper *why) {
    Serial.println(why);
    Serial.print(F("[run] cycles="));  Serial.print(sessionCycles);
    Serial.print(F(" ok="));           Serial.print(sessionOk);
    Serial.print(F(" fault="));        Serial.print(sessionFault);
    Serial.print(F(" elapsed_s="));
    Serial.println((millis() - runStartMs) / 1000UL);
}

// Full M2 cycle, pellet sensor is not the raise trigger.
// Caller sets runMode to Single or Cycling first.
static bool beginCycle() {
    if (dispenser.pelletOnPlate()) {
        Serial.println(F("Pellet sensor is asserted. Empty the plate for this motor test."));
        return false;
    }
    if (!dispenser.dispenseNoFeed()) {
        Serial.print(F("Cannot start cycle, state="));
        Serial.println(stateStr(dispenser.state()));
        return false;
    }

    // Occupied-plate path presents a real pellet and waits forever here.
    sfm::DispenseState started = dispenser.state();
    if (started != sfm::DispenseState::Seeking &&
        started != sfm::DispenseState::Lowering) {
        Serial.println(F("Plate is occupied. Empty it before this motor test."));
        dispenser.takeEvent();
        dispenser.recover();
        seenState = dispenser.state();
        return false;
    }

    // Latch the raise now so Dwelling does not wait for another node.
    dispenser.notifyPeerRaise();

    cycleStartMs  = millis();
    traceCycle    = true;
    raiseArmed    = false;
    beamEntered   = false;
    beamCleared   = false;
    dwellNudged   = false;

    if (runMode == RunMode::Cycling) {
        sessionCycles++;
        Serial.print(F("[cycle "));
        Serial.print(sessionCycles);
        Serial.print(F("] start "));
        Serial.print(stateStr(started));
        Serial.print(F("  elapsed_s="));
        Serial.print((millis() - runStartMs) / 1000UL);
        Serial.print(F("/"));
        Serial.println(runDurationMs / 1000UL);
    } else {
        Serial.print(F("[once] start "));
        Serial.println(stateStr(started));
    }
    seenState = started;
    return true;
}

static void finishCycle(bool ok) {
    bool single = (runMode == RunMode::Single);
    bool cont   = (runMode == RunMode::Cycling);
    if (!single && !cont) return;

    printCycleResult(ok);
    traceCycle = false;

    if (single) {
        runMode = RunMode::Off;
        return;
    }

    if (ok) sessionOk++;
    else    sessionFault++;

    if (!ok) {
        dispenser.recover();
        seenState = dispenser.state();
        Serial.println(F("[run] recovered at the stopped height"));
    }

    uint32_t elapsed = millis() - runStartMs;
    if (elapsed + gapMs >= runDurationMs) {
        runMode = RunMode::Off;
        printRunSummary(F("[run] complete - plate left at the last height"));
        return;
    }
    if (gapMs == 0) {
        if (!beginCycle()) {
            runMode = RunMode::Off;
            printRunSummary(F("[run] stopped"));
        }
        return;
    }

    runMode    = RunMode::Gap;
    gapUntilMs = millis() + gapMs;
    Serial.print(F("[gap] "));
    Serial.print(gapMs / 60000UL);
    Serial.print(F(" min  window_left_s="));
    Serial.println((runDurationMs - elapsed) / 1000UL);
}

static void startSingle() {
    if (runMode != RunMode::Off) {
        Serial.println(F("A run is active. x stops it."));
        return;
    }
    Serial.println(F("One empty-bin motor cycle (pellet checkpoint skipped, M1 off)."));
    warnRaiseWindow();
    runMode    = RunMode::Single;
    runStartMs = millis();
    if (!beginCycle()) runMode = RunMode::Off;
}

static void startContinuous(uint32_t runMin, uint32_t gapMin) {
    if (runMode != RunMode::Off) {
        dispenser.recover();
        seenState  = dispenser.state();
        traceCycle = false;
        runMode    = RunMode::Off;
        Serial.println(F("Restarting continuous run."));
    }

    dispenser.recover();
    seenState = dispenser.state();

    runDurationMs = runMin * 60000UL;
    gapMs         = gapMin * 60000UL;
    runStartMs    = millis();
    sessionCycles = 0;
    sessionOk     = 0;
    sessionFault  = 0;

    Serial.println(F("Continuous empty-bin motor test"));
    Serial.println(F("  M2 cycle: seek, lower, grab descent, full raise. M1 stays off."));
    Serial.println(F("  Pellet checkpoint skipped via dispenseNoFeed + notifyPeerRaise."));
    Serial.print(F("  duration_min="));     Serial.println(runMin);
    Serial.print(F("  gap_min="));          Serial.println(gapMin);
    Serial.print(F("  speed="));            Serial.println(currentSpeed);
    Serial.print(F("  raiseSteps="));       Serial.println(currentRaiseSteps);
    Serial.print(F("  raiseTimeoutMs="));   Serial.println(raiseTimeoutMs);
    Serial.print(F("  loadClearLimitMs=")); Serial.println(sfm::kLoadClearOnRaiseMs);
    Serial.println(F("  Window is wall-clock from now, gaps included."));
    warnRaiseWindow();

    runMode = RunMode::Cycling;
    if (!beginCycle()) runMode = RunMode::Off;
}

static void stopRun() {
    if (runMode == RunMode::Off) {
        Serial.println(F("No run is active."));
        return;
    }
    bool cont = (runMode == RunMode::Cycling || runMode == RunMode::Gap);
    dispenser.recover();
    seenState  = dispenser.state();
    traceCycle = false;
    runMode    = RunMode::Off;
    if (cont) printRunSummary(F("[run] stopped - plate left at this height"));
    else      Serial.println(F("Stopped."));
}

static void tracePhase() {
    sfm::DispenseState now = dispenser.state();
    if (now == seenState) return;

    if (now == sfm::DispenseState::Fault) faultFromPhase = seenState;

    if (traceCycle) {
        Serial.print(F("[phase +"));
        Serial.print(millis() - cycleStartMs);
        Serial.print(F(" ms] "));
        Serial.print(stateStr(seenState));
        Serial.print(F(" -> "));
        Serial.print(stateStr(now));
        Serial.print(F("  pellet="));
        Serial.print(dispenser.pelletOnPlate());
        Serial.print(F(" load="));
        Serial.print(dispenser.atLoadPosition());
        Serial.print(F(" dome="));
        Serial.println(dispenser.domeOpen());
    }

    if (traceCycle && now == sfm::DispenseState::Raising) {
        raiseArmed    = true;
        raiseEnteredMs = millis();
        beamEntered   = false;
        beamCleared   = false;
        Serial.print(F("[raise] "));
        Serial.print(currentRaiseSteps);
        Serial.print(F(" steps at "));
        Serial.print(currentSpeed, 0);
        Serial.print(F(" steps/s, expect "));
        Serial.print(expectedRaiseMs());
        Serial.print(F(" ms, timeout "));
        Serial.print(raiseTimeoutMs);
        Serial.print(F(" ms, beam back in about "));
        Serial.print(expectedBeamMs());
        Serial.print(F(" ms, jam limit "));
        Serial.print(sfm::kLoadClearOnRaiseMs);
        Serial.println(F(" ms"));
    }

    seenState = now;
}

// At the drop position the load beam is already clear. A full raise breaks it
// (flag enters) and then clears it again (plate above the sensor).
static void traceBeam() {
    if (!traceCycle || !raiseArmed) return;
    if (dispenser.state() != sfm::DispenseState::Raising) return;

    bool load = dispenser.atLoadPosition();
    uint32_t dt = millis() - raiseEnteredMs;
    if (load && !beamEntered) {
        beamEntered   = true;
        beamEnteredMs = dt;
        Serial.print(F("[raise] load beam entered at "));
        Serial.print(dt);
        Serial.println(F(" ms"));
    } else if (beamEntered && !load && !beamCleared) {
        beamCleared   = true;
        beamClearedMs = dt;
        Serial.print(F("[raise] load beam cleared at "));
        Serial.print(dt);
        Serial.println(F(" ms"));
    }
}

// dispenseNoFeed holds at the drop until a peer raise. We latch that before
// motion starts; if it is ever missed, fire it once so the test cannot sit
// in Dwelling for the rest of the run.
static void nudgeDwell() {
    if (!traceCycle || dwellNudged) return;
    if (dispenser.state() != sfm::DispenseState::Dwelling) return;
    if ((millis() - cycleStartMs) < 2000) return;
    dwellNudged = true;
    Serial.println(F("[raise] still at the drop - sending the raise trigger again"));
    dispenser.notifyPeerRaise();
}

static bool parseU32(const char *&p, uint32_t &out) {
    while (*p == ' ' || *p == '\t') ++p;
    if (*p < '0' || *p > '9') return false;
    uint32_t v = 0;
    while (*p >= '0' && *p <= '9') {
        uint32_t digit = (uint32_t)(*p - '0');
        if (v > (4294967295UL - digit) / 10UL) return false;
        v = v * 10UL + digit;
        ++p;
    }
    out = v;
    return true;
}

static void handleRaiseCommand(const char *line) {
    if (line[1] == '\0') {
        Serial.print(F("raiseSteps = "));
        Serial.println(currentRaiseSteps);
        return;
    }
    const char *p = line + 1;
    while (*p == ' ' || *p == '\t') ++p;
    if (*p == '\0') {
        Serial.print(F("raiseSteps = "));
        Serial.println(currentRaiseSteps);
        return;
    }
    long steps = atol(p);
    if (steps < 1) {
        Serial.println(F("raiseSteps must be >= 1"));
        return;
    }
    currentRaiseSteps = steps;
    dispenser.setRaiseSteps(currentRaiseSteps);
    Serial.print(F("raiseSteps set to "));
    Serial.println(currentRaiseSteps);
    warnRaiseWindow();
}

static void handleTimeoutCommand(const char *line) {
    const char *p = line + 1;
    while (*p == ' ' || *p == '\t') ++p;
    if (*p == '\0') {
        Serial.print(F("raiseTimeoutMs = "));
        Serial.println(raiseTimeoutMs);
        return;
    }
    uint32_t ms = 0;
    const char *rest = p;
    if (!parseU32(rest, ms) || ms < kMinRaiseTimeoutMs || ms > kMaxRaiseTimeoutMs) {
        Serial.print(F("raise timeout must be "));
        Serial.print(kMinRaiseTimeoutMs);
        Serial.print(F(".."));
        Serial.println(kMaxRaiseTimeoutMs);
        return;
    }
    while (*rest == ' ' || *rest == '\t') ++rest;
    if (*rest != '\0') {
        Serial.println(F("Usage: t <ms>"));
        return;
    }
    raiseTimeoutMs = ms;
    dispenser.setRaiseTimeoutMs(raiseTimeoutMs);
    Serial.print(F("raiseTimeoutMs set to "));
    Serial.println(raiseTimeoutMs);
    warnRaiseWindow();
}

static void handleContinuousCommand(const char *line) {
    const char *p = line + 1;
    while (*p == ' ' || *p == '\t') ++p;
    if (*p == '\0') {
        startContinuous(kDefaultRunMin, kDefaultGapMin);
        return;
    }
    uint32_t runMin = 0;
    uint32_t gapMin = 0;
    if (!parseU32(p, runMin) || !parseU32(p, gapMin)) {
        Serial.println(F("Usage: c <runMin> <gapMin>   example: c 500 5"));
        return;
    }
    while (*p == ' ' || *p == '\t') ++p;
    if (*p != '\0') {
        Serial.println(F("Usage: c <runMin> <gapMin>   example: c 500 5"));
        return;
    }
    if (runMin < 1 || runMin > kMaxMinutes || gapMin > kMaxMinutes) {
        Serial.print(F("Minutes must be run 1.."));
        Serial.print(kMaxMinutes);
        Serial.print(F(" and gap 0.."));
        Serial.println(kMaxMinutes);
        return;
    }
    startContinuous(runMin, gapMin);
}

void handleLine(const char *line) {
    if (line[0] == '\0') return;

    if (line[0] == 'r' || line[0] == 'R') {
        handleRaiseCommand(line);
        return;
    }
    if (line[0] == 't' || line[0] == 'T') {
        handleTimeoutCommand(line);
        return;
    }
    if (line[0] == 'c' || line[0] == 'C') {
        handleContinuousCommand(line);
        return;
    }

    switch (line[0]) {
        case 'd':
        case 'D':
            if (runMode != RunMode::Off) {
                Serial.println(F("A run is active. x stops it."));
                break;
            }
            if (dispenser.dispense()) {
                Serial.println(F("Dispense started."));
            } else {
                Serial.print(F("Cannot dispense – state: "));
                Serial.println(stateStr(dispenser.state()));
            }
            break;
        case 'n':
        case 'N':
            startSingle();
            break;
        case 'x':
        case 'X':
            stopRun();
            break;
        case 'a':
        case 'A':
            if (runMode != RunMode::Off) {
                stopRun();
            } else {
                dispenser.recover();
                seenState = dispenser.state();
                Serial.println(F("Recovered."));
            }
            break;
        case 's':
        case 'S':
            printStatus();
            break;
        case '+':
            currentSpeed += 100.0f;
            dispenser.setMotorSpeed(currentSpeed);
            Serial.print(F("Speed = "));
            Serial.println(currentSpeed);
            warnRaiseWindow();
            break;
        case '-':
            currentSpeed = max(100.0f, currentSpeed - 100.0f);
            dispenser.setMotorSpeed(currentSpeed);
            Serial.print(F("Speed = "));
            Serial.println(currentSpeed);
            warnRaiseWindow();
            break;
        case 'h':
        case 'H':
            printHelp();
            break;
        default:
            Serial.print(F("Unknown: "));
            Serial.println(line);
            break;
    }
}

static void serviceGap() {
    if (runMode != RunMode::Gap) return;
    if ((int32_t)(millis() - gapUntilMs) < 0) return;

    if ((millis() - runStartMs) >= runDurationMs) {
        runMode = RunMode::Off;
        printRunSummary(F("[run] complete - plate left at the last height"));
        return;
    }
    runMode = RunMode::Cycling;
    if (!beginCycle()) {
        runMode = RunMode::Off;
        printRunSummary(F("[run] stopped"));
    }
}

static void handleDispenseEvent(sfm::DispenseEvent ev) {
    switch (ev) {
        case sfm::DispenseEvent::OnPlate:
            Serial.println(F("[Event] OnPlate"));
            break;
        case sfm::DispenseEvent::Loaded:
            Serial.print(F("[Event] Loaded  total="));
            Serial.println(dispenser.pelletCount());
            break;
        case sfm::DispenseEvent::DomeOpened:
            Serial.println(F("[Event] DomeOpened"));
            break;
        case sfm::DispenseEvent::PelletTaken:
            Serial.print(F("[Event] PelletTaken  taken="));
            Serial.println(dispenser.takenCount());
            break;
        case sfm::DispenseEvent::FeedSkipped:
            Serial.println(F("[Event] FeedSkipped (plate occupied)"));
            if (runMode == RunMode::Single || runMode == RunMode::Cycling) {
                Serial.println(F("Occupied plate - motor test stopped."));
                bool cont = (runMode == RunMode::Cycling);
                dispenser.recover();
                seenState  = dispenser.state();
                traceCycle = false;
                runMode    = RunMode::Off;
                if (cont) printRunSummary(F("[run] stopped"));
            }
            break;
        case sfm::DispenseEvent::DomeOpenWarning:
            Serial.println(F("[Event] DomeOpenWarning (>30s open)"));
            break;
        case sfm::DispenseEvent::NoFeedPresented:
            Serial.println(F("[Event] NoFeedPresented"));
            finishCycle(true);
            break;
        case sfm::DispenseEvent::Fault:
            printFaultDetail();
            finishCycle(false);
            break;
        default:
            break;
    }
}

void setup() {
    Serial.begin(115200);
    while (!Serial) {}
    Serial.println(F("SFM DispenseTest"));
    printHelp();
    Serial.println(F("LEDs: 10=pellet present  9=dome open"));

    if (leds.begin() != sfm::ServiceStatus::Ok) {
        Serial.println(F("ERROR: leds.begin() failed"));
    }

    dispenser.setMotorSpeed(kMotorSpeed);
    dispenser.setLowerSteps(kLowerSteps);
    dispenser.setSeekAwaySteps(kSeekAwaySteps);
    dispenser.setGrabSteps(kGrabSteps);
    dispenser.setRaiseSteps(kRaiseSteps);
    dispenser.setFeedTimeoutMs(kFeedTimeoutMs);
    dispenser.setRaiseTimeoutMs(raiseTimeoutMs);

    if (dispenser.begin() != sfm::ServiceStatus::Ok) {
        Serial.println(F("ERROR: dispenser.begin() failed"));
    } else {
        Serial.println(F("Dispenser ready."));
        Serial.print(F("raiseSteps = "));
        Serial.println(currentRaiseSteps);
    }
    seenState = dispenser.state();
}

void loop() {
    dispenser.update();

    // Live sensor mirrors (same mapping as SFM::updateSensorLeds).
    leds.setLed10(dispenser.pelletOnPlate());
    leds.setLed9(dispenser.domeOpen());

    tracePhase();
    traceBeam();
    nudgeDwell();
    handleDispenseEvent(dispenser.takeEvent());
    serviceGap();

    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') {
            lineBuf[lineIdx] = '\0';
            if (lineIdx > 0) handleLine(lineBuf);
            lineIdx = 0;
        } else if (lineIdx < sizeof(lineBuf) - 1) {
            lineBuf[lineIdx++] = c;
        }
    }
}
