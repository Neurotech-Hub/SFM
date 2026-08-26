// DispenseTest – serial-driven bench test for the VFM DispenserService.
//
// Open the Arduino Serial Monitor at 115200 baud.
//
// Commands:
//   d          – start a dispense cycle (also ends Loaded wait and starts next)
//   a          – recover: stop motion / clear Fault / leave Loaded
//   s          – print current dispenser state + photogate readings
//   +          – increase motor speed by 100 steps/s
//   -          – decrease motor speed by 100 steps/s
//   r          – print current raiseSteps
//   r <n>      – set raiseSteps (e.g. "r 1480" or "r 1200")
//
// LED mirrors (debounced sensor state):
//   LED 10 = pellet present
//   LED 9  = dome open
//
// Defaults match library: grab=280, raise=1480, seek-away=800 (clear or cap), feed timeout=30 s.
// raiseSteps is measured from the pellet-drop position (kDefaultGrabSteps below
// the load sensor), not from the load sensor itself.

#include <VFM.h>

static constexpr float    kMotorSpeed     = 300.0f;
static constexpr long     kLowerSteps     = 3072;
static constexpr long     kSeekAwaySteps  = 800;
static constexpr long     kGrabSteps      = 280;
static constexpr long     kRaiseSteps     = 1480;
static constexpr uint32_t kFeedTimeoutMs  = 30000;

vfm::DispenserService dispenser;
vfm::LedService       leds;
float currentSpeed     = kMotorSpeed;
long  currentRaiseSteps = kRaiseSteps;

static char     lineBuf[48];
static uint8_t  lineIdx = 0;

static const char *stateStr(vfm::DispenseState s) {
    switch (s) {
        case vfm::DispenseState::Idle:        return "Idle";
        case vfm::DispenseState::Seeking:  return "Seeking";
        case vfm::DispenseState::Lowering:    return "Lowering";
        case vfm::DispenseState::Loading:  return "Loading";
        case vfm::DispenseState::Raising:     return "Raising";
        case vfm::DispenseState::Loaded:   return "Loaded";
        case vfm::DispenseState::Fault:       return "Fault";
    }
    return "?";
}

void printStatus() {
    Serial.print(F("[State] "));
    Serial.print(stateStr(dispenser.state()));
    Serial.print(F("  pellet="));        Serial.print(dispenser.pelletOnPlate());
    Serial.print(F(" load_position="));  Serial.print(dispenser.atLoadPosition());
    Serial.print(F(" dome_open="));       Serial.print(dispenser.domeOpen());
    Serial.print(F("  Pellets=")); Serial.print(dispenser.pelletCount());
    Serial.print(F("  raiseSteps=")); Serial.print(currentRaiseSteps);
    Serial.print(F("  speed=")); Serial.println(currentSpeed);
}

void handleLine(const char *line) {
    if (line[0] == '\0') return;

    // "r" or "r <n>" — set / query raiseSteps
    if (line[0] == 'r' || line[0] == 'R') {
        if (line[1] == '\0') {
            Serial.print(F("raiseSteps = "));
            Serial.println(currentRaiseSteps);
            return;
        }
        // Skip optional whitespace after 'r'
        const char *p = line + 1;
        while (*p == ' ' || *p == '\t') p++;
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
        return;
    }

    // Single-char commands (first character of the line)
    switch (line[0]) {
        case 'd':
        case 'D':
            if (dispenser.dispense()) {
                Serial.println(F("Dispense started."));
            } else {
                Serial.print(F("Cannot dispense – state: "));
                Serial.println(stateStr(dispenser.state()));
            }
            break;
        case 'a':
        case 'A':
            dispenser.recover();
            Serial.println(F("Recovered."));
            break;
        case 's':
        case 'S':
            printStatus();
            break;
        case '+':
            currentSpeed += 100.0f;
            dispenser.setMotorSpeed(currentSpeed);
            Serial.print(F("Speed = ")); Serial.println(currentSpeed);
            break;
        case '-':
            currentSpeed = max(100.0f, currentSpeed - 100.0f);
            dispenser.setMotorSpeed(currentSpeed);
            Serial.print(F("Speed = ")); Serial.println(currentSpeed);
            break;
        case 'h':
        case 'H':
            Serial.println(F("Commands: d a s + -  |  r  |  r <n>"));
            break;
        default:
            Serial.print(F("Unknown: "));
            Serial.println(line);
            break;
    }
}

void setup() {
    Serial.begin(115200);
    while (!Serial) {}
    Serial.println(F("VFM DispenseTest"));
    Serial.println(F("Commands: d=dispense  a=recover  s=status  +=faster  -=slower"));
    Serial.println(F("          r         = show raiseSteps"));
    Serial.println(F("          r <n>     = set raiseSteps (e.g. r 1480)"));
    Serial.println(F("PelletTaken returns to Idle; DomeOpened reports each dome lift"));
    Serial.println(F("LEDs: 10=pellet present  9=dome open"));

    if (leds.begin() != vfm::ServiceStatus::Ok) {
        Serial.println(F("ERROR: leds.begin() failed"));
    }

    dispenser.setMotorSpeed(kMotorSpeed);
    dispenser.setLowerSteps(kLowerSteps);
    dispenser.setSeekAwaySteps(kSeekAwaySteps);
    dispenser.setGrabSteps(kGrabSteps);
    dispenser.setRaiseSteps(kRaiseSteps);
    dispenser.setFeedTimeoutMs(kFeedTimeoutMs);

    if (dispenser.begin() != vfm::ServiceStatus::Ok) {
        Serial.println(F("ERROR: dispenser.begin() failed"));
    } else {
        Serial.println(F("Dispenser ready."));
        Serial.print(F("raiseSteps = ")); Serial.println(currentRaiseSteps);
    }
}

void loop() {
    dispenser.update();

    // Live sensor mirrors (same mapping as VFM::updateSensorLeds).
    leds.setLed10(dispenser.pelletOnPlate());
    leds.setLed9(dispenser.domeOpen());

    switch (dispenser.takeEvent()) {
        case vfm::DispenseEvent::OnPlate:
            Serial.println(F("[Event] OnPlate"));
            break;
        case vfm::DispenseEvent::Loaded:
            Serial.print(F("[Event] Loaded  total="));
            Serial.println(dispenser.pelletCount());
            break;
        case vfm::DispenseEvent::DomeOpened:
            Serial.println(F("[Event] DomeOpened"));
            break;
        case vfm::DispenseEvent::PelletTaken:
            Serial.print(F("[Event] PelletTaken  taken="));
            Serial.println(dispenser.takenCount());
            break;
        case vfm::DispenseEvent::FeedSkipped:
            Serial.println(F("[Event] FeedSkipped (plate occupied)"));
            break;
        case vfm::DispenseEvent::DomeOpenWarning:
            Serial.println(F("[Event] DomeOpenWarning (>30s open)"));
            break;
        case vfm::DispenseEvent::Fault:
            Serial.print(F("[Event] FAULT – "));
            Serial.println(
                dispenser.faultCode() == vfm::ServiceStatus::FeedTimeout     ? F("FeedTimeout (out of pellets / refill hopper)") :
                dispenser.faultCode() == vfm::ServiceStatus::ActuatorTimeout ? F("ActuatorTimeout (sensor or M2 position)") :
                dispenser.faultCode() == vfm::ServiceStatus::Jam             ? F("Jam") :
                dispenser.faultCode() == vfm::ServiceStatus::PelletLost      ? F("PelletLost") :
                F("?"));
            break;
        default:
            break;
    }

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
