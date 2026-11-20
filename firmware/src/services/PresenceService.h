#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include "ServiceTypes.h"
#include "../hardware/SFMPins.h"

namespace sfm {

// NVS keys (namespace kNvsNamespace).
// presPct is the percent rise above the live baseline that counts as presence.
// The 1.6/1.7 keys (presThr, presFac, presMean, presStd) are deleted on the
// first boot that does not already have presPct, so an old sigma-multiple is
// never read as a percent.
constexpr char kNvsKeyPresencePct[]    = "presPct";
constexpr char kNvsKeyPresenceThr[]    = "presThr";
constexpr char kNvsKeyPresenceFactor[] = "presFac";
constexpr char kNvsKeyPresenceMean[]   = "presMean";
constexpr char kNvsKeyPresenceStd[]    = "presStd";

// Factor is a percent of the live baseline: onDelta = factor/100 * baseline.
// Default is a placeholder until a 24 h run measures noise vs real visits.
constexpr float    kDefaultPresenceFactor = 1.0f;
constexpr float    kMinPresenceFactor     = 0.2f;
constexpr float    kMaxPresenceFactor     = 10.0f;

// touchRead() cadence. Several samples per debounce window, without polling
// the touch peripheral on every loop pass.
constexpr uint32_t kPresenceSampleMs = 20;

// Median of this many raw samples, then an EMA, before any decision.
constexpr uint8_t  kPresenceMedianN     = 5;
constexpr float    kPresenceSmoothAlpha = 0.3f;

// Baseline follows drift only while the pad reads clear. Up is slow so a
// visit cannot be absorbed; down is fast because a falling reading is drift.
constexpr uint32_t kBaselineTauUpMs   = 60000;
constexpr uint32_t kBaselineTauDownMs = 5000;
constexpr float    kPresenceOffRatio  = 0.5f;

// Boot seed: median of the smoothed samples from the first second. A stored
// baseline is not used — a too-low seed latches false presence, and the
// tracker will not move while it believes the pad is occupied.
constexpr uint32_t kPresenceSeedMs = 1000;

// Re-baseline window. Runs alongside normal detection. The baseline is the
// lowest 1 s median in the window; sigma is the spread of that same second.
constexpr uint32_t kPresenceCalMs         = 20000;
constexpr uint32_t kPresenceCalWindowMs   = 1000;
constexpr uint8_t  kPresenceCalMinSamples = 10;

// Baseline tracking and a calibration start both wait until the dome has
// been closed this long, so a visit cannot be learned as the new idle.
constexpr uint32_t kDomeBaselineHoldMs = 2000;

// Stuck-present shadow guard. After this long of presence with the dome
// closed, a smoothed range below 3 * idle sigma emits PresenceStuckCandidate.
// kPresenceStuckActive false: the event is only a candidate, the baseline
// is not moved. Mice rest on the pad, so an automatic rebaseline would end
// a real visit and release experiments that wait on presence-clear.
constexpr uint32_t kPresenceStuckMs       = 15UL * 60UL * 1000UL;
constexpr uint32_t kPresenceStuckWindowMs = 60000;
constexpr float    kPresenceStuckSigmaMul = 3.0f;
constexpr bool     kPresenceStuckActive   = false;

// Opt-in debug trace period (CAN id 0x400 + nodeId). Not persisted.
constexpr uint32_t kPresenceTraceMs = 1000;

// Trace flag bits (byte 7 of the trace frame).
constexpr uint8_t kTraceFlagPresent     = 0x01;
constexpr uint8_t kTraceFlagDome        = 0x02;
constexpr uint8_t kTraceFlagTracking    = 0x04;
constexpr uint8_t kTraceFlagCalibrating = 0x08;
constexpr uint8_t kTraceFlagStuck       = 0x10;

// Extra heartbeat sensorBits (byte 4). Bits 0-2 stay pellet / load / dome.
constexpr uint8_t kHbBitSensitivityWarn = 0x08;
constexpr uint8_t kHbBitBaselineFrozen  = 0x10;

// How many touch-seen dome bouts are required before the sensitivity warning
// compares onDelta with the 5th percentile of their peaks.
constexpr uint8_t kSensitivityMinBouts = 20;
constexpr uint8_t kDomeBoutPeakKeep    = 64;

// Latched one-shot notifications for the calibration capture. Read with
// takeEvent(). Stuck candidates and dome-bout summaries use their own takes
// so they cannot overwrite a calibration result.
enum class PresenceEvent : uint8_t {
    None = 0,
    CalibrationStarted,
    CalibrationDone,
    CalibrationFailed,
};

struct PresenceCalibration {
    uint32_t samples   = 0;     // samples in the quietest 1 s window
    float    mean      = 0.0f;  // that window's median (the new baseline)
    float    stdDev    = 0.0f;  // sigma of the smoothed samples in that window
    float    factor    = kDefaultPresenceFactor;
    uint32_t threshold = 0;     // effective on-level after the attempt
    bool     ok        = false;
};

// One second of the opt-in trace. baseline is counts (24-bit). Deltas are
// smoothed counts relative to the baseline over that second, clamped to int16.
struct PresenceTraceSample {
    uint32_t baseline = 0;
    int16_t  maxDelta = 0;
    int16_t  minDelta = 0;
    uint8_t  flags    = 0;
};

// PresenceStuckCandidate payload. deltaPctX100 is percent above baseline
// times 100 (1.50% -> 150). rangeCounts is the smoothed range over 60 s.
struct PresenceStuckReport {
    uint16_t deltaPctX100 = 0;
    uint16_t rangeCounts  = 0;
};

// DomeBoutSummary payload. Percents are percent-of-baseline times 100.
// duration100ms is the bout length in 100 ms steps, capped at 65535.
struct DomeBoutReport {
    bool     touchSeen      = false;
    uint16_t peakPctX100    = 0;
    int16_t  preliftPctX100 = 0;
    uint16_t duration100ms  = 0;
};

// ---------------------------------------------------------------------------
// PresenceService
//
// Mouse presence on the capacitive pad (PIN_PRESENCE, touchRead). Raw counts
// rise when a mouse is present.
//
//   smooth  = EMA(median of the last 5 raw samples)
//   onDelta = factor/100 * baseline
//   present once (smooth - baseline) > onDelta has held for kSensorDebounceMs
//   clear   once (smooth - baseline) < 0.5 * onDelta has held for the same
//
// The baseline tracks drift only while the pad reads clear, the delta is
// below the clear level, and the dome has been closed for kDomeBaselineHoldMs.
// Reported presence stays a pure touch measurement. The dome only gates
// tracking and supplies the per-bout summary used to tune the factor.
// ---------------------------------------------------------------------------
class PresenceService {
public:
    PresenceService() = default;

