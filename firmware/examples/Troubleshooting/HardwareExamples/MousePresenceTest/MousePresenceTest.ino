// MousePresenceTest – capacitive mouse-presence sensor bring-up + calibration.
//
// Pin:
//   GPIO5  – PIN_PRESENCE (ESP32-S3 touchRead)
//   GPIO11 – PIN_BTN (short press starts calibration)
//   GPIO9  – LED_IO_9 (solid ON while calibrating; confirm flash when done)
//
// Bench behaviour:
//   Idle     ~ 35 000 – 35 500
//   Present  ~ 36 000 – 100 000   (raw INCREASES when animal is present)
//   presence = (raw > threshold)
//
// Calibration (button or serial 'c'):
//   Keep the pad CLEAR (no mouse) for 5 s.
//   Collect samples → mean, std_dev (population σ).
//   thr = mean + factor * std_dev
//   (factor is user-settable; default 3.0)
//   LED 9 solid ON during capture; flashConfirm() on success.
//
// Open Serial Monitor at 115200 baud.
//
// Commands:
//   s       print current raw + presence state
//   t <n>   set absolute threshold (e.g. t 40000)
//   f <n>   set cal factor (e.g. f 3 or f 2.5); re-applies if σ known
//   + / -   nudge absolute threshold by 5000
//   m       toggle continuous monitor (default ON)
//   c       start 5 s calibration (same as button)
//   h       help

#include <SFM.h>
#include <math.h>

using namespace sfm;

static constexpr uint32_t kSampleMs      = 200;
static constexpr uint32_t kCalSampleMs   = 25;
static constexpr uint32_t kCalDurationMs = 5000;
static constexpr uint32_t kDefaultThr    = 35000;
static constexpr uint32_t kNudge         = 5000;
static constexpr uint32_t kBtnDebounceMs = 30;
static constexpr float    kDefaultFactor = 3.0f;
static constexpr float    kMinFactor     = 0.1f;
static constexpr float    kMaxFactor     = 100.0f;

LedService leds;

uint32_t threshold   = kDefaultThr;
float    calFactor   = kDefaultFactor;
bool     present     = false;
bool     prevPresent = false;
bool     monitoring  = true;
uint32_t lastSampleMs = 0;
uint32_t lastRaw      = 0;

// Last successful cal stats (for re-apply when factor changes)
bool     calValid    = false;
float    calMean     = 0.0f;
float    calStdDev   = 0.0f;

// Calibration FSM + Welford online mean / variance
enum class CalState : uint8_t { Idle, Running };
CalState calState        = CalState::Idle;
uint32_t calStartMs      = 0;
uint32_t calLastSampleMs = 0;
uint32_t calCount        = 0;
double   calMeanAcc      = 0.0;  // running mean
double   calM2Acc        = 0.0;  // sum of squares of differences from mean

// Button (active LOW)
bool     btnWasPressed   = false;
uint32_t btnLastChangeMs = 0;

static char    lineBuf[24];
static uint8_t lineIdx = 0;

static bool isPresent(uint32_t raw, uint32_t thr) {
    return raw > thr;
}

static uint32_t thresholdFromStats(float mean, float stdDev, float factor) {
    double thr = (double)mean + (double)factor * (double)stdDev;
    if (thr < 1.0) thr = 1.0;
    if (thr > (double)UINT32_MAX) thr = (double)UINT32_MAX;
    return (uint32_t)(thr + 0.5);
}

void printHelp() {
    Serial.println(F("Commands:"));
    Serial.println(F("  s       current raw + state"));
    Serial.println(F("  t <n>   set absolute threshold"));
    Serial.println(F("  f <n>   set factor (thr = mean + factor*std_dev)"));
    Serial.println(F("  + / -   nudge absolute threshold by 5000"));
    Serial.println(F("  m       toggle continuous monitor"));
    Serial.println(F("  c       5 s calibration (or press BTN_IO_11)"));
    Serial.println(F("  h       help"));
    Serial.println(F("Logic: raw > threshold => PRESENT"));
    Serial.println(F("Cal: thr = mean + factor * std_dev; pad must stay CLEAR"));
}

void applyThreshold(uint32_t thr) {
    threshold = thr;
    present = isPresent(lastRaw, threshold);
    prevPresent = present;

    Serial.print(F("[PRESENCE] thr=")); Serial.print(threshold);
    Serial.print(F("  raw=")); Serial.print(lastRaw);
    Serial.print(F("  factor=")); Serial.print(calFactor, 2);
    Serial.print(F("  -> "));
    Serial.println(present ? F("PRESENT") : F("clear"));
}

