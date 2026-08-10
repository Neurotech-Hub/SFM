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
| **200 ms**    | `kPelletTakenConfirmMs` | `DispenserService.h` | Pellet sensor clear this long while `Loaded` → `PelletTaken`. Set from bench data: too short and a reach-in flicker counts as a take |
| **2 s**       | `kPelletLoadConfirmMs`  | `DispenserService.h` | Pellet sensor held this long during `Loading` → `OnPlate`. M1 stops on the first sighting and holds; if the beam clears before the window elapses the wheel resumes. Rejects a fragment tumbling past the beam |
| **0.5×**      | `kDefaultFeedSpeedScale`| `DispenserService.h` | M1 runs at this fraction of `motorSpeed_` — half speed, so the wheel drops one pellet at a time |
| **30 s**      | `kDomeOpenWarnMs`       | `DispenserService.h` | Dome held open continuously → `DomeOpenWarning` (non-sticky)                             |
| **5 s**       | `kLoadClearOnRaiseMs`   | `DispenserService.h` | After the raise starts, the load position sensor must clear within this or Fault/`Jam`   |
| **500 ms**    | `kPelletLostMs`         | `DispenserService.h` | Pellet sensor clear this long during the raise → Fault/`PelletLost`                     |
| **280 steps** | `kDefaultGrabSteps`     | `DispenserService.h` | M2 continues **down past** the load position sensor by this much before M1 turns. The sensor is not the drop height — the plate has to sit this far below it for the pellet to land cleanly. The load position sensor is ignored during this descent |
| **1480 steps**| `kDefaultRaiseSteps`    | `DispenserService.h` | M2 raise travel **from the drop position** (= 280 + 1200 above the load sensor); bench default for 28BYJ-48. Measure it with `ActuatorCalTest` from the grab depth, not from the load position sensor |
| **800 steps** | `kDefaultSeekAwaySteps` | `DispenserService.h` | M2 up travel cap to clear the load sensor before the approach. When Seeking starts with the load sensor asserted, motion stops at sensor-clear **or** this step count, whichever comes first. A fixed (non-gated) seek is used only when the node already knows the plate is at drop depth (`belowLoad_`) |


---



## Dispenser — motion defaults

Defined in `src/services/DispenserService.h`. Overridable before `begin()` via setters (`setRaiseSteps`, `setGrabSteps`, `setSeekAwaySteps`, `setFeedTimeoutMs`, etc.).

The three travel numbers are one calibrated set — the load sensor is a reference point, not the
drop height. Changing `kDefaultGrabSteps` moves the raise datum with it, so re-check
`kDefaultRaiseSteps` whenever the grab depth changes.


| Value       | Constant                 | Meaning                                |
| ----------- | ------------------------ | -------------------------------------- |
| 500 steps/s | `kDefaultMotorSpeed`     | AccelStepper commanded speed; M2 runs at this, M1 at `× kDefaultFeedSpeedScale` |
| 0.5×        | `kDefaultFeedSpeedScale` | M1 feed speed as a fraction of `motorSpeed_`. Kept as a scale, not an absolute, so `setMotorSpeed()` and the bench `+`/`-` keys move both motors together |
| 3072 steps  | `kDefaultLowerSteps`     | Max approach budget for M2 toward the load sensor. Must cover the longest legitimate approach — one that starts from a seek-away taken at presentation height, ≈ (`kDefaultRaiseSteps` − `kDefaultGrabSteps`) + `kDefaultSeekAwaySteps` ≈ 2000 steps. Exhausting it retries only when the load sensor is asserted or the plate is already known to be at drop depth; otherwise it faults (a blind seek-up after `PelletLost` can drive into the stop) |
| 800 steps   | `kDefaultSeekAwaySteps`  | M2 up travel cap to clear the load sensor (sensor-clear or this many steps, whichever first, when seek starts on the sensor) |
| 280 steps   | `kDefaultGrabSteps`      | M2 down past the load sensor to the pellet-drop position |
| 1480 steps  | `kDefaultRaiseSteps`     | M2 up travel from the pellet-drop position |
| 8 s         | `kDefaultLowerTimeoutMs` | M2 seek-away / approach / grab-descent timeout (re-armed per sub-phase) |
| 30 s        | `kDefaultFeedTimeoutMs`  | M1 pellet load timeout |
| 8 s         | `kDefaultRaiseTimeoutMs` | M2 raise phase timeout                    |


Not overrideable via SetConfig CAN yet — only compile-time / setter before begin.

---

## Dispenser — no-feed dispense

Same header. Unlike the motion defaults above, the dwell is **runtime-configurable per command**
(`DispenseNoFeed` payload, uint16 LE ms) — these are just the default and clamp bounds when the base
station omits or over/under-shoots it.


| Value  | Constant             | Meaning                                                              |
| ------ | -------------------- | --------------------------------------------------------------------- |
| 6 s    | `kDefaultNoFeedDwellMs` | Dwell at the drop position (M1 idle) before raising, when the command carries no dwell payload |
| 500 ms | `kNoFeedDwellMinMs`  | Commanded dwell is clamped to this floor                              |
| 60 s   | `kNoFeedDwellMaxMs`  | Commanded dwell is clamped to this ceiling                            |


---



## Dispenser — delivery confirmation, jam and warning timers

Same header; **not** runtime-configurable via CAN today.


