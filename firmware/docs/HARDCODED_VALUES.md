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
| **1 s**       | `kPelletTakenConfirmMs` | `DispenserService.h` | Pellet sensor clear this long while `Loaded` → `PelletTaken`. Too short and a reach-in flicker or a nudged pellet counts as a take (200 ms did, session 110426_1.2F) |
| **500 ms**    | `kPelletReturnConfirmMs`| `DispenserService.h` | After a take, pellet sensor blocked this long with the plate still up → `PelletReturned`, back to `Loaded` |
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
| **350 steps** | `kDefaultGrabSteps`     | `DispenserService.h` | M2 continues **down past the raw PG2 break** by this much before M1 turns. The debounced sensor is not the datum — the edge is latched the instant the beam breaks, and the 100 ms debounce only confirms it |
| **1650 steps**| `kDefaultRaiseSteps`    | `DispenserService.h` | M2 raise travel **from the drop position** (raw PG2 break minus `kDefaultGrabSteps`). Bench default for 28BYJ-48. Measure it with `ActuatorCalTest` from that drop position |
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
| 3072 steps  | `kDefaultLowerSteps`     | Max approach budget for M2 toward the load sensor. Must cover the longest legitimate approach — one that starts from a seek-away taken at presentation height, ≈ (`kDefaultRaiseSteps` − `kDefaultGrabSteps`) + `kDefaultSeekAwaySteps` ≈ 2100 steps. Exhausting it retries only when the load sensor is asserted or the plate is already known to be at drop depth; otherwise it faults (a blind seek-up after `PelletLost` can drive into the stop) |
| 800 steps   | `kDefaultSeekAwaySteps`  | M2 up travel cap to clear the load sensor (sensor-clear or this many steps, whichever first, when seek starts on the sensor) |
| 350 steps   | `kDefaultGrabSteps`      | M2 down past the raw PG2 break to the pellet-drop position |
| 1650 steps  | `kDefaultRaiseSteps`     | M2 up travel from the pellet-drop position |
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
| 1 s    | `kPelletTakenConfirmMs` | Pellet sensor clear while `Loaded` → `PelletTaken`, cycle completes |
| 500 ms | `kPelletReturnConfirmMs`| Pellet back in the beam after a take, plate up → `PelletReturned`, `Loaded` |
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


Reported presence is still a pure touch measurement. The dome only freezes
the baseline and produces a per-bout summary; it is not OR-ed into presence.

Each 20 ms sample is the median of the last 5 raw reads, then an EMA
(`kPresenceSmoothAlpha` 0.3). That filter, plus the 100 ms debounce, puts a
present or clear edge about 200 ms after the pad actually changed. Compare
sessions across the 1.7.0 and 1.7.1 firmware with that lag in mind.

```
onDelta = factor / 100 × baseline
present once (smooth − baseline) > onDelta for 100 ms
clear   once (smooth − baseline) < 0.5 × onDelta for 100 ms
```

The baseline is a double and tracks only while the pad reads clear, the
delta is already below the clear level, and the dome has been closed for
2 s. It rises with a 60 s time constant and falls with a 5 s time constant.
A falling reading is treated as drift.

At boot the baseline is the median of the first 1 s. Nothing is restored
from NVS: a stored baseline that had drifted low would latch false presence,
and the tracker will not move while it believes the pad is occupied. The
first boot of 1.7.1 deletes `presThr`, `presFac`, `presMean` and `presStd`
and writes `presPct` (the percent). An old value such as 60 is never read
as a percent.

`cal` (button, serial, or `CanCmd::CalibratePresence`) re-baselines over
20 s without pausing detection. The new baseline is the lowest 1 s median
in that window; sigma is the spread of that same second of smoothed
samples. If the dome is open the window waits until it has been closed for
2 s. A finger held down the whole time leaves the baseline high, so that
visit reads clear; fast downward tracking corrects it after the finger lifts.

The stuck guard watches for 15 min of presence with the dome closed and a
smoothed range under 3× the idle sigma. It emits `PresenceStuckCandidate`
and does not move the baseline (`kPresenceStuckActive` is false). Mice rest
on the pad, so an automatic rebaseline would end a real visit and release
experiments that wait on presence-clear. Turn the flag on only after a run
shows wet-pad and resting-mouse readings can be told apart.

Build every node with Arduino-ESP32 core **3.3.13** (Boards Manager:
"esp32 by Espressif Systems"). The logic uses `touchRead()` only.