    ServiceStatus begin();

    // Non-blocking; call every loop pass. domeOpen is the debounced PG3
    // state from DispenserService (already updated this pass).
    void update(bool domeOpen);

    // --- Debounced state ---
    bool     present() const           { return present_; }
    uint32_t raw() const               { return raw_; }
    double   smooth() const            { return smooth_; }
    double   baseline() const          { return baseline_; }
    double   delta() const             { return smooth_ - baseline_; }
    double   onDelta() const           { return onDelta_; }
    float    noiseSigma() const        { return noiseSigma_; }
    // Effective on-level, baseline + onDelta, for logs and the serial print.
    uint32_t threshold() const;
    float    factor() const            { return factor_; }
    bool     calibrating() const       { return calibrating_; }
    bool     seeded() const            { return seeded_; }
    bool     tracking() const          { return tracking_; }
    bool     baselineFrozen() const    { return !tracking_; }
    bool     sensitivityWarning() const { return sensitivityWarning_; }
    bool     traceEnabled() const      { return traceEnabled_; }

    // Clamp + store the percent. Returns false if the value was rejected
    // (non-finite / non-positive). The decision boundary updates immediately.
    bool setFactor(float factorPercent);

    // Runtime only. Not written to NVS. Resets the trace min/max window.
    void setTraceEnabled(bool enabled);

    // Forget the stored percent and return to the compile-time default.
    // The live baseline is left alone.
    void clearStoredFactor();

    // Begin a re-baseline. Returns false if one is already running.
    // Detection keeps running through the window.
    bool startCalibration();
    void cancelCalibration();

    PresenceEvent takeEvent();
    const PresenceCalibration &lastCalibration() const { return lastCal_; }

    // Latest stuck-candidate / dome-bout report, or false if none is waiting.
    bool takeStuck(PresenceStuckReport &out);
    bool takeDomeBout(DomeBoutReport &out);

