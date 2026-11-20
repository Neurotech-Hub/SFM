#include "PresenceService.h"
#include <math.h>
#include <string.h>

namespace sfm {

namespace {

void sortDoubles(double *v, uint8_t n) {
    for (uint8_t i = 1; i < n; i++) {
        double key = v[i];
        int j = (int)i - 1;
        while (j >= 0 && v[j] > key) {
            v[j + 1] = v[j];
            j--;
        }
        v[j + 1] = key;
    }
}

void sortU16(uint16_t *v, uint8_t n) {
    for (uint8_t i = 1; i < n; i++) {
        uint16_t key = v[i];
        int j = (int)i - 1;
        while (j >= 0 && v[j] > key) {
            v[j + 1] = v[j];
            j--;
        }
        v[j + 1] = key;
    }
}

} // namespace

// ---------------------------------------------------------------------------
ServiceStatus PresenceService::begin() {
    pinMode(PIN_PRESENCE, INPUT);
    loadFromNvs();

    raw_          = readRaw();
    present_      = false;
    // A not-ready touch peripheral reads near zero. Don't let that become
    // the EMA start, or the boot median is pulled low and presence latches on.
    if (raw_ >= 1000) {
        smooth_     = static_cast<double>(raw_);
        smoothInit_ = true;
        pushMedian(raw_);
    }
    rawPresent_   = false;
    seeded_       = false;
    seedCount_    = 0;
    seedStartMs_  = 0;
    lastSampleMs_ = millis();
    lastChangeMs_ = lastSampleMs_;
    recomputeOnDelta();
    return ServiceStatus::Ok;
}

// ---------------------------------------------------------------------------
void PresenceService::update(bool domeOpen) {
    const uint32_t now = millis();
    if ((now - lastSampleMs_) < kPresenceSampleMs) return;
    lastSampleMs_ = now;

    raw_ = readRaw();
    pushMedian(raw_);

    double med = 0.0;
    {
        uint32_t tmp[kPresenceMedianN];
        const uint8_t n = medianCount_;
        for (uint8_t i = 0; i < n; i++) tmp[i] = medianRing_[i];
        for (uint8_t i = 1; i < n; i++) {
            uint32_t key = tmp[i];
            int j = (int)i - 1;
            while (j >= 0 && tmp[j] > key) {
                tmp[j + 1] = tmp[j];
                j--;
            }
            tmp[j + 1] = key;
        }
        med = static_cast<double>(tmp[n / 2]);
    }

    // A dead or shorted pad reads near zero. Hold the last good smooth value
    // so that glitch cannot drag the baseline down and latch false presence.
    if (med < 1000.0) {
        noteDomeTimer(domeOpen, now);
        return;
    }

    if (!smoothInit_) {
        smooth_ = med;
        smoothInit_ = true;
    } else {
        smooth_ = static_cast<double>(kPresenceSmoothAlpha) * med
                + (1.0 - static_cast<double>(kPresenceSmoothAlpha)) * smooth_;
    }

    if (!seeded_) {
        noteDomeTimer(domeOpen, now);
        if (seedCount_ < kSeedCap) seedBuf_[seedCount_++] = smooth_;
        if (seedStartMs_ == 0) seedStartMs_ = now;
        if ((now - seedStartMs_) >= kPresenceSeedMs && seedCount_ >= kPresenceCalMinSamples) {
            finishSeed(now);
        }
        return;
    }

    pushDelta(smooth_ - baseline_);
    recomputeOnDelta();
    updateDecision(now);
    noteDome(domeOpen, now);
    if (calibrating_) serviceCalibration(now);
    trackBaseline(now);
    updateStuck(now);

    const double d = smooth_ - baseline_;
    if (!traceHave_) {
        traceMin_ = d;
        traceMax_ = d;
        traceHave_ = true;
    } else {
        if (d < traceMin_) traceMin_ = d;
        if (d > traceMax_) traceMax_ = d;
    }
}

// ---------------------------------------------------------------------------
bool PresenceService::setFactor(float factorPercent) {
    if (!isfinite(factorPercent) || factorPercent <= 0.0f) return false;
    if (factorPercent < kMinPresenceFactor) factorPercent = kMinPresenceFactor;
    if (factorPercent > kMaxPresenceFactor) factorPercent = kMaxPresenceFactor;
    factor_ = factorPercent;
    saveFactorToNvs();
    recomputeOnDelta();
    if (seeded_) applyBoundary(millis());
    updateSensitivity();
    return true;
}

void PresenceService::setTraceEnabled(bool enabled) {
    traceEnabled_ = enabled;
    traceHave_    = false;
}

void PresenceService::clearStoredFactor() {
    factor_ = kDefaultPresenceFactor;
    saveFactorToNvs();
    recomputeOnDelta();
    if (seeded_) applyBoundary(millis());
    updateSensitivity();
}

// ---------------------------------------------------------------------------
bool PresenceService::startCalibration() {
    if (calibrating_) return false;
    calibrating_      = true;
    calWaiting_       = true;
    calStartMs_       = 0;
    calWindowStartMs_ = 0;
    calWindowCount_   = 0;
    calBestMedian_    = 0.0;
    calBestSigma_     = 0.0;
    calBestSamples_   = 0;
    calHaveWindow_    = false;
    pendingEvent_     = PresenceEvent::CalibrationStarted;
    return true;
}

void PresenceService::cancelCalibration() {
    calibrating_ = false;
    calWaiting_  = false;
}

PresenceEvent PresenceService::takeEvent() {
    PresenceEvent ev = pendingEvent_;
    pendingEvent_ = PresenceEvent::None;
    return ev;
}

bool PresenceService::takeStuck(PresenceStuckReport &out) {
    if (!stuckPending_) return false;
    out = stuckReport_;
    stuckPending_ = false;
    return true;
}

bool PresenceService::takeDomeBout(DomeBoutReport &out) {
    if (!boutPending_) return false;
    out = boutReport_;
    boutPending_ = false;
    return true;
}

PresenceTraceSample PresenceService::takeTrace() {
    PresenceTraceSample s;
    uint32_t b = baseline_ > 0.0 ? static_cast<uint32_t>(baseline_ + 0.5) : 0;
    if (b > 0xFFFFFFu) b = 0xFFFFFFu;
    s.baseline = b;
    s.maxDelta = clampI16(traceHave_ ? traceMax_ : (smooth_ - baseline_));
    s.minDelta = clampI16(traceHave_ ? traceMin_ : (smooth_ - baseline_));
    s.flags = 0;
    if (present_) s.flags |= kTraceFlagPresent;
    if (domeOpen_) s.flags |= kTraceFlagDome;
    if (tracking_) s.flags |= kTraceFlagTracking;
    if (calibrating_) s.flags |= kTraceFlagCalibrating;
    if (stuckReported_ && present_ && !domeOpen_) s.flags |= kTraceFlagStuck;
    traceHave_ = false;
    return s;
}

uint32_t PresenceService::threshold() const {
    if (!seeded_ || baseline_ <= 0.0) return 0;
    double level = baseline_ + onDelta_;
    if (level < 1.0) level = 1.0;
    if (level > static_cast<double>(UINT32_MAX)) level = static_cast<double>(UINT32_MAX);
    return static_cast<uint32_t>(level + 0.5);
}

// ---------------------------------------------------------------------------
// Private
// ---------------------------------------------------------------------------

uint32_t PresenceService::readRaw() {
    return touchRead(PIN_PRESENCE);
}

void PresenceService::loadFromNvs() {
    prefs_.begin(kNvsNamespace, true);
    const bool hasPct = prefs_.isKey(kNvsKeyPresencePct);
    float stored = hasPct ? prefs_.getFloat(kNvsKeyPresencePct, kDefaultPresenceFactor) : 0.0f;
    prefs_.end();

    if (hasPct && isfinite(stored) && stored >= kMinPresenceFactor && stored <= kMaxPresenceFactor) {
        factor_ = stored;
        return;
    }
    factor_ = kDefaultPresenceFactor;
    migrateNvs();
}

void PresenceService::migrateNvs() {
    prefs_.begin(kNvsNamespace, false);
    prefs_.remove(kNvsKeyPresenceThr);
    prefs_.remove(kNvsKeyPresenceFactor);
    prefs_.remove(kNvsKeyPresenceMean);
    prefs_.remove(kNvsKeyPresenceStd);
    prefs_.putFloat(kNvsKeyPresencePct, factor_);
    prefs_.end();
}

void PresenceService::saveFactorToNvs() {
    prefs_.begin(kNvsNamespace, false);
    prefs_.putFloat(kNvsKeyPresencePct, factor_);
    prefs_.end();
}

void PresenceService::recomputeOnDelta() {
    if (baseline_ > 1.0) {
        onDelta_ = static_cast<double>(factor_) / 100.0 * baseline_;
    } else {
        onDelta_ = 1.0e12;
    }
}

void PresenceService::applyBoundary(uint32_t now) {
    const double delta = smooth_ - baseline_;
    bool next = rawPresent_;
    if (delta > onDelta_) next = true;
    else if (delta < static_cast<double>(kPresenceOffRatio) * onDelta_) next = false;
    rawPresent_   = next;
    present_      = next;
    lastChangeMs_ = now;
}

void PresenceService::pushMedian(uint32_t sample) {
    medianRing_[medianIdx_] = sample;
    medianIdx_ = static_cast<uint8_t>((medianIdx_ + 1) % kPresenceMedianN);
    if (medianCount_ < kPresenceMedianN) medianCount_++;
}

double PresenceService::medianOf(const double *values, uint8_t n) const {
    double tmp[kSeedCap];
    if (n > kSeedCap) n = kSeedCap;
    memcpy(tmp, values, n * sizeof(double));
    sortDoubles(tmp, n);
    return tmp[n / 2];
}

double PresenceService::sigmaOf(const double *values, uint8_t n, double center) const {
    if (n < 2) return 1.0;
    double acc = 0.0;
    for (uint8_t i = 0; i < n; i++) {
        double d = values[i] - center;
        acc += d * d;
    }
    double v = acc / static_cast<double>(n);
    if (v < 1.0) v = 1.0;
    return sqrt(v);
}

void PresenceService::pushDelta(double delta) {
    deltaHist_[deltaHistIdx_] = delta;
    deltaHistIdx_ = static_cast<uint8_t>((deltaHistIdx_ + 1) % kDeltaHistN);
    if (deltaHistCount_ < kDeltaHistN) deltaHistCount_++;
}

double PresenceService::lookbackDelta(uint8_t back) const {
    if (deltaHistCount_ == 0) return smooth_ - baseline_;
    if (back >= deltaHistCount_) back = static_cast<uint8_t>(deltaHistCount_ - 1);
    int i = static_cast<int>(deltaHistIdx_) - 1 - static_cast<int>(back);
    while (i < 0) i += kDeltaHistN;
    return deltaHist_[i];
}

double PresenceService::minRecentDelta(uint8_t n) const {
    uint8_t use = n;
    if (use > deltaHistCount_) use = deltaHistCount_;
    if (use == 0) return smooth_ - baseline_;
    double m = lookbackDelta(0);
    for (uint8_t i = 1; i < use; i++) {
        double v = lookbackDelta(i);
        if (v < m) m = v;
    }
    return m;
}

uint16_t PresenceService::pctX100(double deltaCounts) const {
    if (baseline_ < 1.0 || deltaCounts <= 0.0) return 0;
    double v = deltaCounts / baseline_ * 10000.0;
    if (v > 65535.0) v = 65535.0;
    return static_cast<uint16_t>(v + 0.5);
}

int16_t PresenceService::pctX100Signed(double deltaCounts) const {
    if (baseline_ < 1.0) return 0;
    return clampI16(deltaCounts / baseline_ * 10000.0);
}

int16_t PresenceService::clampI16(double v) {
    if (v > 32767.0) return 32767;
    if (v < -32768.0) return -32768;
    if (v >= 0.0) return static_cast<int16_t>(v + 0.5);
    return static_cast<int16_t>(v - 0.5);
}

void PresenceService::finishSeed(uint32_t now) {
    baseline_ = medianOf(seedBuf_, seedCount_);
    noiseSigma_ = static_cast<float>(sigmaOf(seedBuf_, seedCount_, baseline_));
    noiseVar_ = static_cast<double>(noiseSigma_) * static_cast<double>(noiseSigma_);
    seeded_ = true;
    lastTrackMs_ = now;
    recomputeOnDelta();
    applyBoundary(now);
}

void PresenceService::noteDomeTimer(bool domeOpen, uint32_t now) {
    if (domeOpen) {
        domeClosedSinceMs_ = 0;
        domeClosedSettled_ = false;
        return;
    }
    if (domeClosedSinceMs_ == 0) domeClosedSinceMs_ = now;
    domeClosedSettled_ = (now - domeClosedSinceMs_) >= kDomeBaselineHoldMs;
}

void PresenceService::noteDome(bool domeOpen, uint32_t now) {
    noteDomeTimer(domeOpen, now);

    if (domeOpen && !domeOpen_) {
        preliftDelta_      = lookbackDelta(kPreliftBack);
        boutStartMs_       = now;
        boutTouch_         = false;
        boutPeakSustained_ = 0.0;
        boutOpen_          = true;
    }

    if (domeOpen && boutOpen_) {
        const double sustained = minRecentDelta(kSustainN);
        if (sustained > boutPeakSustained_) boutPeakSustained_ = sustained;
        if (present_ || sustained > onDelta_) boutTouch_ = true;
    }

    if (!domeOpen && domeOpen_ && boutOpen_) {
        const uint32_t elapsed = now - boutStartMs_;
        uint32_t steps = elapsed / 100u;
        if (steps > 65535u) steps = 65535u;
        const bool touch = boutTouch_ || boutPeakSustained_ > onDelta_;
        boutReport_.touchSeen      = touch;
        boutReport_.peakPctX100    = pctX100(boutPeakSustained_);
        boutReport_.preliftPctX100 = pctX100Signed(preliftDelta_);
        boutReport_.duration100ms  = static_cast<uint16_t>(steps);
        boutPending_ = true;
        if (touch) recordBoutPeak(boutReport_.peakPctX100);
        boutOpen_ = false;
    }

    domeOpen_ = domeOpen;
}

void PresenceService::updateDecision(uint32_t now) {
    const double delta = smooth_ - baseline_;
    const double off = static_cast<double>(kPresenceOffRatio) * onDelta_;
    bool candidate = rawPresent_;
    if (delta > onDelta_) candidate = true;
    else if (delta < off) candidate = false;

    if (candidate != rawPresent_) {
        rawPresent_   = candidate;
        lastChangeMs_ = now;
    }
    if ((now - lastChangeMs_) >= kSensorDebounceMs) {
        present_ = rawPresent_;
    }
}

void PresenceService::trackBaseline(uint32_t now) {
    tracking_ = false;

    double dt = 20.0;
    if (lastTrackMs_ != 0) {
        dt = static_cast<double>(now - lastTrackMs_);
        if (dt < 0.0) dt = 0.0;
        if (dt > 250.0) dt = 250.0;
    }
    lastTrackMs_ = now;

    const double delta = smooth_ - baseline_;
    const bool inClearBand = delta < static_cast<double>(kPresenceOffRatio) * onDelta_;
    if (present_ || !inClearBand || !domeClosedSettled_ || smooth_ < 1000.0) return;

    const double tau = (smooth_ < baseline_)
        ? static_cast<double>(kBaselineTauDownMs)
        : static_cast<double>(kBaselineTauUpMs);
    const double a = 1.0 - exp(-dt / tau);
    baseline_ += (smooth_ - baseline_) * a;
    recomputeOnDelta();

    const double resid = smooth_ - baseline_;
    const double noiseA = 1.0 - exp(-dt / 30000.0);
    noiseVar_ = noiseVar_ * (1.0 - noiseA) + resid * resid * noiseA;
    if (noiseVar_ < 1.0) noiseVar_ = 1.0;
    noiseSigma_ = static_cast<float>(sqrt(noiseVar_));
    tracking_ = true;
}

void PresenceService::updateStuck(uint32_t now) {
    if (!(present_ && !domeOpen_)) {
        stuckStartMs_      = 0;
        lastStuckSampleMs_ = 0;
        stuckCount_        = 0;
        stuckIdx_          = 0;
        stuckReported_     = false;
        return;
    }

    if (stuckStartMs_ == 0) stuckStartMs_ = now;
    if (lastStuckSampleMs_ == 0 || (now - lastStuckSampleMs_) >= 1000u) {
        lastStuckSampleMs_ = now;
        stuckRing_[stuckIdx_] = smooth_;
        stuckIdx_ = static_cast<uint8_t>((stuckIdx_ + 1) % kStuckRingN);
        if (stuckCount_ < kStuckRingN) stuckCount_++;
    }

    if (stuckReported_) return;
    if (stuckCount_ < kStuckRingN) return;
    if ((now - stuckStartMs_) < kPresenceStuckMs) return;

    double mn = stuckRing_[0];
    double mx = stuckRing_[0];
    for (uint8_t i = 1; i < kStuckRingN; i++) {
        if (stuckRing_[i] < mn) mn = stuckRing_[i];
        if (stuckRing_[i] > mx) mx = stuckRing_[i];
    }
    const double range = mx - mn;
    const double limit = static_cast<double>(kPresenceStuckSigmaMul) * static_cast<double>(noiseSigma_);
    if (range >= limit) return;

    stuckReport_.deltaPctX100 = pctX100(smooth_ - baseline_);
    double rangeWire = range;
    if (rangeWire < 0.0) rangeWire = 0.0;
    if (rangeWire > 65535.0) rangeWire = 65535.0;
    stuckReport_.rangeCounts = static_cast<uint16_t>(rangeWire + 0.5);
    stuckPending_ = true;
    stuckReported_ = true;

    if (kPresenceStuckActive) {
        baseline_ = smooth_;
        recomputeOnDelta();
        applyBoundary(now);
    }
}

void PresenceService::serviceCalibration(uint32_t now) {
    if (calWaiting_) {
        if (!domeClosedSettled_) return;
        calWaiting_       = false;
        calStartMs_       = now;
        calWindowStartMs_ = now;
        calWindowCount_   = 0;
    }

    if (calWindowCount_ < kCalWindowCap) {
        calWindow_[calWindowCount_++] = smooth_;
    }

    if ((now - calWindowStartMs_) >= kPresenceCalWindowMs
        && calWindowCount_ >= kPresenceCalMinSamples) {
        considerCalWindow();
        calWindowCount_   = 0;
        calWindowStartMs_ = now;
    }

    if ((now - calStartMs_) >= kPresenceCalMs) {
        if (calWindowCount_ >= kPresenceCalMinSamples) considerCalWindow();
        finishCalibration();
    }
}

void PresenceService::considerCalWindow() {
    const double med = medianOf(calWindow_, calWindowCount_);
    const double sig = sigmaOf(calWindow_, calWindowCount_, med);
    if (!calHaveWindow_ || med < calBestMedian_) {
        calBestMedian_  = med;
        calBestSigma_   = sig;
        calBestSamples_ = calWindowCount_;
        calHaveWindow_  = true;
    }
}

void PresenceService::finishCalibration() {
    calibrating_ = false;
    calWaiting_  = false;

    lastCal_.samples = calBestSamples_;
    lastCal_.factor  = factor_;

    if (!calHaveWindow_) {
        lastCal_.ok        = false;
        lastCal_.mean      = static_cast<float>(baseline_);
        lastCal_.stdDev    = noiseSigma_;
        lastCal_.threshold = threshold();
        pendingEvent_      = PresenceEvent::CalibrationFailed;
        return;
    }

    baseline_    = calBestMedian_;
    noiseSigma_  = static_cast<float>(calBestSigma_);
    noiseVar_    = calBestSigma_ * calBestSigma_;
    if (noiseVar_ < 1.0) noiseVar_ = 1.0;
    recomputeOnDelta();
    applyBoundary(millis());

    lastCal_.ok        = true;
    lastCal_.mean      = static_cast<float>(baseline_);
    lastCal_.stdDev    = noiseSigma_;
    lastCal_.threshold = threshold();
    pendingEvent_      = PresenceEvent::CalibrationDone;
}

void PresenceService::recordBoutPeak(uint16_t peakPct) {
    boutPeaks_[boutPeakIdx_] = peakPct;
    boutPeakIdx_ = static_cast<uint8_t>((boutPeakIdx_ + 1) % kDomeBoutPeakKeep);
    if (boutPeakCount_ < kDomeBoutPeakKeep) boutPeakCount_++;
    updateSensitivity();
}

void PresenceService::updateSensitivity() {
    if (boutPeakCount_ < kSensitivityMinBouts) {
        sensitivityWarning_ = false;
        return;
    }
    uint16_t tmp[kDomeBoutPeakKeep];
    const uint8_t n = boutPeakCount_;
    if (n < kDomeBoutPeakKeep) {
        memcpy(tmp, boutPeaks_, n * sizeof(uint16_t));
    } else {
        memcpy(tmp, boutPeaks_, kDomeBoutPeakKeep * sizeof(uint16_t));
    }
    sortU16(tmp, n);
    uint8_t idx = static_cast<uint8_t>((5u * n + 99u) / 100u);
    if (idx == 0) idx = 1;
    idx = static_cast<uint8_t>(idx - 1);
    if (idx >= n) idx = static_cast<uint8_t>(n - 1);
    const double p5Percent = static_cast<double>(tmp[idx]) / 100.0;
    sensitivityWarning_ = static_cast<double>(factor_) > 0.5 * p5Percent;
}

} // namespace sfm
