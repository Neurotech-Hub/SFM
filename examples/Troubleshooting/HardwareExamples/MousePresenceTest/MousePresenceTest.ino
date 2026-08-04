// MousePresenceTest – capacitive mouse-presence sensor bring-up + calibration.
//
// Pin:
//   GPIO5  – PIN_PRESENCE (ESP32-S3 touchRead)
//   GPIO11 – PIN_BTN (short press starts calibration)
//   GPIO9  – LED_IO_9 (solid ON while calibrating; confirm flash when done)
//
// Bench behaviour:
//   Idle     ~ 35 000 – 35 500
//   Present  ~ 36 000 – 1 00 000   (raw INCREASES when animal is present)
//   presence = (raw > threshold)
//
// Calibration (button or serial 'c'):
//   Keep the pad CLEAR (no mouse) for 5 s.
//   thr = max + (max - min)   // one noise-range above the highest idle reading
//   LED 9 solid ON during capture; flashConfirm() (same as NVS clear) on success.
//
// Open Serial Monitor at 115200 baud.
//
// Commands:
//   s       print current raw + presence state
//   t <n>   set threshold (e.g. t 40000)
//   + / -   nudge threshold by 5000
//   m       toggle continuous monitor (default ON)
//   c       start 5 s calibration (same as button)
//   h       help

#include <VFM.h>

using namespace vfm;

static constexpr uint32_t kSampleMs      = 200;
static constexpr uint32_t kCalSampleMs   = 25;
static constexpr uint32_t kCalDurationMs = 5000;
static constexpr uint32_t kDefaultThr    = 35000;
static constexpr uint32_t kNudge         = 5000;
static constexpr uint32_t kBtnDebounceMs = 30;

LedService leds;

uint32_t threshold   = kDefaultThr;
bool     present     = false;
bool     prevPresent = false;
bool     monitoring  = true;
uint32_t lastSampleMs = 0;
uint32_t lastRaw      = 0;

// Calibration FSM
enum class CalState : uint8_t { Idle, Running };
CalState calState     = CalState::Idle;
uint32_t calStartMs   = 0;
uint32_t calLastSampleMs = 0;
uint32_t calMin       = 0;
uint32_t calMax       = 0;
uint64_t calSum       = 0;
uint32_t calCount     = 0;

// Button (active LOW)
bool     btnWasPressed   = false;
uint32_t btnLastChangeMs = 0;

static char    lineBuf[24];
static uint8_t lineIdx = 0;

static bool isPresent(uint32_t raw, uint32_t thr) {
    return raw > thr;
}

void printHelp() {
    Serial.println(F("Commands:"));
    Serial.println(F("  s       current raw + state"));
    Serial.println(F("  t <n>   set threshold"));
    Serial.println(F("  + / -   nudge threshold by 5000"));
    Serial.println(F("  m       toggle continuous monitor"));
    Serial.println(F("  c       5 s calibration (or press BTN_IO_11)"));
    Serial.println(F("  h       help"));
    Serial.println(F("Logic: raw > threshold => PRESENT"));
    Serial.println(F("Cal: thr = idle_max + (idle_max - idle_min); pad must stay CLEAR"));
}

void applyThreshold(uint32_t thr) {
    threshold = thr;
    present = isPresent(lastRaw, threshold);
    prevPresent = present;

    Serial.print(F("[PRESENCE] thr=")); Serial.print(threshold);
    Serial.print(F("  raw=")); Serial.print(lastRaw);
    Serial.print(F("  -> "));
    Serial.println(present ? F("PRESENT") : F("clear"));
}

void printStatus() {
    Serial.print(F("[PRESENCE] raw="));
    Serial.print(lastRaw);
    Serial.print(F("  thr="));
    Serial.print(threshold);
    Serial.print(F("  -> "));
    Serial.println(present ? F("PRESENT") : F("clear"));
}

void sampleOnce() {
    lastRaw = touchRead(PIN_PRESENCE);
    present = isPresent(lastRaw, threshold);
}

void startCalibration() {
    if (calState == CalState::Running) {
        Serial.println(F("[PRESENCE] Calibration already running"));
        return;
    }

    Serial.println(F("[PRESENCE] CAL START – keep pad CLEAR for 5 s..."));
    Serial.println(F("  LED9 solid ON during capture"));

    leds.stopAllBlinks();
    leds.setLed9(true);

    calState = CalState::Running;
    calStartMs = millis();
    calLastSampleMs = 0;
    calMin = UINT32_MAX;
    calMax = 0;
    calSum = 0;
    calCount = 0;
}