    // Min/max smoothed delta since the previous call, then the window resets.
    PresenceTraceSample takeTrace();

private:
    static constexpr uint8_t kSeedCap      = 64;
    static constexpr uint8_t kCalWindowCap = 64;
    static constexpr uint8_t kStuckRingN   = 60;
    static constexpr uint8_t kDeltaHistN   = 16;
    static constexpr uint8_t kSustainN     = 5;
    static constexpr uint8_t kPreliftBack  = 8;

    Preferences prefs_;

    float    factor_    = kDefaultPresenceFactor;
    double   onDelta_   = 0.0;
    uint32_t raw_       = 0;
    double   smooth_    = 0.0;
    double   baseline_  = 0.0;
    bool     smoothInit_ = false;

    uint32_t medianRing_[kPresenceMedianN] = {};
    uint8_t  medianCount_ = 0;
    uint8_t  medianIdx_   = 0;

    bool     present_      = false;
    bool     rawPresent_   = false;
    uint32_t lastChangeMs_ = 0;
    uint32_t lastSampleMs_ = 0;
    uint32_t lastTrackMs_  = 0;
    bool     tracking_     = false;

    bool   seeded_     = false;
    double seedBuf_[kSeedCap] = {};
    uint8_t seedCount_ = 0;
    uint32_t seedStartMs_ = 0;

    float  noiseSigma_ = 1.0f;
    double noiseVar_   = 1.0;

    bool     domeOpen_          = false;
    uint32_t domeClosedSinceMs_ = 0;
    bool     domeClosedSettled_ = false;

    double  deltaHist_[kDeltaHistN] = {};
    uint8_t deltaHistCount_ = 0;
    uint8_t deltaHistIdx_   = 0;

    bool     boutOpen_          = false;
    uint32_t boutStartMs_       = 0;
    bool     boutTouch_         = false;
    double   boutPeakSustained_ = 0.0;
    double   preliftDelta_      = 0.0;

    uint16_t boutPeaks_[kDomeBoutPeakKeep] = {};
    uint8_t  boutPeakCount_ = 0;
    uint8_t  boutPeakIdx_   = 0;
    bool     sensitivityWarning_ = false;

    bool     calibrating_   = false;
    bool     calWaiting_    = false;
    uint32_t calStartMs_    = 0;
    uint32_t calWindowStartMs_ = 0;
    double   calWindow_[kCalWindowCap] = {};
    uint8_t  calWindowCount_ = 0;
    double   calBestMedian_  = 0.0;
    double   calBestSigma_   = 0.0;
    uint32_t calBestSamples_ = 0;
    bool     calHaveWindow_  = false;

    uint32_t stuckStartMs_      = 0;
    uint32_t lastStuckSampleMs_ = 0;
    double   stuckRing_[kStuckRingN] = {};
    uint8_t  stuckCount_   = 0;
    uint8_t  stuckIdx_     = 0;
    bool     stuckReported_ = false;

    bool    traceEnabled_ = false;
    bool    traceHave_    = false;
    double  traceMin_     = 0.0;
    double  traceMax_     = 0.0;

    PresenceEvent       pendingEvent_ = PresenceEvent::None;
    PresenceCalibration lastCal_;
    bool                stuckPending_ = false;
    PresenceStuckReport stuckReport_;
    bool                boutPending_ = false;
    DomeBoutReport      boutReport_;

    uint32_t readRaw();
    void     loadFromNvs();
    void     saveFactorToNvs();
    void     migrateNvs();
    void     recomputeOnDelta();
    void     applyBoundary(uint32_t now);
    void     pushMedian(uint32_t sample);
    double   medianOf(const double *values, uint8_t n) const;
    double   sigmaOf(const double *values, uint8_t n, double center) const;
    void     pushDelta(double delta);
    double   lookbackDelta(uint8_t back) const;
    double   minRecentDelta(uint8_t n) const;
    uint16_t pctX100(double deltaCounts) const;
    int16_t  pctX100Signed(double deltaCounts) const;
    void     finishSeed(uint32_t now);
    void     noteDome(bool domeOpen, uint32_t now);
    void     noteDomeTimer(bool domeOpen, uint32_t now);
    void     updateDecision(uint32_t now);
    void     trackBaseline(uint32_t now);
    void     updateStuck(uint32_t now);
    void     serviceCalibration(uint32_t now);
    void     considerCalWindow();
    void     finishCalibration();
    void     recordBoutPeak(uint16_t pctX100);
    void     updateSensitivity();
    static int16_t clampI16(double v);
};

} // namespace sfm