void applyFactor(float factor) {
    if (factor < kMinFactor) factor = kMinFactor;
    if (factor > kMaxFactor) factor = kMaxFactor;
    calFactor = factor;

    Serial.print(F("[PRESENCE] factor=")); Serial.println(calFactor, 2);

    if (calValid) {
        uint32_t thr = thresholdFromStats(calMean, calStdDev, calFactor);
        Serial.print(F("[PRESENCE] Re-apply from last cal: mean="));
        Serial.print(calMean, 1);
        Serial.print(F("  std_dev=")); Serial.print(calStdDev, 1);
        Serial.print(F("  thr=mean+f*σ=")); Serial.println(thr);
        applyThreshold(thr);
    } else {
        Serial.println(F("[PRESENCE] No cal stats yet – run 'c' or press button"));
    }
}

void printStatus() {
    Serial.print(F("[PRESENCE] raw="));
    Serial.print(lastRaw);
    Serial.print(F("  thr="));
    Serial.print(threshold);
    Serial.print(F("  factor="));
    Serial.print(calFactor, 2);
    if (calValid) {
        Serial.print(F("  mean=")); Serial.print(calMean, 1);
        Serial.print(F("  σ=")); Serial.print(calStdDev, 1);
    }
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

    Serial.print(F("[PRESENCE] CAL START – keep pad CLEAR for 5 s...  factor="));
    Serial.println(calFactor, 2);
    Serial.println(F("  LED9 solid ON during capture"));

    leds.stopAllBlinks();
    leds.setLed9(true);

    calState = CalState::Running;
    calStartMs = millis();
    calLastSampleMs = 0;
    calCount = 0;
    calMeanAcc = 0.0;
    calM2Acc = 0.0;
}

void finishCalibration(bool ok) {
    calState = CalState::Idle;
    leds.setLed9(false);

    if (!ok || calCount < 10) {
        Serial.println(F("[PRESENCE] CAL FAILED – not enough samples; threshold unchanged"));
        return;
    }

    // Population std_dev (σ); use sample σ (n-1) if you prefer — population is fine for N~200
    double variance = (calCount > 0) ? (calM2Acc / (double)calCount) : 0.0;
    if (variance < 0.0) variance = 0.0;
    float mean = (float)calMeanAcc;
    float stdDev = (float)sqrt(variance);

    calValid = true;
    calMean = mean;
    calStdDev = stdDev;

    uint32_t thr = thresholdFromStats(mean, stdDev, calFactor);

    Serial.print(F("[PRESENCE] CAL DONE  samples=")); Serial.print(calCount);
    Serial.print(F("  mean=")); Serial.print(mean, 1);
    Serial.print(F("  std_dev=")); Serial.print(stdDev, 1);
    Serial.print(F("  factor=")); Serial.println(calFactor, 2);
    Serial.print(F("[PRESENCE] thr = mean + factor*std_dev = ")); Serial.println(thr);

    applyThreshold(thr);

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

    // Welford online algorithm
    double x = (double)touchRead(PIN_PRESENCE);
    calCount++;
    double delta = x - calMeanAcc;
    calMeanAcc += delta / (double)calCount;
    double delta2 = x - calMeanAcc;
    calM2Acc += delta * delta2;
}

void updateButton() {
    const uint32_t now = millis();
    bool pressed = (digitalRead(PIN_BTN) == LOW);

    if (pressed == btnWasPressed) return;
    if ((now - btnLastChangeMs) < kBtnDebounceMs) return;

    btnLastChangeMs = now;
    btnWasPressed = pressed;

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
    } else if (line[0] == 'f' || line[0] == 'F') {
        const char *p = line + 1;
        while (*p == ' ') p++;
        if (*p == '\0') {
            Serial.print(F("Current factor=")); Serial.println(calFactor, 2);
            Serial.println(F("Usage: f <n>  (e.g. f 3 or f 2.5)"));
            return;
        }
        char *end = nullptr;
        float f = strtof(p, &end);
        if (end == p || f <= 0.0f) {
            Serial.println(F("Invalid factor"));
            return;
        }
        applyFactor(f);
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

    Serial.println(F("\n===== SFM Mouse Presence Test ====="));
    Serial.println(F("PIN_PRESENCE=GPIO5  BTN=GPIO11  LED9=GPIO9"));
    Serial.println(F("presence = raw > thr"));
    Serial.println(F("Cal: thr = mean + factor*std_dev (5 s, pad CLEAR)"));
    Serial.print(F("Default thr=")); Serial.print(kDefaultThr);
    Serial.print(F("  factor=")); Serial.println(kDefaultFactor, 2);
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
