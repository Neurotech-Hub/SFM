// ActuatorCalTest – M2 actuator calibration (raise / load / grab positions).
//
// Matches DispenserService motor settings:
//   AccelStepper HALF4WIRE (A1, A3, A2, A4) = Orange, Pink, Yellow, Blue
//   setSpeed() + runSpeed() only  (never stop()/move()/run())
//   +speed = UP,  -speed = DOWN
//   Default speed = kDefaultMotorSpeed (500 steps/s)
//   setMaxSpeed(speed * 2)
//
// PG2 (GPIO45, active LOW / INPUT_PULLUP): pellet loading / home position.
// If GPIO45 is unusable on your board, change kPg2Pin below to PIN_PGX (GPIO6).
//
// Open Serial Monitor at 115200 baud.
//
// Typical calibration flow. NOTE the order: PG2 is only a reference point, the
// firmware feeds kDefaultGrabSteps BELOW it, and raiseSteps is measured from
// there — so find the grab depth first, then zero, then jog up.
//   1. home     – drive DOWN until PG2 triggers → loading position (pos=0)
//   2. d <n>    – jog DOWN n steps (IGNORES PG2) until M1 can drop a pellet
//                 cleanly; that n is kDefaultGrabSteps (bench value: 280)
//   3. z        – zero here, at the pellet-drop position
//   4. u <n>    – jog UP n steps toward the presentation dome, repeating until
//                 the height looks right; 's' then reads out kDefaultRaiseSteps
//                 measured from the DROP position (bench value: 1600)
//   5. home     – return to PG2 loading position when done
//
// Commands (line-based; Enter required):
//   home        drive down to PG2 and zero (loading position)
//   u [n]       jog UP n steps   (default 50) – finds raiseSteps
//   d [n]       jog DOWN n steps (default 50) – ignores PG2, finds grab depth
//   x           abort motion, de-energise
//   z           zero position here (without moving)
//   s           print status (pos, raiseSteps candidate, PG2, speed)
//   + / -       speed ±50 steps/s
//   h / help    this help

#include <VFM.h>
#include <AccelStepper.h>

using namespace vfm;

// --- Match library defaults ---
static constexpr float    kDefaultSpeed = kDefaultMotorSpeed; // 500
static constexpr float    kMinSpeed     = 100.0f;
static constexpr float    kMaxSpeed     = 800.0f;
static constexpr long     kDefaultJog   = 50;
static constexpr uint32_t kDebounceMs   = kSensorDebounceMs; // 20
static constexpr uint32_t kHomeTimeoutMs = kDefaultLowerTimeoutMs; // 8000
static constexpr long     kHomeMaxSteps  = kDefaultLowerSteps;     // 2048

// PG2 pin: library default is PIN_PG2 (45). Use PIN_PGX if you bodged to GPIO6.
static constexpr uint8_t kPg2Pin = PIN_PG2;

AccelStepper motor2(AccelStepper::HALF4WIRE,
                    PIN_M2_A1, PIN_M2_A3, PIN_M2_A2, PIN_M2_A4);

enum class Mode : uint8_t { Idle, Homing, JogUp, JogDown };

float    speed        = kDefaultSpeed;
Mode     mode         = Mode::Idle;
long     remaining    = 0;   // steps left in current jog
long     position     = 0;   // signed: 0 = last PG2 home, + = above home (UP)
bool     pg2Raw       = false;
bool     pg2State     = false;
uint32_t pg2LastChangeMs = 0;
uint32_t homeStartMs  = 0;

static char    lineBuf[24];
static uint8_t lineIdx = 0;

// ---------------------------------------------------------------------------
void haltMotor() {
    motor2.setSpeed(0);
    motor2.disableOutputs();
    remaining = 0;
    mode = Mode::Idle;
}

void updatePg2() {
    uint32_t now = millis();
    bool raw = (digitalRead(kPg2Pin) == LOW); // active LOW
    if (raw != pg2Raw) {
        pg2Raw = raw;
        pg2LastChangeMs = now;
    }
    if ((now - pg2LastChangeMs) >= kDebounceMs) {
        pg2State = pg2Raw;
    }
}

void printStatus() {
    Serial.print(F("[CAL] pos="));
    Serial.print(position);
    Serial.print(F("  raiseSteps="));
    Serial.print(position > 0 ? position : 0);
    Serial.print(F("  PG2="));
    Serial.print(pg2State ? F("TRIGGERED(home)") : F("clear"));
    Serial.print(F("  speed="));
    Serial.print(speed, 0);
    Serial.print(F("  mode="));
    switch (mode) {
        case Mode::Idle:   Serial.println(F("Idle")); break;
        case Mode::Homing: Serial.println(F("Homing")); break;
        case Mode::JogUp:  Serial.println(F("JogUp")); break;
        case Mode::JogDown:Serial.println(F("JogDown")); break;
    }
}

void printHelp() {
    Serial.println(F("Commands:"));
    Serial.println(F("  home     DOWN until PG2 → loading pos (zero)"));
    Serial.println(F("  u [n]    jog UP n steps (default 50) → find raiseSteps"));
    Serial.println(F("  d [n]    jog DOWN n steps (default 50) – IGNORES PG2 → grab depth"));
    Serial.println(F("  x        abort + de-energise"));
    Serial.println(F("  z        zero position here"));
    Serial.println(F("  s        status"));
    Serial.println(F("  + / -    speed ±50"));
    Serial.println(F("  h        help"));
    Serial.println(F("Library: +speed=UP  -speed=DOWN  HALF4WIRE setSpeed/runSpeed"));
}

