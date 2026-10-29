#pragma once

#include "../hardware/SFMPins.h"
#include "ServiceTypes.h"
#include <Arduino.h>

namespace sfm {

// LED service for the status LED and the two user IO LEDs.
//
// Blink channels:
//   1. Status LED  (PIN_STATUS_LED) – fault / discovery indication
//   2. LED 9       (PIN_LED_IO_9)   – boot / discovery / button-hold warning,
//                                     then a live dome-open mirror once enabled
//   3. LED 10      (PIN_LED_IO_10)  – live pellet-present mirror; also used in
//                                     confirm flashes
//
// All LEDs are digital (on/off). RGB or PWM extensions can be added later.
class LedService {
public:
  LedService() = default;

  ServiceStatus begin() {
    pinMode(PIN_STATUS_LED, OUTPUT);
    pinMode(PIN_LED_IO_9, OUTPUT);
    pinMode(PIN_LED_IO_10, OUTPUT);
    stopAllBlinks();
    allOff();
    return ServiceStatus::Ok;
  }

  // --- Direct on/off controls ---
  void setStatusLed(bool on) { digitalWrite(PIN_STATUS_LED, on ? HIGH : LOW); }
  void setLed9(bool on)      { digitalWrite(PIN_LED_IO_9,   on ? HIGH : LOW); }
  void setLed10(bool on)     { digitalWrite(PIN_LED_IO_10,  on ? HIGH : LOW); }

  void allOff() {
    setStatusLed(false);
    setLed9(false);
    setLed10(false);
  }

  void allOn() {
    setStatusLed(true);
    setLed9(true);
    setLed10(true);
  }

  // Stop every blink channel so solid / flash patterns are not overridden.
  void stopAllBlinks() {
    statusBlinkMs_ = 0;
    led9BlinkMs_   = 0;
    led10BlinkMs_  = 0;
  }

  // Blocking confirm flash (same pattern as NVS clear): all LEDs on/off N times.
  // ~600 ms for defaults (3 × 100 ms on + 100 ms off). Call sparingly.
  void flashConfirm(uint8_t times = 3, uint32_t onMs = 100, uint32_t offMs = 100) {
    stopAllBlinks();
    for (uint8_t i = 0; i < times; i++) {
      allOn();
      delay(onMs);
      allOff();
      delay(offMs);
    }
  }

  // --- Blink channels (pass 0 to stop) ---
  void setStatusLedBlinkMs(uint32_t ms) {
    statusBlinkMs_    = ms;
    statusBlinkStart_ = millis();
    statusOn_         = false;
  }

  void setLed9BlinkMs(uint32_t ms) {
    led9BlinkMs_    = ms;
    led9BlinkStart_ = millis();
    led9On_         = false;
  }

  void setLed10BlinkMs(uint32_t ms) {
    led10BlinkMs_    = ms;
    led10BlinkStart_ = millis();
    led10On_         = false;
  }

  // Tick all blink channels; call from loop().
  void update() {
    if (statusBlinkMs_ > 0) {
      if ((millis() - statusBlinkStart_) >= statusBlinkMs_) {
        statusBlinkStart_ = millis();
        statusOn_ = !statusOn_;
        setStatusLed(statusOn_);
      }
    }

    if (led9BlinkMs_ > 0) {
      if ((millis() - led9BlinkStart_) >= led9BlinkMs_) {
        led9BlinkStart_ = millis();
        led9On_ = !led9On_;
        setLed9(led9On_);
      }
    }

    if (led10BlinkMs_ > 0) {
      if ((millis() - led10BlinkStart_) >= led10BlinkMs_) {
        led10BlinkStart_ = millis();
        led10On_ = !led10On_;
        setLed10(led10On_);
      }
    }
  }

private:
  uint32_t statusBlinkMs_    = 0;
  uint32_t statusBlinkStart_ = 0;
  bool     statusOn_         = false;

  uint32_t led9BlinkMs_    = 0;
  uint32_t led9BlinkStart_ = 0;
  bool     led9On_         = false;

  uint32_t led10BlinkMs_    = 0;
  uint32_t led10BlinkStart_ = 0;
  bool     led10On_         = false;
};

} // namespace sfm
