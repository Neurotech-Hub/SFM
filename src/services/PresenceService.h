#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include "ServiceTypes.h"
#include "../hardware/VFMPins.h"

namespace vfm {

// NVS key for the calibrated threshold (namespace kNvsNamespace)
constexpr char kNvsKeyPresenceThr[] = "presThr";

// Fallback used until a calibration has been stored in NVS.
// Bench readings: idle ≈ 35 000–35 500, animal present ≈ 36 000 and up.
constexpr uint32_t kDefaultPresenceThreshold = 35000;

// Calibration samples the idle pad, then sets
//   threshold = max + (max - min)
// i.e. one noise range above the highest idle reading. The pad must stay clear
// for the whole capture or the threshold lands above real presence readings.
constexpr uint32_t kPresenceCalMs         = 5000;
constexpr uint32_t kPresenceCalSampleMs   = 25;
constexpr uint32_t kPresenceCalMinSamples = 10;

// touchRead() cadence outside calibration. Several samples per debounce window
// without polling the (comparatively slow) touch peripheral every loop pass.
constexpr uint32_t kPresenceSampleMs = 20;

// Latched one-shot notifications; read with takeEvent().
enum class PresenceEvent : uint8_t {
    None = 0,
    CalibrationStarted,
    CalibrationDone,
    CalibrationFailed,
};

struct PresenceCalibration {
    uint32_t samples   = 0;
    uint32_t minRaw    = 0;
    uint32_t maxRaw    = 0;
    uint32_t avgRaw    = 0;
    uint32_t threshold = 0;  // threshold in force after the attempt
    bool     ok        = false;
};

// ---------------------------------------------------------------------------
// PresenceService
//
// Animal presence detection on the capacitive pad (PIN_PRESENCE, touchRead).
// Raw counts rise when an animal is present, so presence = raw > threshold.
//
// The threshold is calibrated against the idle pad and persisted to NVS, so a
// node keeps its calibration across reboots exactly like its CAN node ID.
// Debounced output means one approach yields one trigger and one clear.
// ---------------------------------------------------------------------------
class PresenceService {
public:
    PresenceService() = default;

    ServiceStatus begin();

    // Non-blocking; call every loop pass. Sampling pauses while calibrating.
    void update();

    // --- Debounced state ---
    bool     present() const     { return present_; }
    uint32_t raw() const         { return raw_; }
    uint32_t threshold() const   { return threshold_; }
    bool     calibrating() const { return calibrating_; }

    // Apply for this run only — used for sketch-level overrides so they cannot
    // overwrite a stored calibration.
    void setThreshold(uint32_t thr);

    // Apply and persist to NVS (deliberate operator action / calibration).
    void saveThreshold(uint32_t thr);

    // Forget the stored value and fall back to kDefaultPresenceThreshold.
    void clearStoredThreshold();

    // Begin a capture of the idle pad. Returns false if one is already running.
    bool startCalibration();
    void cancelCalibration();

    PresenceEvent takeEvent();
    const PresenceCalibration &lastCalibration() const { return lastCal_; }

private:
    Preferences prefs_;

    uint32_t threshold_ = kDefaultPresenceThreshold;
    uint32_t raw_       = 0;

    bool     present_      = false;  // debounced
    bool     rawPresent_   = false;  // undebounced candidate
    uint32_t lastChangeMs_ = 0;      // when rawPresent_ last flipped
    uint32_t lastSampleMs_ = 0;

    bool     calibrating_     = false;
    uint32_t calStartMs_      = 0;
    uint32_t calLastSampleMs_ = 0;
    uint32_t calMin_          = 0;
    uint32_t calMax_          = 0;
    uint64_t calSum_          = 0;
    uint32_t calCount_        = 0;

    PresenceEvent       pendingEvent_ = PresenceEvent::None;
    PresenceCalibration lastCal_;

    uint32_t readRaw();
    void     applyThreshold(uint32_t thr);
    void     updateCalibration(uint32_t now);
    void     finishCalibration();
    void     saveThresholdToNvs(uint32_t thr);
    uint32_t loadThresholdFromNvs();
};

} // namespace vfm
