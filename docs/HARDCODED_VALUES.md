# Hardcoded / tunable values

Living note of firmware constants that may need changing after bench or field tweaks.
**Update this file when you change a default.** Source of truth remains the code; this is the checklist.

Pins (`VFMPins.h`) and CAN ID opcodes (`ServiceTypes.h`) are omitted unless they carry timing or motion meaning.
For what these timers guard and where they sit in the cycle, see [DISPENSE_CYCLE.md](DISPENSE_CYCLE.md).

---

## High-priority (likely to change)

These are the ones called out most often during bring-up.


| Value         | Constant                | Location             | Notes                                                                                   |
| ------------- | ----------------------- | -------------------- | --------------------------------------------------------------------------------------- |
| **200 ms**    | `kPelletTakenConfirmMs` | `DispenserService.h` | Pellet sensor clear this long while presented → `PelletTaken`. Set from bench data: too short and a reach-in flicker counts as a take |
| **30 s**      | `kDomeOpenWarnMs`       | `DispenserService.h` | Dome held open continuously → `DomeOpenWarning` (non-sticky)                             |
| **5 s**       | `kLoadClearOnRaiseMs`   | `DispenserService.h` | After the raise starts, the load position sensor must clear within this or Fault/`Jam`   |
| **500 ms**    | `kPelletLostMs`         | `DispenserService.h` | Pellet sensor clear this long during the raise → Fault/`PelletLost`                     |
| **700 steps** | `kDefaultRaiseSteps`    | `DispenserService.h` | M2 raise travel from the load position; bench default for 28BYJ-48                      |


---



## Dispenser — motion defaults

Defined in `src/services/DispenserService.h`. Overridable before `begin()` via setters (`setRaiseSteps`, `setFeedTimeoutMs`, etc.).


| Value       | Constant                 | Meaning                                |
| ----------- | ------------------------ | -------------------------------------- |
| 500 steps/s | `kDefaultMotorSpeed`     | AccelStepper commanded speed (M1/M2)   |
| 2048 steps  | `kDefaultLowerSteps`     | Max seek-away / approach budget for M2 |
| 700 steps   | `kDefaultRaiseSteps`     | M2 up travel from the load position    |
| 4096 steps  | `kDefaultFeedMaxSteps`   | M1 max steps before feed timeout path  |
| 8 s         | `kDefaultLowerTimeoutMs` | M2 lower / seek-away phase timeout     |
| 30 s        | `kDefaultFeedTimeoutMs`  | M1 pellet load timeout                 |
| 8 s         | `kDefaultRaiseTimeoutMs` | M2 raise phase timeout                 |
| 20 ms       | `kSensorDebounceMs`      | Sensor debounce (pellet / load / dome) |


Not overrideable via SetConfig CAN yet — only compile-time / setter before begin.

---



## Dispenser — delivery confirmation, jam and warning timers

Same header; **not** runtime-configurable via CAN today.


| Value  | Constant                | Trigger                                                              |
| ------ | ----------------------- | -------------------------------------------------------------------- |
| 200 ms | `kPelletTakenConfirmMs` | Pellet sensor clear while presented → `PelletTaken`, cycle completes |
| 500 ms | `kPelletLostMs`         | Pellet sensor clear during the raise → Fault/`PelletLost`            |
| 5 s    | `kLoadClearOnRaiseMs`   | Load position sensor still blocked after raise start → Jam           |
| 30 s   | `kDomeOpenWarnMs`       | Dome held open → `DomeOpenWarning`                                   |


---



## CAN / discovery


| Value  | Constant                      | Location         | Notes                                                                       |
| ------ | ----------------------------- | ---------------- | --------------------------------------------------------------------------- |
| 5 s    | `kDefaultHeartbeatIntervalMs` | `CanService.h`   | Default status heartbeat; **runtime** via `SetConfig` / `HeartbeatInterval` |
| 500 ms | `kAnnounceRetryMs`            | `NodeIdentity.h` | Retry ANNOUNCE while awaiting ASSIGN                                        |
| 5 s    | `kDiscoveryTimeoutMs`         | `NodeIdentity.h` | Give up waiting for ASSIGN                                                  |


---



## UI / LED / button (`VFM`)


| Value          | Where                                 | Notes                                                                         |
| -------------- | ------------------------------------- | ----------------------------------------------------------------------------- |
| 3 s            | `VFM` ctor → `btnHoldMs_(3000)`       | Hold to arm NVS clear (`VFM.h` in-class default `1000` is overridden by ctor) |
| 100 ms         | `VFM.cpp` LED9 blink while hold armed | Rapid blink warning                                                           |
| 1.5 s / 150 ms | `kPingBlinkMs` / `kPingBlinkPeriodMs` | Status LED “which node” blink on Ping                                         |
| 500 ms         | LED9 blink at boot                    | Fast blink = booting                                                          |
| 1 s            | LED9 / status blink                   | Slow = waiting for discovery                                                  |
| 40             | `touchThreshold_`                     | Capacitive animal-presence threshold                                          |
| 100 ms         | `flashLedsClear()` delays             | Visual confirm of NVS clear                                                   |


---



## How to change

1. Edit the `constexpr` (or ctor default) in the file listed above.
2. Rebuild / flash the node firmware.
3. Update the corresponding row in this document.
4. If the value becomes experiment- or site-specific, prefer a setter / `SetConfig` path so nodes do not need a reflash.
