# Hardcoded / tunable values

Living note of firmware constants that may need changing after bench or field tweaks.
**Update this file when you change a default.** Source of truth remains the code; this is the checklist.

GUI / experiment / report defaults: [BASE_STATION_HARDCODED_VALUES.md](../../packages/dev_gui/docs/BASE_STATION_HARDCODED_VALUES.md).

Pins (`SFMPins.h`) and CAN ID opcodes (`ServiceTypes.h`) are omitted unless they carry timing or motion meaning.
For what these timers guard and where they sit in the cycle, see [DISPENSE_CYCLE.md](DISPENSE_CYCLE.md).

---

## High-priority (likely to change)

These are the ones called out most often during bring-up.


| Value         | Constant                | Location             | Notes                                                                                   |
| ------------- | ----------------------- | -------------------- | --------------------------------------------------------------------------------------- |
| **200 ms**    | `kPelletTakenConfirmMs` | `DispenserService.h` | Pellet sensor clear this long while `Loaded` → `PelletTaken`. Set from bench data: too short and a reach-in flicker counts as a take |
| **2 s**       | `kPelletLoadConfirmMs`  | `DispenserService.h` | Pellet sensor held this long during `Loading` → `OnPlate`. Raw beam stops M1 immediately; if the beam clears before the window elapses the wheel resumes the run-pause pattern. Rejects a fragment tumbling past the beam |
| **0.5×**      | `kDefaultFeedSpeedScale`| `DispenserService.h` | M1 runs at this fraction of `motorSpeed_` — half speed, so the wheel drops one pellet at a time |
| **500 steps** | `kDefaultFeedBurstSteps`| `DispenserService.h` | M1 steps per run burst before a settle pause (same idea as `StepperMotorTest`) |
| **1 s**       | `kDefaultFeedPauseMs`   | `DispenserService.h` | M1 coils-off pause between feed bursts — avoids pellet build-up on the plate |
| **30 s**      | `kDomeOpenWarnMs`       | `DispenserService.h` | Dome held open continuously → `DomeOpenWarning` (non-sticky)                             |
| **5 s**       | `kLoadClearOnRaiseMs`   | `DispenserService.h` | After the raise starts, the load position sensor must clear within this or Fault/`Jam`   |
| **500 ms**    | `kPelletLostMs`         | `DispenserService.h` | Pellet sensor clear this long during the raise → retract and reload, or an early take once the raise is 80% done and the dome is open |
| **80%**       | `kRaiseCommitPct`       | `DispenserService.h` | Below this fraction of the raise, a dome opening or a lost pellet retracts the plate. At or above it, a lost pellet with the dome open counts as a take |
| **500 ms**    | `kDomeCloseSettleMs`    | `DispenserService.h` | Dome must stay closed this long before a raise starts or resumes |
| **3**         | `kMaxPelletReloads`     | `DispenserService.h` | Automatic reloads in one dispense. The next miss faults `PelletLost` |
| **280 steps** | `kDefaultGrabSteps`     | `DispenserService.h` | M2 continues **down past the raw PG2 break** by this much before M1 turns. The debounced sensor is not the datum — the edge is latched the instant the beam breaks, and the 100 ms debounce only confirms it |
| **1480 steps**| `kDefaultRaiseSteps`    | `DispenserService.h` | M2 raise travel **from the drop position** (raw PG2 break minus `kDefaultGrabSteps`). Bench default for 28BYJ-48. Measure it with `ActuatorCalTest` from that drop position |
| **800 steps** | `kDefaultSeekAwaySteps` | `DispenserService.h` | M2 up travel cap before the approach. On an asserted load sensor, motion stops at sensor-clear or this count. From a known drop depth the seek runs until the beam breaks and then clears, and faults if the cap arrives first |


---



## Dispenser — motion defaults

Defined in `src/services/DispenserService.h`. Overridable before `begin()` via setters (`setRaiseSteps`, `setGrabSteps`, `setSeekAwaySteps`, `setFeedTimeoutMs`, `setFeedBurstSteps`, `setFeedPauseMs`, etc.).

The three travel numbers are one calibrated set — the load sensor is a reference point, not the
drop height. Changing `kDefaultGrabSteps` moves the raise datum with it, so re-check
`kDefaultRaiseSteps` whenever the grab depth changes.