| Value  | Constant                | Trigger                                                              |
| ------ | ----------------------- | -------------------------------------------------------------------- |
| 2 s    | `kPelletLoadConfirmMs`  | Pellet sensor held during `Loading` → `OnPlate`, raise starts        |
| 200 ms | `kPelletTakenConfirmMs` | Pellet sensor clear while `Loaded` → `PelletTaken`, cycle completes |
| 500 ms | `kPelletLostMs`         | Pellet sensor clear during the raise → Fault/`PelletLost`            |
| 5 s    | `kLoadClearOnRaiseMs`   | Load position sensor still blocked after raise start → Jam           |
| 30 s   | `kDomeOpenWarnMs`       | Dome held open → `DomeOpenWarning`                                   |


---



## Shared input debounce


One window for every sensor input, so a single bout produces one trigger event
and one clear event no matter which sensor reported it.


| Value  | Constant              | Location         | Notes                                                                     |
| ------ | --------------------- | ---------------- | ------------------------------------------------------------------------- |
| 100 ms | `kSensorDebounceMs`   | `ServiceTypes.h` | Pellet, load position, dome, **and** mouse presence all debounce on this |


---



## Presence detection (`PresenceService`)


The threshold is calibrated against the idle pad and stored in NVS under the
same namespace as the node ID, so it survives reboots. Calibration is started by
a short click of `PIN_BTN` or the serial `cal` command; a 3 s hold of the same
button clears the node ID instead. The multiplier is set with serial
`factor <n>` (also persisted).

Calibration rule: `threshold = mean + factor × std_dev` (Welford online stats
over the 5 s idle capture; population σ). The pad must stay clear for the
capture. Changing `factor` after a successful cal recomputes and saves the
threshold from the stored mean/σ without a new capture.


| Value    | Constant / where                | Notes                                                                                     |
| -------- | ------------------------------- | ----------------------------------------------------------------------------------------- |
| 35000    | `kDefaultPresenceThreshold`     | Compile-time fallback used only until a calibration is stored. Bench idle ≈ 35 000–35 500 |
| 3.0      | `kDefaultPresenceFactor`        | Default multiplier in `thr = mean + factor × σ`. Runtime via serial `factor <n>`           |
| 0.1–100  | `kMinPresenceFactor` / `kMax…`  | Clamp range for the factor                                                                |
| 5 s      | `kPresenceCalMs`                | Idle capture duration                                                                     |
| 25 ms    | `kPresenceCalSampleMs`          | Sample cadence during the capture (≈200 samples over 5 s)                                 |
| 10       | `kPresenceCalMinSamples`        | Below this the attempt fails and the threshold is left unchanged                          |
| 20 ms    | `kPresenceSampleMs`             | `touchRead()` cadence in normal operation — several samples per debounce window            |
| `presThr`| NVS key                         | Stored threshold                                                                          |
| `presFac`| NVS key                         | Stored factor                                                                             |
| `presMean` / `presStd` | NVS keys              | Last-cal mean / σ so factor changes can re-apply after reboot                             |


Threshold changes apply immediately rather than waiting out a debounce window:
the reading did not change, the decision boundary did.


---



## CAN / discovery


| Value  | Constant                      | Location         | Notes                                                                       |
| ------ | ----------------------------- | ---------------- | --------------------------------------------------------------------------- |
| 5 s    | `kDefaultHeartbeatIntervalMs` | `CanService.h`   | Default status heartbeat; **runtime** via `SetConfig` / `HeartbeatInterval` |
| 500 ms | `kAnnounceRetryMs`            | `NodeIdentity.h` | Retry ANNOUNCE while awaiting ASSIGN                                        |
| 5 s    | `kDiscoveryTimeoutMs`         | `NodeIdentity.h` | Give up waiting for ASSIGN                                                  |


---



## UI / LED / button (`VFM`)


| Value          | Where                                 | Notes                                                                              |
| -------------- | ------------------------------------- | ---------------------------------------------------------------------------------- |
| 3 s            | `VFM` ctor → `btnHoldMs_(3000)`       | Hold to arm NVS clear (`VFM.h` in-class default `1000` is overridden by ctor)      |
| 50 ms          | `kBtnClickMinMs` (`VFM.h`)            | Minimum press for a click to count as "recalibrate presence"; shorter = bounce     |
| 100 ms         | `VFM.cpp` LED9 blink while hold armed | Rapid blink warning                                                                |
| 1.5 s / 150 ms | `kPingBlinkMs` / `kPingBlinkPeriodMs` | Status LED “which node” blink on Ping                                              |
| 500 ms         | LED9 blink at boot                    | Fast blink = booting                                                               |
| 1 s            | LED9 / status blink                   | Slow = waiting for discovery                                                       |
| —              | LED9 during presence calibration      | Solid ON for the whole capture; yields back to the dome mirror when done            |
| —              | LED9 after discovery                  | Live dome mirror: lit = dome open. Yields to the button-hold blink                  |
| —              | LED10                                 | Live pellet mirror: lit = pellet on the plate. No other steady owner                |
| 100 ms         | `LedService::flashConfirm()` delays   | Visual confirm (NVS clear / presence cal)                                          |


---



## How to change

1. Edit the `constexpr` (or ctor default) in the file listed above.
2. Rebuild / flash the node firmware.
3. Update the corresponding row in this document.
4. If the value becomes experiment- or site-specific, prefer a setter / `SetConfig` path so nodes do not need a reflash.
