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
| **2 s**       | `kPelletLoadConfirmMs`  | `DispenserService.h` | Pellet sensor held this long during the feed → `PelletLoaded`. M1 stops on the first sighting and holds; if the beam clears before the window elapses the wheel resumes. Rejects a fragment tumbling past the beam |
| **0.5×**      | `kDefaultFeedSpeedScale`| `DispenserService.h` | M1 runs at this fraction of `motorSpeed_` — half speed, so the wheel drops one pellet at a time |
| **30 s**      | `kDomeOpenWarnMs`       | `DispenserService.h` | Dome held open continuously → `DomeOpenWarning` (non-sticky)                             |
| **5 s**       | `kLoadClearOnRaiseMs`   | `DispenserService.h` | After the raise starts, the load position sensor must clear within this or Fault/`Jam`   |
| **500 ms**    | `kPelletLostMs`         | `DispenserService.h` | Pellet sensor clear this long during the raise → Fault/`PelletLost`                     |
| **320 steps** | `kDefaultGrabSteps`     | `DispenserService.h` | M2 continues **down past** the load position sensor by this much before M1 turns. The sensor is not the drop height — the plate has to sit this far below it for the pellet to land cleanly. PG2 is ignored during this descent |
| **1420 steps**| `kDefaultRaiseSteps`    | `DispenserService.h` | M2 raise travel **from the drop position** (= 320 + 1100 above the load sensor); bench default for 28BYJ-48. Measure it with `ActuatorCalTest` from the grab depth, not from PG2 home |
| **800 steps** | `kDefaultSeekAwaySteps` | `DispenserService.h` | M2 up travel to clear the load sensor before the approach. Fixed travel, not sensor-gated: at the drop position PG2 may already read clear, and a sensor-gated seek would skip the move and then lower into the floor |


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
| 3072 steps  | `kDefaultLowerSteps`     | Max approach budget for M2 toward the load sensor. Must cover the longest legitimate approach — one that starts from a seek-away taken at presentation height, ≈ (`kDefaultRaiseSteps` − `kDefaultGrabSteps`) + `kDefaultSeekAwaySteps` ≈ 1900 steps. Exhausting it is not a fault on the first try: the approach backs off by one seek-away and re-approaches, and only faults if that also fails |
| 800 steps   | `kDefaultSeekAwaySteps`  | M2 up travel to clear the load sensor (fixed, not sensor-gated) |
| 320 steps   | `kDefaultGrabSteps`      | M2 down past the load sensor to the pellet-drop position |
| 1420 steps  | `kDefaultRaiseSteps`     | M2 up travel from the pellet-drop position |
| 4096 steps  | `kDefaultFeedMaxSteps`   | M1 max steps before feed timeout path     |
| 8 s         | `kDefaultLowerTimeoutMs` | M2 seek-away / approach / grab-descent timeout (re-armed per sub-phase) |
| 30 s        | `kDefaultFeedTimeoutMs`  | M1 pellet load timeout                    |
| 8 s         | `kDefaultRaiseTimeoutMs` | M2 raise phase timeout                    |
| 100 ms      | `kSensorDebounceMs`      | Sensor debounce (pellet / load / dome)    |


Not overrideable via SetConfig CAN yet — only compile-time / setter before begin.

---



## Dispenser — delivery confirmation, jam and warning timers

Same header; **not** runtime-configurable via CAN today.


| Value  | Constant                | Trigger                                                              |
| ------ | ----------------------- | -------------------------------------------------------------------- |
| 2 s    | `kPelletLoadConfirmMs`  | Pellet sensor held during the feed → `PelletLoaded`, raise starts    |
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
| —              | LED9 after discovery                  | Live dome mirror: lit = dome open. Yields to the button-hold blink            |
| —              | LED10                                 | Live pellet mirror: lit = pellet on the plate. No other steady owner          |
| 35000          | `presenceThreshold_`                  | Presence detection sensor threshold (`raw > thr` → animal present)            |
| 100 ms         | `flashLedsClear()` delays             | Visual confirm of NVS clear                                                   |


---



## How to change

1. Edit the `constexpr` (or ctor default) in the file listed above.
2. Rebuild / flash the node firmware.
3. Update the corresponding row in this document.
4. If the value becomes experiment- or site-specific, prefer a setter / `SetConfig` path so nodes do not need a reflash.
