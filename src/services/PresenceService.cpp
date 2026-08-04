#include "PresenceService.h"

namespace vfm {

// ---------------------------------------------------------------------------
ServiceStatus PresenceService::begin() {
    pinMode(PIN_PRESENCE, INPUT);

    threshold_ = loadThresholdFromNvs();

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
void PresenceService::setThreshold(uint32_t thr) {
    applyThreshold(thr);
}

void PresenceService::saveThreshold(uint32_t thr) {
    applyThreshold(thr);
    saveThresholdToNvs(thr);
}

void PresenceService::clearStoredThreshold() {
    prefs_.begin(kNvsNamespace, false);
    prefs_.remove(kNvsKeyPresenceThr);
    prefs_.end();
    applyThreshold(kDefaultPresenceThreshold);
}

// ---------------------------------------------------------------------------
bool PresenceService::startCalibration() {
    if (calibrating_) return false;

    calibrating_      = true;
    calStartMs_       = millis();
    calLastSampleMs_  = 0;
    calMin_           = UINT32_MAX;
    calMax_           = 0;
    calSum_           = 0;
    calCount_         = 0;

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

void PresenceService::updateCalibration(uint32_t now) {
    if ((now - calStartMs_) >= kPresenceCalMs) {
        finishCalibration();
        return;
    }

    if (calLastSampleMs_ != 0 && (now - calLastSampleMs_) < kPresenceCalSampleMs) return;
    calLastSampleMs_ = now;

    uint32_t v = readRaw();
    raw_ = v;
    if (v < calMin_) calMin_ = v;
    if (v > calMax_) calMax_ = v;
    calSum_ += v;
    calCount_++;
}

void PresenceService::finishCalibration() {
    calibrating_ = false;

    lastCal_.samples = calCount_;
    lastCal_.minRaw  = (calCount_ > 0) ? calMin_ : 0;
    lastCal_.maxRaw  = calMax_;
    lastCal_.avgRaw  = (calCount_ > 0) ? static_cast<uint32_t>(calSum_ / calCount_) : 0;

    if (calCount_ < kPresenceCalMinSamples) {
        lastCal_.ok        = false;
        lastCal_.threshold = threshold_; // left untouched
        pendingEvent_      = PresenceEvent::CalibrationFailed;
        return;
    }

    uint32_t range = calMax_ - calMin_;
    uint32_t thr   = calMax_ + range;
    if (thr <= calMax_) thr = calMax_ + 1; // zero-range / overflow guard

    lastCal_.ok        = true;
    lastCal_.threshold = thr;

    saveThreshold(thr);
    pendingEvent_ = PresenceEvent::CalibrationDone;
}

void PresenceService::saveThresholdToNvs(uint32_t thr) {
    prefs_.begin(kNvsNamespace, false);
    prefs_.putUInt(kNvsKeyPresenceThr, thr);
    prefs_.end();
}

uint32_t PresenceService::loadThresholdFromNvs() {
    prefs_.begin(kNvsNamespace, true); // read-only
    uint32_t thr = prefs_.getUInt(kNvsKeyPresenceThr, kDefaultPresenceThreshold);
    prefs_.end();
    return thr;
}

} // namespace vfm