| Value       | Constant                 | Meaning                                |
| ----------- | ------------------------ | -------------------------------------- |
| 500 steps/s | `kDefaultMotorSpeed`     | AccelStepper commanded speed; M2 runs at this, M1 at `× kDefaultFeedSpeedScale` |
| 0.5×        | `kDefaultFeedSpeedScale` | M1 feed speed as a fraction of `motorSpeed_`. Kept as a scale, not an absolute, so `setMotorSpeed()` and the bench `+`/`-` keys move both motors together |
| 500 steps   | `kDefaultFeedBurstSteps` | M1 steps per burst during `Loading`, then pause |
| 1 s         | `kDefaultFeedPauseMs`    | Coils-off settle pause between M1 feed bursts |
| 3072 steps  | `kDefaultLowerSteps`     | Max approach budget for M2 toward the load sensor. Must cover the longest legitimate approach — one that starts from a seek-away taken at presentation height, ≈ (`kDefaultRaiseSteps` − `kDefaultGrabSteps`) + `kDefaultSeekAwaySteps` ≈ 2000 steps. Exhausting it retries only when the load sensor is asserted or the plate is already known to be at drop depth; otherwise it faults (a blind seek-up after `PelletLost` can drive into the stop) |
| 800 steps   | `kDefaultSeekAwaySteps`  | M2 up travel cap to clear the load sensor (sensor-clear or this many steps, whichever first, when seek starts on the sensor) |
| 280 steps   | `kDefaultGrabSteps`      | M2 down past the raw PG2 break to the pellet-drop position |
| 1480 steps  | `kDefaultRaiseSteps`     | M2 up travel from the pellet-drop position |
| 80%         | `kRaiseCommitPct`        | Raise fraction below which a dome open or pellet loss retracts |
| 500 ms      | `kDomeCloseSettleMs`     | Dome-closed settle before a raise starts or resumes |
| 3           | `kMaxPelletReloads`      | Reloads per dispense before Fault/`PelletLost` |
| 8 s         | `kDefaultLowerTimeoutMs` | M2 seek-away / approach / grab-descent timeout (re-armed per sub-phase) |
| 30 s        | `kDefaultFeedTimeoutMs`  | M1 pellet load timeout |
| 8 s         | `kDefaultRaiseTimeoutMs` | M2 raise phase timeout                    |


Not overrideable via SetConfig CAN yet — only compile-time / setter before begin.

---

## Dispenser — no-feed dispense

**No tunables.** A no-feed cycle holds at the drop position until it sees a `Raising` event from another
node on the bus, then raises. There is no dwell constant, no clamp, and no payload on `DispenseNoFeed` —
the fed node's own raise is the timing reference, so nothing here needs tuning per rig. See
[DISPENSE_CYCLE.md](DISPENSE_CYCLE.md) § No-feed dispense.

There is also no timeout: a node whose peer never raises holds until `Recover`.


---



## Dispenser — delivery confirmation, jam and warning timers

Same header; **not** runtime-configurable via CAN today.


| Value  | Constant                | Trigger                                                              |
| ------ | ----------------------- | -------------------------------------------------------------------- |
| 2 s    | `kPelletLoadConfirmMs`  | Pellet sensor held during `Loading` → `OnPlate`, raise starts        |
| 200 ms | `kPelletTakenConfirmMs` | Pellet sensor clear while `Loaded` → `PelletTaken`, cycle completes |
| 500 ms | `kPelletLostMs`         | Pellet sensor clear during the raise → retract and reload, or an early take |
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
a short click of `PIN_BTN`, the serial `cal` command, or a broadcast
`CanCmd::CalibratePresence` (0x09) from the base station — see
[DISPENSE_CYCLE.md](DISPENSE_CYCLE.md) for the wire format and the resulting
`CanEvent::PresenceCalResult` (0x10). A 3 s hold of `PIN_BTN` clears the node ID
instead. The multiplier is set with serial `factor <n>` (also persisted).

Calibration rule: `threshold = mean + factor × std_dev` (Welford online stats
over the 5 s idle capture; population σ). The pad must stay clear for the
capture. Changing `factor` after a successful cal recomputes and saves the
threshold from the stored mean/σ without a new capture.


| Value    | Constant / where                | Notes                                                                                     |
| -------- | ------------------------------- | ----------------------------------------------------------------------------------------- |
| 35000    | `kDefaultPresenceThreshold`     | Compile-time fallback used only until a calibration is stored. Bench idle ≈ 35 000–35 500 |
| 60       | `kDefaultPresenceFactor`        | Default multiplier in `thr = mean + factor × σ`. Runtime via serial `factor <n>`. A factor already stored in NVS (`presFac`) is kept across reboot |
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
| 5 s    | `kDefaultHeartbeatIntervalMs` | `CanService.h`   | Boot default until `SetConfig`; **min** `kMinHeartbeatIntervalMs` = 100 ms; GUI typically pushes 60 s |
| 100 ms | `kMinHeartbeatIntervalMs`     | `CanService.h`   | Floor applied on `SetConfig` / `HeartbeatInterval`                                                  |
| 500 ms | `kAnnounceRetryMs`            | `NodeIdentity.h` | Retry ANNOUNCE while awaiting ASSIGN                                        |
| 5 s    | `kDiscoveryTimeoutMs`         | `NodeIdentity.h` | Give up waiting for ASSIGN                                                  |


