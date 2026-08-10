#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include "ServiceTypes.h"
#include "../hardware/VFMPins.h"

namespace vfm {

// NVS keys (namespace kNvsNamespace)
constexpr char kNvsKeyPresenceThr[]    = "presThr";
constexpr char kNvsKeyPresenceFactor[] = "presFac";
constexpr char kNvsKeyPresenceMean[]   = "presMean";
constexpr char kNvsKeyPresenceStd[]    = "presStd";

// Fallback used until a calibration has been stored in NVS.
// Bench readings: idle ≈ 35 000–35 500, animal present ≈ 36 000 and up.
constexpr uint32_t kDefaultPresenceThreshold = 35000;

// Calibration samples the idle pad (Welford online mean / variance), then sets
//   threshold = mean + factor * std_dev
// Factor default 3.0; pad must stay clear for the whole capture.
constexpr float    kDefaultPresenceFactor = 3.0f;
constexpr float    kMinPresenceFactor     = 0.1f;
constexpr float    kMaxPresenceFactor     = 100.0f;
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
    float    mean      = 0.0f;
    float    stdDev    = 0.0f;
    float    factor    = kDefaultPresenceFactor;
    uint32_t threshold = 0;  // threshold in force after the attempt
    bool     ok        = false;
};

// ---------------------------------------------------------------------------
// PresenceService
//
// Mouse presence detection on the capacitive pad (PIN_PRESENCE, touchRead).
// Raw counts rise when a mouse is present, so presence = raw > threshold.
//
// The threshold is calibrated against the idle pad:
//   thr = mean + factor * std_dev
// Threshold, factor, and last-cal mean/σ are persisted to NVS so a node keeps
// its calibration across reboots. Changing the factor re-applies from the
// last cal stats when they are available.
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
    float    factor() const      { return factor_; }
    bool     calibrating() const { return calibrating_; }
    bool     hasCalStats() const { return calValid_; }
    float    calMean() const     { return calMean_; }
    float    calStdDev() const   { return calStdDev_; }

    // Clamp + store the calibration multiplier. If last-cal mean/σ are known,
    // recomputes and saves the threshold immediately. Returns false if the
    // value was rejected (non-finite / non-positive).
    bool setFactor(float factor);

    // Forget stored threshold / factor / cal stats; fall back to defaults.
    void clearStoredThreshold();

    // Begin a capture of the idle pad. Returns false if one is already running.
    bool startCalibration();
    void cancelCalibration();

    PresenceEvent takeEvent();
    const PresenceCalibration &lastCalibration() const { return lastCal_; }

private:
    Preferences prefs_;

    uint32_t threshold_ = kDefaultPresenceThreshold;
    float    factor_    = kDefaultPresenceFactor;
    uint32_t raw_       = 0;

    bool     present_      = false;  // debounced
    bool     rawPresent_   = false;  // undebounced candidate
    uint32_t lastChangeMs_ = 0;      // when rawPresent_ last flipped
    uint32_t lastSampleMs_ = 0;

    // Last successful calibration stats (also restored from NVS)
    bool  calValid_  = false;
    float calMean_   = 0.0f;
    float calStdDev_ = 0.0f;

    bool     calibrating_     = false;
    uint32_t calStartMs_      = 0;
    uint32_t calLastSampleMs_ = 0;
    uint32_t calCount_        = 0;
    double   calMeanAcc_      = 0.0;  // Welford running mean
    double   calM2Acc_        = 0.0;  // Welford sum of squares of diffs

    PresenceEvent       pendingEvent_ = PresenceEvent::None;
    PresenceCalibration lastCal_;

    uint32_t readRaw();
    void     applyThreshold(uint32_t thr);
    uint32_t thresholdFromStats(float mean, float stdDev, float factor) const;
    void     updateCalibration(uint32_t now);
    void     finishCalibration();
    void     saveThresholdToNvs(uint32_t thr);
    void     saveFactorToNvs(float factor);
    void     saveCalStatsToNvs(float mean, float stdDev);
    void     loadFromNvs();
};

} // namespace vfm
