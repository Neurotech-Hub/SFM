// StepperMotorTest – hardware bring-up test for VFM 28BYJ-48 steppers.
//
// Motor 1 (M1) – Pellet feeder   : GPIO 35(Orange) 36(Yellow) 37(Pink) 38(Blue)
// Motor 2 (M2) – Actuator        : GPIO 40(Orange) 41(Yellow) 42(Pink) 43(Blue)
// Red wire on both motors → +5 V (common).
//
// Open Serial Monitor at 115200 baud.
//
// IMPORTANT: uses setSpeed() + runSpeed() exclusively (no move()/run()).
// Mixing move() with setSpeed() corrupts AccelStepper's internal speed and
// causes the motor to lock – drawing current but not rotating.
//
// Commands:
//   1  – M1 jog forward (single)   |  q  – M1 jog reverse (single)
//   c  – M1 continuous forward     |  v  – M1 continuous reverse
//        (pattern: 500 steps → pause 1 s → repeat until x)
//   2  – M2 jog forward            |  w  – M2 jog reverse
//   x  – stop and de-energise both motors
//   +  – increase speed by 50 steps/s
//   -  – decrease speed by 50 steps/s
//   p  – print step counters and speed
//   h  – help

#include <VFM.h>
#include <AccelStepper.h>

using namespace vfm;

static constexpr long     kJogSteps      = 500;
static constexpr long     kContBurstSteps = 500;
static constexpr uint32_t kContPauseMs   = 1000;
static constexpr float    kDefaultSpeed  = 400.0f;
static constexpr float    kMinSpeed      = 100.0f;
static constexpr float    kMaxSpeed      = 800.0f;

// AccelStepper HALF4WIRE: (A1, A3, A2, A4) = Orange, Pink, Yellow, Blue
AccelStepper motor1(AccelStepper::HALF4WIRE,
                    PIN_M1_A1, PIN_M1_A3, PIN_M1_A2, PIN_M1_A4);
AccelStepper motor2(AccelStepper::HALF4WIRE,
                    PIN_M2_A1, PIN_M2_A3, PIN_M2_A2, PIN_M2_A4);

float  speed      = kDefaultSpeed;
long   m1Steps    = 0;   // remaining half-steps for single jog
long   m2Steps    = 0;
long   m1Total    = 0;
long   m2Total    = 0;
int8_t m1Continuous = 0; // 0=off, +1=forward, -1=reverse
long   m1ContLeft = 0;   // steps left in current continuous burst
uint32_t m1PauseUntilMs = 0; // non-zero while paused between bursts

void stopM1() {
    motor1.setSpeed(0);
    motor1.disableOutputs();
    m1Steps = 0;
    m1Continuous = 0;
    m1ContLeft = 0;
    m1PauseUntilMs = 0;
}

void deenergiseAll() {
    stopM1();
    motor2.setSpeed(0);
    motor2.disableOutputs();
    m2Steps = 0;
}

void startJog(AccelStepper &motor, long &remaining, float jogSpeed) {
    motor.enableOutputs();
    motor.setCurrentPosition(0);
    motor.setSpeed(jogSpeed);
    remaining = abs((long)kJogSteps);
}

void beginM1ContBurst() {
    m1ContLeft = kContBurstSteps;
    m1PauseUntilMs = 0;
    motor1.enableOutputs();
    motor1.setCurrentPosition(0);
    motor1.setSpeed(m1Continuous > 0 ? speed : -speed);
}

void startM1Continuous(int8_t dir) {
    m1Steps = 0;
    m1Continuous = dir;
    beginM1ContBurst();
    Serial.print(F("[M1] Continuous "));
    Serial.print(dir > 0 ? F("FORWARD") : F("REVERSE"));
    Serial.print(F("  (")); Serial.print(kContBurstSteps);
    Serial.print(F(" steps / ")); Serial.print(kContPauseMs);
    Serial.println(F(" ms pause, x to stop)"));
}

void applySpeedToRunning() {
    if (m1Continuous != 0 && m1PauseUntilMs == 0 && m1ContLeft > 0) {
        motor1.setSpeed(m1Continuous > 0 ? speed : -speed);
    }
}

