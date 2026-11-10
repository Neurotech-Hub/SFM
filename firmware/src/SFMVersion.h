#pragma once

#include <stdint.h>

namespace sfm {

// Bump together with firmware/library.properties. Minor for behaviour or
// protocol changes, patch for fixes. Record what changed under Versioning
// in firmware/docs/HARDCODED_VALUES.md. 1.6.0: dome-safe raise, auto-reload,
// drop position latched at the raw PG2 break, and this version report.
// 1.7.0: 1 s take confirm, PelletReturned, FeedSkipped presented byte.
constexpr uint8_t kFirmwareVersionMajor = 1;
constexpr uint8_t kFirmwareVersionMinor = 7;
constexpr uint8_t kFirmwareVersionPatch = 0;
constexpr char    kFirmwareVersionStr[] = "1.7.0";

} // namespace sfm