void startHome() {
    if (pg2State) {
        // Already at loading position
        motor2.setCurrentPosition(0);
        position = 0;
        haltMotor();
        Serial.println(F("[CAL] Already on PG2 – zeroed (loading position)"));
        printStatus();
        return;
    }

    motor2.enableOutputs();
    motor2.setCurrentPosition(0);
    motor2.setSpeed(-speed); // DOWN toward PG2
    remaining = kHomeMaxSteps;
    homeStartMs = millis();
    mode = Mode::Homing;
    Serial.println(F("[CAL] Homing DOWN to PG2..."));
}

void startJog(long steps, bool up) {
    if (steps <= 0) {
        Serial.println(F("[CAL] Step count must be > 0"));
        return;
    }
    if (mode != Mode::Idle) {
        Serial.println(F("[CAL] Busy – type 'x' to abort first"));
        return;
    }

    motor2.enableOutputs();
    motor2.setCurrentPosition(0);
    motor2.setSpeed(up ? speed : -speed);
    remaining = steps;
    mode = up ? Mode::JogUp : Mode::JogDown;

    Serial.print(F("[CAL] Jog "));
    Serial.print(up ? F("UP ") : F("DOWN "));
    Serial.print(steps);
    Serial.println(F(" steps"));
    if (!up) {
        Serial.println(F("[CAL] DOWN ignores PG2 (grab-depth calibration) – 'z' to zero here"));
    }
}

void runMotion() {
    if (mode == Mode::Idle) return;

    if (mode == Mode::Homing) {
        if ((millis() - homeStartMs) >= kHomeTimeoutMs || remaining <= 0) {
            haltMotor();
            Serial.println(F("[CAL] ERROR: home timeout – PG2 not seen"));
            printStatus();
            return;
        }
        if (pg2State) {
            motor2.setSpeed(0);
            motor2.disableOutputs();
            motor2.setCurrentPosition(0);
            position = 0;
            remaining = 0;
            mode = Mode::Idle;
            Serial.println(F("[CAL] PG2 hit – at loading position (pos=0)"));
            printStatus();
            return;
        }
        if (motor2.runSpeed()) {
            remaining--;
        }
        return;
    }

    // Jog up / down
    if (remaining <= 0) {
        haltMotor();
        Serial.print(F("[CAL] Jog done. pos="));
        Serial.print(position);
        if (position > 0) {
            Serial.print(F("  → candidate raiseSteps="));
            Serial.print(position);
        }
        Serial.println();
        printStatus();
        return;
    }

    // JogDown intentionally does NOT stop on PG2
    if (motor2.runSpeed()) {
        remaining--;
        if (mode == Mode::JogUp) {
            position++;
        } else {
            position--;
        }
    }
}

long parseOptionalSteps(const char *p, long defaultSteps) {
    while (*p == ' ') p++;
    if (*p == '\0') return defaultSteps;
    long n = atol(p);
    return (n > 0) ? n : defaultSteps;
}

void handleSerialLine(const char *line) {
    if (strcmp(line, "home") == 0) {
        startHome();
    } else if (line[0] == 'u' || line[0] == 'U') {
        startJog(parseOptionalSteps(line + 1, kDefaultJog), true);
    } else if (line[0] == 'd' || line[0] == 'D') {
        // 'd' alone could be confused; require start with d and optional number
        // but not "down" as separate – accept "d" / "d 100"
        if (strncmp(line, "down", 4) == 0) {
            startJog(parseOptionalSteps(line + 4, kDefaultJog), false);
        } else {
            startJog(parseOptionalSteps(line + 1, kDefaultJog), false);
        }
    } else if (strcmp(line, "x") == 0) {
        haltMotor();
        Serial.println(F("[CAL] Aborted"));
        printStatus();
    } else if (strcmp(line, "z") == 0) {
        motor2.setCurrentPosition(0);
        position = 0;
        Serial.println(F("[CAL] Position zeroed here"));
        printStatus();
    } else if (strcmp(line, "s") == 0) {
        printStatus();
    } else if (strcmp(line, "+") == 0) {
        speed = min(kMaxSpeed, speed + 50.0f);
        motor2.setMaxSpeed(speed * 2.0f);
        printStatus();
    } else if (strcmp(line, "-") == 0) {
        speed = max(kMinSpeed, speed - 50.0f);
        motor2.setMaxSpeed(speed * 2.0f);
        printStatus();
    } else if (strcmp(line, "h") == 0 || strcmp(line, "help") == 0) {
        printHelp();
    } else if (line[0] != '\0') {
        Serial.print(F("Unknown: ")); Serial.println(line);
    }
}

// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    while (!Serial && millis() < 3000) {}

    Serial.println(F("\n===== VFM ActuatorCalTest (M2) ====="));
    Serial.println(F("M2 = actuator GPIO40-43  |  PG2 = loading home"));
    Serial.print(F("PG2 pin=GPIO")); Serial.println(kPg2Pin);
    Serial.print(F("Speed=")); Serial.print(kDefaultSpeed, 0);
    Serial.print(F(" steps/s  (library kDefaultMotorSpeed)"));
    Serial.print(F("  raise default=")); Serial.println(kDefaultRaiseSteps);
    printHelp();

    pinMode(kPg2Pin, INPUT_PULLUP);
    pg2Raw = (digitalRead(kPg2Pin) == LOW);
    pg2State = pg2Raw;
    pg2LastChangeMs = millis();

    motor2.setMaxSpeed(speed * 2.0f);
    haltMotor();
    printStatus();
}

void loop() {
    updatePg2();
    runMotion();

    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r' || c == '\n') {
            if (lineIdx > 0) {
                lineBuf[lineIdx] = '\0';
                handleSerialLine(lineBuf);
                lineIdx = 0;
            }
            continue;
        }
        if (lineIdx < (sizeof(lineBuf) - 1)) {
            lineBuf[lineIdx++] = c;
        }
    }
}