void runMotors() {
    // M1 continuous: 500 steps → pause 1 s → repeat
    if (m1Continuous != 0) {
        if (m1PauseUntilMs != 0) {
            if ((int32_t)(millis() - m1PauseUntilMs) >= 0) {
                beginM1ContBurst();
            }
        } else if (m1ContLeft > 0) {
            if (motor1.runSpeed()) {
                m1ContLeft--;
                m1Total += m1Continuous;
                if (m1ContLeft == 0) {
                    motor1.setSpeed(0);
                    motor1.disableOutputs();
                    m1PauseUntilMs = millis() + kContPauseMs;
                }
            }
        }
    } else if (m1Steps > 0) {
        if (motor1.runSpeed()) {
            m1Steps--;
            if (m1Steps == 0) motor1.disableOutputs();
        }
    }

    if (m2Steps > 0) {
        if (motor2.runSpeed()) {
            m2Steps--;
            if (m2Steps == 0) motor2.disableOutputs();
        }
    }
}

void printStatus() {
    Serial.print(F("[Motor] speed="));  Serial.print(speed, 0);
    Serial.print(F(" steps/s  jog="));  Serial.print(kJogSteps);
    Serial.print(F("  M1 total="));     Serial.print(m1Total);
    Serial.print(F("  M2 total="));     Serial.print(m2Total);
    Serial.print(F("  M1 cont="));
    if (m1Continuous == 0) {
        Serial.println(F("OFF"));
    } else {
        Serial.print(m1Continuous > 0 ? F("FWD") : F("REV"));
        if (m1PauseUntilMs != 0) Serial.println(F(" (pause)"));
        else {
            Serial.print(F(" left=")); Serial.println(m1ContLeft);
        }
    }
}

void printHelp() {
    Serial.println(F("Commands:"));
    Serial.println(F("  1 / q   M1 jog forward / reverse (single burst)"));
    Serial.println(F("  c / v   M1 continuous: 500 steps, pause 1s, repeat"));
    Serial.println(F("  2 / w   M2 jog forward / reverse"));
    Serial.println(F("  x       stop + de-energise both"));
    Serial.println(F("  + / -   speed ±50 steps/s (applies to running continuous)"));
    Serial.println(F("  p       status    h  help"));
}

void setup() {
    Serial.begin(115200);
    while (!Serial && millis() < 3000) {}

    Serial.println(F("\n===== VFM StepperMotorTest ====="));
    Serial.println(F("M1 = feeder (GPIO35-38)  |  M2 = actuator (GPIO40-43)"));
    Serial.println(F("Red wire -> +5 V on both motors"));
    Serial.print(F("Default speed: ")); Serial.print(kDefaultSpeed, 0);
    Serial.println(F(" half-steps/s"));
    printHelp();

    motor1.setMaxSpeed(kMaxSpeed);
    motor2.setMaxSpeed(kMaxSpeed);

    deenergiseAll();
}

void loop() {
    runMotors();

    if (!Serial.available()) return;

    char cmd = (char)Serial.read();
    switch (cmd) {
        case '1':
            stopM1();
            startJog(motor1, m1Steps, speed);
            m1Total += kJogSteps;
            Serial.print(F("[M1] Jog forward ")); Serial.print(kJogSteps);
            Serial.print(F(" steps  total=")); Serial.println(m1Total);
            break;
        case 'q':
            stopM1();
            startJog(motor1, m1Steps, -speed);
            m1Total -= kJogSteps;
            Serial.print(F("[M1] Jog reverse ")); Serial.print(kJogSteps);
            Serial.print(F(" steps  total=")); Serial.println(m1Total);
            break;
        case 'c':
            startM1Continuous(+1);
            break;
        case 'v':
            startM1Continuous(-1);
            break;
        case '2':
            startJog(motor2, m2Steps, speed);
            m2Total += kJogSteps;
            Serial.print(F("[M2] Forward ")); Serial.print(kJogSteps);
            Serial.print(F(" steps  total=")); Serial.println(m2Total);
            break;
        case 'w':
            startJog(motor2, m2Steps, -speed);
            m2Total -= kJogSteps;
            Serial.print(F("[M2] Reverse ")); Serial.print(kJogSteps);
            Serial.print(F(" steps  total=")); Serial.println(m2Total);
            break;
        case 'x':
            deenergiseAll();
            m1Total = 0;
            m2Total = 0;
            Serial.println(F("[Motor] Stopped, coils de-energised"));
            break;
        case '+':
            speed = min(kMaxSpeed, speed + 50.0f);
            applySpeedToRunning();
            printStatus();
            break;
        case '-':
            speed = max(kMinSpeed, speed - 50.0f);
            applySpeedToRunning();
            printStatus();
            break;
        case 'p':
            printStatus();
            break;
        case 'h':
            printHelp();
            break;
        default:
            break;
    }
}