void finishCalibration(bool ok) {
    calState = CalState::Idle;
    leds.setLed9(false);

    if (!ok || calCount < 10) {
        Serial.println(F("[PRESENCE] CAL FAILED – not enough samples; threshold unchanged"));
        return;
    }

    uint32_t range = calMax - calMin;
    uint32_t thr = calMax + range;
    if (thr <= calMax) thr = calMax + 1; // overflow / zero-range guard

    uint32_t avg = (uint32_t)(calSum / calCount);

    Serial.print(F("[PRESENCE] CAL DONE  samples=")); Serial.print(calCount);
    Serial.print(F("  min=")); Serial.print(calMin);
    Serial.print(F("  max=")); Serial.print(calMax);
    Serial.print(F("  avg=")); Serial.print(avg);
    Serial.print(F("  range=")); Serial.println(range);
    Serial.print(F("[PRESENCE] thr = max + range = ")); Serial.println(thr);

    applyThreshold(thr);

    // Same visual confirm as NVS clear (~600 ms blocking flash)
    leds.flashConfirm();
    Serial.println(F("[PRESENCE] LED confirm flash complete"));
}

void updateCalibration() {
    if (calState != CalState::Running) return;

    const uint32_t now = millis();

    if ((now - calStartMs) >= kCalDurationMs) {
        finishCalibration(true);
        return;
    }

    if (calLastSampleMs != 0 && (now - calLastSampleMs) < kCalSampleMs) return;
    calLastSampleMs = now;

    uint32_t v = touchRead(PIN_PRESENCE);
    if (v < calMin) calMin = v;
    if (v > calMax) calMax = v;
    calSum += v;
    calCount++;
}

void updateButton() {
    const uint32_t now = millis();
    bool pressed = (digitalRead(PIN_BTN) == LOW);

    if (pressed == btnWasPressed) return;
    if ((now - btnLastChangeMs) < kBtnDebounceMs) return;

    btnLastChangeMs = now;
    btnWasPressed = pressed;

    // Falling edge (press) starts calibration
    if (pressed) {
        startCalibration();
    }
}

void handleSerialLine(const char *line) {
    if (line[0] == 't' || line[0] == 'T') {
        const char *p = line + 1;
        while (*p == ' ') p++;
        if (*p == '\0') {
            Serial.println(F("Usage: t <n>"));
            return;
        }
        char *end = nullptr;
        unsigned long n = strtoul(p, &end, 10);
        if (end == p) {
            Serial.println(F("Invalid threshold"));
            return;
        }
        applyThreshold((uint32_t)n);
    } else if (strcmp(line, "s") == 0) {
        sampleOnce();
        printStatus();
    } else if (strcmp(line, "m") == 0) {
        monitoring = !monitoring;
        Serial.print(F("Monitor ")); Serial.println(monitoring ? F("ON") : F("OFF"));
    } else if (strcmp(line, "c") == 0) {
        startCalibration();
    } else if (strcmp(line, "h") == 0 || strcmp(line, "help") == 0) {
        printHelp();
    } else if (line[0] != '\0') {
        Serial.print(F("Unknown: ")); Serial.println(line);
    }
}

void setup() {
    Serial.begin(115200);
    while (!Serial && millis() < 3000) {}

    Serial.println(F("\n===== VFM Mouse Presence Test ====="));
    Serial.println(F("PIN_PRESENCE=GPIO5  BTN=GPIO11  LED9=GPIO9"));
    Serial.println(F("presence = raw > thr   |  press BTN or 'c' to calibrate (5 s, pad clear)"));
    Serial.print(F("Default threshold: ")); Serial.println(kDefaultThr);
    printHelp();

    leds.begin();
    pinMode(PIN_BTN, INPUT_PULLUP);

    sampleOnce();
    prevPresent = present;
    printStatus();
    lastSampleMs = millis();
}

void loop() {
    leds.update();
    updateButton();
    updateCalibration();

    const uint32_t now = millis();

    // Pause normal presence streaming while calibrating
    if (calState == CalState::Idle && (now - lastSampleMs) >= kSampleMs) {
        lastSampleMs = now;
        sampleOnce();

        if (present != prevPresent) {
            Serial.print(F("[PRESENCE] "));
            Serial.print(present ? F("PRESENT") : F("clear"));
            Serial.print(F("  raw=")); Serial.print(lastRaw);
            Serial.print(F("  thr=")); Serial.println(threshold);
            prevPresent = present;
        } else if (monitoring) {
            printStatus();
        }
    }

    while (Serial.available()) {
        char c = (char)Serial.read();

        if (c == '+' || c == '=') {
            applyThreshold(threshold + kNudge);
            lineIdx = 0;
            continue;
        }
        if (c == '-' || c == '_') {
            applyThreshold((threshold > kNudge) ? (threshold - kNudge) : 0);
            lineIdx = 0;
            continue;
        }

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