---



## UI / LED / button (`SFM`)


| Value          | Where                                 | Notes                                                                              |
| -------------- | ------------------------------------- | ---------------------------------------------------------------------------------- |
| 3 s            | `SFM` ctor → `btnHoldMs_(3000)`       | Hold to arm NVS clear (`SFM.h` in-class default `1000` is overridden by ctor)      |
| 50 ms          | `kBtnClickMinMs` (`SFM.h`)            | Minimum press for a click to count as "recalibrate presence"; shorter = bounce     |
| 100 ms         | `SFM.cpp` LED9 blink while hold armed | Rapid blink warning                                                                |
| 1.5 s / 150 ms | `kPingBlinkMs` / `kPingBlinkPeriodMs` | Status LED “which node” blink on Ping                                              |
| 500 ms (50–5000 ms clamp) | `kDefaultSyncFlashMs` / `kMinSyncFlashMs` / `kMaxSyncFlashMs` (`SFM.h`) | Status LED solid-ON hold on `CanCmd::SyncFlash` (camera sync at session start)     |
| 500 ms         | LED9 blink at boot                    | Fast blink = booting                                                               |
| 1 s            | LED9 / status blink                   | Slow = waiting for discovery                                                       |
| —              | LED9 during presence calibration      | Solid ON for the whole capture                                                     |
| 600 ms / 100 ms | `kCalConfirmDurationMs` / `kCalConfirmBlinkMs` (`SFM.h`) | After a successful calibration, status LED and LED 9 blink together, then LED 9 returns to the dome mirror |
| —              | LED9 after discovery                  | Live dome mirror: lit = dome open. Yields to the button-hold blink                  |
| —              | LED10                                 | Live pellet mirror: lit = pellet on the plate. No other steady owner                |
| 100 ms         | `LedService::flashConfirm()` delays   | Visual confirm of an NVS ID clear (status, LED 9, and LED 10). Presence-cal confirm is the non-blocking blink above |


---



## How to change

1. Edit the `constexpr` (or ctor default) in the file listed above.
2. Rebuild / flash the node firmware.
3. Update the corresponding row in this document.
4. If the value becomes experiment- or site-specific, prefer a setter / `SetConfig` path so nodes do not need a reflash.

## Versioning

The running firmware reports itself as `CanEvent::FirmwareInfo` (`0x15`, payload `major, minor, patch`). The base station logs that as a `Firmware Info` row and copies the per-node map into `session_start`. A node that never sends it (1.5.0 or older) is logged as `unknown`.

The number lives in two places that must stay identical. `packages/dev_gui/tests/test_protocol.py` fails if they differ.

| File | What to bump |
| ---- | ------------ |
| `firmware/src/SFMVersion.h` | `kFirmwareVersionMajor` / `Minor` / `Patch` and `kFirmwareVersionStr` |
| `firmware/library.properties` | `version=` |

Use the **minor** number for a behaviour or protocol change, and the **patch** number for a fix that does not change the wire format. Current release: **1.6.0**.

When you bump the version, add a subsection here for the new number. The base station only records the number; this list is what that number means.

### 1.6.0

First version the node reports on the bus (`FirmwareInfo`). Everything below is relative to 1.5.0, which does not send that event.

- **Drop position.** Latched at the raw PG2 break, then 280 steps further down. The 100 ms debounce only confirms the edge. A raise is always 1480 steps from that position. After a fault or `Recover` the position is forgotten and the next dispense homes again.
- **Seek from below the sensor.** Up until PG2 breaks and then clears, still capped at 800 steps. A cap that arrives first faults `ActuatorTimeout`. Seeking that starts on an asserted PG2 is unchanged: up until the beam clears.
- **Occupied plate.** Homes through the same seek, approach, and grab (M1 stays off), then raises. The old shortened raise from inside the beam is gone. A plate already at the top still stays `Loaded` with no motion.
- **Dome.** A raise does not start until the dome has been closed for 500 ms (`DomeHold`, no timeout). If the dome opens before 80% of the raise, the plate retracts to the drop position and waits. No-feed cycles follow the same rule on their own dome.
- **Pellet during the raise.** Clear for 500 ms before 80%, or at/after 80% with the dome closed: retract and reload. Clear for 500 ms at/after 80% with the dome open: count it as a take (`Loaded`, then `PelletTaken`), finish the travel, return to `Idle`. Up to 3 reloads per dispense, each logged as `PelletReload`; the next miss faults `PelletLost`.
- **New wire values.** States `DomeHold` = 8, `Retracting` = 9. Events `DomeHold` = `0x12`, `Retracting` = `0x13`, `PelletReload` = `0x14` (count, reason, attempt), `FirmwareInfo` = `0x15` (major, minor, patch).
