#include "PresenceService.h"
#include <math.h>

namespace sfm {

// ---------------------------------------------------------------------------
ServiceStatus PresenceService::begin() {
    pinMode(PIN_PRESENCE, INPUT);

    loadFromNvs();

    // Seed from a real reading so the startup level does not look like an edge.
    uint32_t now   = millis();
    raw_           = readRaw();
    rawPresent_    = (raw_ > threshold_);
    present_       = rawPresent_;
    lastChangeMs_  = now;
    lastSampleMs_  = now;

    return ServiceStatus::Ok;
}

// ---------------------------------------------------------------------------
void PresenceService::update() {
    const uint32_t now = millis();

    if (calibrating_) {
        updateCalibration(now);
        return;
    }

    if ((now - lastSampleMs_) < kPresenceSampleMs) return;
    lastSampleMs_ = now;

    raw_ = readRaw();

    bool candidate = (raw_ > threshold_);
    if (candidate != rawPresent_) {
        rawPresent_   = candidate;
        lastChangeMs_ = now;
    }
    if ((now - lastChangeMs_) >= kSensorDebounceMs) {
        present_ = rawPresent_;
    }
}

// ---------------------------------------------------------------------------
bool PresenceService::setFactor(float factor) {
    if (!isfinite(factor) || factor <= 0.0f) return false;
    if (factor < kMinPresenceFactor) factor = kMinPresenceFactor;
    if (factor > kMaxPresenceFactor) factor = kMaxPresenceFactor;

    factor_ = factor;
    saveFactorToNvs(factor_);

    if (calValid_) {
        uint32_t thr = thresholdFromStats(calMean_, calStdDev_, factor_);
        applyThreshold(thr);
        saveThresholdToNvs(thr);
    }
    return true;
}

void PresenceService::clearStoredThreshold() {
    prefs_.begin(kNvsNamespace, false);
    prefs_.remove(kNvsKeyPresenceThr);
    prefs_.remove(kNvsKeyPresenceFactor);
    prefs_.remove(kNvsKeyPresenceMean);
    prefs_.remove(kNvsKeyPresenceStd);
    prefs_.end();

    calValid_  = false;
    calMean_   = 0.0f;
    calStdDev_ = 0.0f;
    factor_    = kDefaultPresenceFactor;
    applyThreshold(kDefaultPresenceThreshold);
}

// ---------------------------------------------------------------------------
bool PresenceService::startCalibration() {
    if (calibrating_) return false;

    calibrating_     = true;
    calStartMs_      = millis();
    calLastSampleMs_ = 0;
    calCount_        = 0;
    calMeanAcc_      = 0.0;
    calM2Acc_        = 0.0;

    pendingEvent_ = PresenceEvent::CalibrationStarted;
    return true;
}

void PresenceService::cancelCalibration() {
    calibrating_ = false;
}

PresenceEvent PresenceService::takeEvent() {
    PresenceEvent ev = pendingEvent_;
    pendingEvent_ = PresenceEvent::None;
    return ev;
}

// ---------------------------------------------------------------------------
// Private
// ---------------------------------------------------------------------------

uint32_t PresenceService::readRaw() {
    return touchRead(PIN_PRESENCE);
}

// A new threshold re-evaluates the current reading immediately rather than
// debouncing the old comparison out first — the level did not change, the
// decision boundary did.
void PresenceService::applyThreshold(uint32_t thr) {
    threshold_    = thr;
    rawPresent_   = (raw_ > threshold_);
    present_      = rawPresent_;
    lastChangeMs_ = millis();
}

uint32_t PresenceService::thresholdFromStats(float mean, float stdDev, float factor) const {
    double thr = static_cast<double>(mean) + static_cast<double>(factor) * static_cast<double>(stdDev);
    if (thr < 1.0) thr = 1.0;
    if (thr > static_cast<double>(UINT32_MAX)) thr = static_cast<double>(UINT32_MAX);
    return static_cast<uint32_t>(thr + 0.5);
}

void PresenceService::updateCalibration(uint32_t now) {
    if ((now - calStartMs_) >= kPresenceCalMs) {
        finishCalibration();
        return;
    }

    if (calLastSampleMs_ != 0 && (now - calLastSampleMs_) < kPresenceCalSampleMs) return;
    calLastSampleMs_ = now;

    // Welford online algorithm
    double x = static_cast<double>(readRaw());
    raw_ = static_cast<uint32_t>(x);
    calCount_++;
    double delta = x - calMeanAcc_;
    calMeanAcc_ += delta / static_cast<double>(calCount_);
    double delta2 = x - calMeanAcc_;
    calM2Acc_ += delta * delta2;
}

void PresenceService::finishCalibration() {
    calibrating_ = false;

    lastCal_.samples = calCount_;
    lastCal_.factor  = factor_;

    if (calCount_ < kPresenceCalMinSamples) {
        lastCal_.ok        = false;
        lastCal_.mean      = 0.0f;
        lastCal_.stdDev    = 0.0f;
        lastCal_.threshold = threshold_; // left untouched
        pendingEvent_      = PresenceEvent::CalibrationFailed;
        return;
    }

    double variance = calM2Acc_ / static_cast<double>(calCount_);
    if (variance < 0.0) variance = 0.0;
    float mean   = static_cast<float>(calMeanAcc_);
    float stdDev = static_cast<float>(sqrt(variance));

    uint32_t thr = thresholdFromStats(mean, stdDev, factor_);

    calValid_  = true;
    calMean_   = mean;
    calStdDev_ = stdDev;

    lastCal_.ok        = true;
    lastCal_.mean      = mean;
    lastCal_.stdDev    = stdDev;
    lastCal_.threshold = thr;

    applyThreshold(thr);

    // Single NVS transaction (not 3 separate begin/end round trips) — each
    // commit is a flash erase/write and can stall for a while if NVS needs
    // to compact a page, so cut that time to a third right before the
    // non-blocking confirm blink in SFM::handlePresenceEvents().
    prefs_.begin(kNvsNamespace, false);
    prefs_.putFloat(kNvsKeyPresenceMean, mean);
    prefs_.putFloat(kNvsKeyPresenceStd, stdDev);
    prefs_.putFloat(kNvsKeyPresenceFactor, factor_);
    prefs_.putUInt(kNvsKeyPresenceThr, thr);
    prefs_.end();

    pendingEvent_ = PresenceEvent::CalibrationDone;
}

void PresenceService::saveThresholdToNvs(uint32_t thr) {
    prefs_.begin(kNvsNamespace, false);
    prefs_.putUInt(kNvsKeyPresenceThr, thr);
    prefs_.end();
}

void PresenceService::saveFactorToNvs(float factor) {
    prefs_.begin(kNvsNamespace, false);
    prefs_.putFloat(kNvsKeyPresenceFactor, factor);
    prefs_.end();
}

void PresenceService::loadFromNvs() {
    prefs_.begin(kNvsNamespace, true); // read-only
    threshold_ = prefs_.getUInt(kNvsKeyPresenceThr, kDefaultPresenceThreshold);
    factor_    = prefs_.getFloat(kNvsKeyPresenceFactor, kDefaultPresenceFactor);
    if (!isfinite(factor_) || factor_ < kMinPresenceFactor || factor_ > kMaxPresenceFactor) {
        factor_ = kDefaultPresenceFactor;
    }

    // Stats are only valid when both keys were written by a successful cal.
    const bool hasMean = prefs_.isKey(kNvsKeyPresenceMean);
    const bool hasStd  = prefs_.isKey(kNvsKeyPresenceStd);
    if (hasMean && hasStd) {
        calMean_   = prefs_.getFloat(kNvsKeyPresenceMean, 0.0f);
        calStdDev_ = prefs_.getFloat(kNvsKeyPresenceStd, 0.0f);
        calValid_  = isfinite(calMean_) && isfinite(calStdDev_) && calStdDev_ >= 0.0f;
    } else {
        calValid_  = false;
        calMean_   = 0.0f;
        calStdDev_ = 0.0f;
    }
    prefs_.end();
}

} // namespace sfm