| Value | Constant | Notes |
| ----- | -------- | ----- |
| 1.0 | `kDefaultPresenceFactor` | Percent above baseline. Placeholder until a 24 h run. NVS key `presPct` |
| 0.2–10 | `kMinPresenceFactor` / `kMaxPresenceFactor` | Clamp. Out-of-range `presPct` resets to the default |
| 20 ms | `kPresenceSampleMs` | `touchRead()` cadence |
| 5 / 0.3 | `kPresenceMedianN` / `kPresenceSmoothAlpha` | Spike reject, then EMA |
| 60 s / 5 s | `kBaselineTauUpMs` / `kBaselineTauDownMs` | Baseline time constants |
| 0.5 | `kPresenceOffRatio` | Clear level as a fraction of onDelta |
| 1 s | `kPresenceSeedMs` | Boot median window |
| 20 s | `kPresenceCalMs` | Re-baseline window |
| 2 s | `kDomeBaselineHoldMs` | Dome must be closed this long before tracking or a re-baseline starts |
| 15 min | `kPresenceStuckMs` | Shadow stuck-candidate delay. `kPresenceStuckActive` = false |
| 1 s | `kPresenceTraceMs` | Opt-in trace period. `ConfigType::PresenceTrace` (0x03), not stored |


The on-node test procedure is [PRESENCE_DRIFT_TEST.md](PRESENCE_DRIFT_TEST.md).


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

Use the **minor** number for a behaviour or protocol change, and the **patch** number for a fix that does not change the wire format. Current release: **1.7.1**.

When you bump the version, add a subsection here for the new number. The base station only records the number; this list is what that number means.

### 1.7.1

Presence no longer uses a frozen `mean + factor × σ` threshold. The factor is a percent above a drifting baseline. Flash the base station with this tree at the same time as the nodes: the calibration payload, two new events, the trace id, and two heartbeat flag bits all changed.

- **Decision.** Median of 5, then EMA. Present above `factor%` of the baseline, clear below half of that, each debounced 100 ms. End-to-end lag is about 200 ms.
- **Baseline.** Tracks only while clear, below the clear level, and with the dome closed for 2 s. Up τ = 60 s, down τ = 5 s. Boot seed is the median of the first 1 s. Not stored.
- **Factor.** `ConfigType::PresenceFactor` (0x02) is unchanged on the wire and now means percent, clamped 0.2–10, default 1.0, NVS key `presPct`. The old keys are deleted on first boot.
- **Re-baseline.** `CalibratePresence` still 0x09. 20 s, detection keeps running. `PresenceCalResult` (0x10) is now `ok`, `baseline` uint32 LE, `sigma×10` uint16 LE.
- **New events.** `PresenceStuckCandidate` (0x17): delta%×100 uint16, range uint16. Shadow only. `DomeBoutSummary` (0x18): touchSeen, peak%×100 uint16, prelift%×100 int16, duration in 100 ms steps uint16.
- **Heartbeat byte 4.** Bit 3 sensitivity warning, bit 4 baseline frozen. Byte 3 stays 0/1.
- **Trace.** `ConfigType::PresenceTrace` (0x03, uint8, not stored) sends one frame a second on `0x400 + nodeId`: baseline uint24, max delta int16, min delta int16, flags.

### 1.7.0

- **Take confirm 1 s** (was 200 ms). A nudged pellet that rolls back is no longer a take.
- **`PelletReturned` (`0x16`, count LE16).** After a take, if the pellet is back in the beam for 500 ms with the plate still up, the node reports it and returns to `Loaded`. The real take then reports `PelletTaken` again with the same `takenCount_`, so the pellet is counted once.
- **`FeedSkipped` presented byte.** Payload is now count LE16 + `presented`: 1 when the occupied plate was already at the top (the node is `Loaded`, no motion, no `Loaded` event follows), 0 when it homes and raises (`Loaded` follows). Base stations older than this treat the byte as absent.

### 1.6.0

First version the node reports on the bus (`FirmwareInfo`). Everything below is relative to 1.5.0, which does not send that event.

- **Drop position.** Latched at the raw PG2 break, then 280 steps further down. The 100 ms debounce only confirms the edge. A raise is always 1480 steps from that position. After a fault or `Recover` the position is forgotten and the next dispense homes again.
- **Seek from below the sensor.** Up until PG2 breaks and then clears, still capped at 800 steps. A cap that arrives first faults `ActuatorTimeout`. Seeking that starts on an asserted PG2 is unchanged: up until the beam clears.
- **Occupied plate.** Homes through the same seek, approach, and grab (M1 stays off), then raises. The old shortened raise from inside the beam is gone. A plate already at the top still stays `Loaded` with no motion.
- **Dome.** A raise does not start until the dome has been closed for 500 ms (`DomeHold`, no timeout). If the dome opens before 80% of the raise, the plate retracts to the drop position and waits. No-feed cycles follow the same rule on their own dome.
- **Pellet during the raise.** Clear for 500 ms before 80%, or at/after 80% with the dome closed: retract and reload. Clear for 500 ms at/after 80% with the dome open: count it as a take (`Loaded`, then `PelletTaken`), finish the travel, return to `Idle`. Up to 3 reloads per dispense, each logged as `PelletReload`; the next miss faults `PelletLost`.
- **New wire values.** States `DomeHold` = 8, `Retracting` = 9. Events `DomeHold` = `0x12`, `Retracting` = `0x13`, `PelletReload` = `0x14` (count, reason, attempt), `FirmwareInfo` = `0x15` (major, minor, patch).
