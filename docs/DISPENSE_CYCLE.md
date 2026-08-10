# Dispense cycle

How a VFM node delivers a pellet, how it knows the pellet was taken, and what it reports along the way.
This is the reference for the sensing model and the event vocabulary; tunable timings live in
[HARDCODED_VALUES.md](HARDCODED_VALUES.md). Pin / motor and sensor wiring: [WIRING.md](WIRING.md).

## Sensors

Each node has three optical sensors, named for the job they do. Hardware symbols: **PG1** = pellet
sensor, **PG2** = load-position sensor, **PG3** = dome sensor; **M1** = pellet motor, **M2** =
actuator motor (see [WIRING.md](WIRING.md)).


| Sensor                   | Pin    | Asserted when         | Job                                                                                                                    |
| ------------------------ | ------ | --------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **Pellet sensor**        | GPIO46 | beam broken (pin LOW) | A pellet is sitting on the plate. Latching: it stays asserted from the moment the pellet lands until the pellet leaves |
| **Load position sensor** | GPIO45 | beam broken (pin LOW) | The actuator is at the load position (fully down)                                                                      |
| **Dome sensor**          | GPIO44 | pin HIGH (idle LOW)   | The dome is lifted. The dome is spring-returned, so every access is a clean lift-and-release bout                      |


All three are debounced in firmware by `kSensorDebounceMs` (100 ms) before any logic or reporting acts on them.

Two of them are mirrored live on the board LEDs so a bench operator can read the sensors without a serial
monitor: **LED 10 = pellet sensor**, **LED 9 = dome sensor**, lit when asserted. Both follow the debounced
state, so what the LED shows is what the firmware is acting on. LED 9 is shared with the boot and discovery
blinks, the button-hold warning, and the presence calibration capture, and only becomes the dome mirror once
the node is enabled and none of those own it.

A fourth input, **mouse presence**, is the capacitive pad read by `PresenceService`. It is independent of the
pellet sensor and the dispense cycle, and is reported for behavioral context only. Raw counts rise when a mouse
is present, so mouse presence is `raw > threshold`, and it shares the same 100 ms debounce as the photogates — one approach produces
one trigger and one clear. The threshold is calibrated against the idle pad (short click of the on-board button
or the serial `cal` command) and stored in NVS alongside the node ID, so it survives reboots.

The pellet sensor sits on the plate and reports occupancy in every state. Because it holds its state, the node
always knows whether the plate is occupied — before a dispense, during travel, and after an access.

## The cycle

```
 Dispense
    │
    ▼
 Occupancy check (pellet sensor)
    │
    ├─ plate occupied ─► FeedSkipped ─► Raising ──────────────┐
    │                                                         │
    └─ plate empty ─► Seeking (conditional)                   │
                              │                               │
                              ▼                               │
                          Lowering                            │
                              │                               │
                              ▼                               │
                           Loading                            │
                              │                               │
              pellet sensor confirms                          │
                              │                               │
                              ▼                               │
                           OnPlate                            │
                              │                               │
                              ▼                               │
                           Raising                            │
                              │                               │
  pellet sensor clears ─► Fault (PelletLost)                  │
                              │                               │
                              ▼                               │
                            Loaded ◄──────────────────────────┘
                              │
        ┌─ dome lifts ────────┤──► DomeOpened
        │                     │
        └─ dome held open ────┤──► DomeOpenWarning
                              │
        pellet sensor clears ─┴──► PelletTaken ─► Idle
```

**Occupancy check.** On `Dispense` the node reads the pellet sensor first. If a pellet is already on the
plate it does not lower and does not run the feed wheel: it reports `FeedSkipped` and raises what is there
(or stays `Loaded` if the plate is already elevated). An occupied plate sitting at the load sensor never made
the grab descent, so its raise is shortened by `kDefaultGrabSteps` to finish at the same top height.
A node never stacks a second pellet on an occupied plate.

**Seeking.** This phase is conditional. It runs when the load sensor is asserted (plate at the load
position) or when the node already knows the plate is at drop depth (`belowLoad_`). Entering this state
emits the `Seeking` phase event (`0x0D`).

When Seeking starts on an asserted load sensor, M2 raises until the beam clears **or**
`kDefaultSeekAwaySteps` elapse, whichever comes first. A fixed `kDefaultSeekAwaySteps` raise (sensor
ignored) is used only for the known drop-depth case, where the beam is already clear. Seeking is never
started from an unknown height with a clear sensor — that path drove the plate into the stop after a
`PelletLost` recover.

**Lowering.** Only when the plate is empty. M2 approaches until the load position sensor asserts, budgeted by
`kDefaultLowerSteps`. Entering this state emits `Lowering` (`0x07`) independently of `Seeking`; the two phases
are not folded together. M2 then keeps going down a further `kDefaultGrabSteps` to the
**drop position**, ignoring the sensor for that stretch. Both parts are budgeted by `kDefaultLowerTimeoutMs`.

Whether to seek away is decided by the load sensor and the tracked drop-depth flag together. The sensor alone
cannot distinguish drop depth from the elevated position (both read clear). The node marks itself at-or-below
the load position when the grab descent starts, and clears that only when a raise clears the load sensor or
completes. After `recover()`, an asserted sensor re-affirms at-load; a clear sensor leaves the flag unchanged
so a plate left at drop depth is not mistaken for elevated. An approach that spends its whole budget without
seeing the sensor may back off once only when it is safe to raise (sensor asserted, or `belowLoad_` already
true); otherwise it faults with `ActuatorTimeout`.

**Loading.** With the plate at the drop position, M1 turns the pellet wheel at half the commanded speed
(`kDefaultFeedSpeedScale`) until the pellet sensor asserts. The first sighting stops the wheel immediately, so
it cannot follow with a second pellet, and starts a `kPelletLoadConfirmMs` hold. The pellet must keep the beam
broken for that whole window to count: a fragment tumbling past clears the beam early, and the wheel simply
resumes loading within the same budget. Once the window elapses the node reports `OnPlate` and begins the
raise in the same tick. If no pellet is confirmed within `kDefaultFeedTimeoutMs` — an empty hopper or a wheel
jam — the node faults with `FeedTimeout` (refill the hopper).

**Raising.** M2 lifts the plate by `kDefaultRaiseSteps` from the drop position — the grab descent back plus
the top height above the load sensor. Two checks run during travel:
the load position sensor must clear within `kLoadClearOnRaiseMs` (otherwise `Jam`), and the pellet sensor must
stay asserted. A pellet that falls off in transit clears the sensor for `kPelletLostMs` and faults with
`PelletLost`, so an empty plate never enters `Loaded`. If the raise travel itself exceeds
`kDefaultRaiseTimeoutMs`, the fault is `ActuatorTimeout` (sensor or M2 motor).

**Loaded.** The plate is at the top and the pellet is ready for the mouse. The node stays here, watching two things:

- Every dome lift reports `DomeOpened`. There is no suppression window — one event per bout, and the
spring return guarantees each bout is a distinct edge.
- A dome left open for `kDomeOpenWarnMs` reports `DomeOpenWarning` once per bout. This is a warning, not a
fault: the node keeps operating.

**Taken.** When the pellet sensor clears for `kPelletTakenConfirmMs`, the pellet is gone. The node reports
`PelletTaken` and returns to `Idle` — the cycle is complete. The confirm window rejects momentary sensor
flicker as the mouse reaches past the beam.

A node never reloads on its own. Deciding when the next `Dispense` goes out belongs to the base station or the
running experiment.

## No-feed dispense

`DispenseNoFeed` runs the identical motion as `Dispense` — same lowering, same seek-away/approach, same grab
descent to the drop position, same raise — but M1 never turns. Instead the node holds at the drop position for a
commanded dwell (default `kDefaultNoFeedDwellMs` = 6 s, matching the time a fed cycle typically spends loading),
then raises exactly as a fed cycle does. The animal finds an empty plate at the top. This exists so a module can
be run through the motions — including the acoustic and vibration signature — without delivering a pellet, e.g.
so a two-armed choice task can activate every arm each trial and the animal can't use sound alone to find the
baited one.

```
 DispenseNoFeed
    │
    ▼
 Occupancy check (pellet sensor)
    │
    ├─ plate occupied ─► FeedSkipped (real pellet — same as Dispense, never silently discarded)
    │
    └─ plate empty ─► Lowering ─► Dwelling (M1 idle, dwell_ms) ─► Raising ─► NoFeedPresented
                                                                                  │
                                                    ┌─ dome lifts ──────► DomeOpened (pellet_present=false)
                                                    │
                                                    └─ dome held open ──► DomeOpenWarning

                        No PelletTaken is ever emitted — the cycle stays Loaded until the next
                        Dispense / DispenseNoFeed command, or Recover.
```

An occupied plate is never silently swapped for empty: exactly as with `Dispense`, occupancy is checked first,
and a plate that already holds a pellet is presented honestly (`FeedSkipped`) rather than run through the
no-feed path. The pellet-lost guard that runs during a fed raise is skipped for a no-feed raise (an empty plate
always has the pellet sensor clear, so that guard would otherwise fault every cycle); the load-sensor jam guard
still applies, since it is about motion, not the pellet.

## Events

Node → base on CAN ID `0x300 + nodeId`. Byte 0 is the event code.


| Code   | Event             | Extra payload              | Meaning                                                                 |
| ------ | ----------------- | -------------------------- | ----------------------------------------------------------------------- |
| `0x01` | `OnPlate`         | count LE16                 | The pellet sensor confirmed during `Loading`; the raise is starting     |
| `0x02` | `Loaded`          | count LE16                 | The plate reached the top; the pellet is ready for the mouse            |
| `0x03` | `DomeOpened`      | count LE16, pellet present | The dome was lifted while the dispenser was `Loaded`                    |
| `0x04` | `Fault`           | `ServiceStatus`            | Motion or delivery failure; sticky until `Recover`                      |
| `0x05` | `Pong`            | —                          | Reply to `Ping`                                                         |
| `0x06` | `InputChanged`    | input id, active           | A sensor changed state                                                  |
| `0x07` | `Lowering`        | count LE16                 | Phase entered: approaching the load position                            |
| `0x08` | `Loading`         | count LE16                 | Phase entered: pellet wheel turning                                     |
| `0x09` | `Raising`         | count LE16                 | Phase entered: lifting the plate                                        |
| `0x0A` | `DomeOpenWarning` | count LE16                 | The dome has been open for `kDomeOpenWarnMs`                            |
| `0x0B` | `PelletTaken`     | count LE16, dome open      | The pellet left the plate; retrieval confirmed                          |
| `0x0C` | `FeedSkipped`     | count LE16                 | A dispense arrived with the plate occupied; feed and lower were skipped |
| `0x0D` | `Seeking`         | count LE16                 | Conditional phase: clear load sensor (or step cap) before `Lowering`    |
| `0x0E` | `NoFeedPresented` | count LE16 (unchanged)     | A no-feed raise finished; empty plate at the top. `count` does NOT increment |
| `0x0F` | `Dwelling`        | count LE16                 | Phase entered: holding at the drop position, M1 idle (no-feed cycle)   |
| `0x10` | `PresenceCalResult` | ok(1), threshold LE32, samples LE16 | Response to a `CalibratePresence` command — see below      |


`count` is the running total of `Loaded` milestones from that node — `NoFeedPresented` deliberately does not
advance it, so the base station can tell an unrewarded cycle from a real delivery just by watching the count.

**Presence recalibration.** `CanCmd::CalibratePresence` (`0x09`, no payload, broadcast-friendly) starts a fresh
5 s idle-pad capture on the presence sensor — the same action as a short `PIN_BTN` click. The node replies with
`PresenceCalResult`: `ok=1` and the new `threshold` (uint32 LE) on success, or `ok=0` with the *unchanged*
threshold if the capture saw fewer than `kPresenceCalMinSamples` (10) samples. Calibrating with an animal on the
pad sets a threshold above real presence readings, so the cage must be empty for the whole capture — see
[HARDCODED_VALUES.md](HARDCODED_VALUES.md#presence-detection-presenceservice) for the full timing table.

The context byte on dome and take events separates intent from accident. `DomeOpened` carries whether a
pellet was on the plate at the moment of the lift, so an opening with an empty plate is recognizable as
exploration rather than retrieval. `PelletTaken` carries whether the dome was open when the plate emptied: open
means a normal retrieval, closed means the pellet left without an access and the mechanism deserves a look.

`InputChanged` payloads are `[0x06, inputId, active]` with input IDs pellet sensor = `1`, load position = `2`,
dome = `3`, mouse presence = `4`. These fire on every debounced edge so the base station sees sensor activity
immediately; heartbeats are the periodic recovery snapshot.

## Faults

A fault halts both motors, latches a status code, lights the status LED solid, and holds the node in `Fault`
until it receives `Recover`.


| Code               | Cause                                                                                                      |
| ------------------ | ---------------------------------------------------------------------------------------------------------- |
| `FeedTimeout`      | M1 never confirmed a pellet on the plate — hopper empty / refill pellets                                   |
| `ActuatorTimeout`  | M2 never reached its target (seek / lower / raise) — load-sensor issue or motor stuck                      |
| `Jam`              | The load position sensor did not clear after the raise started; the plate is obstructed                    |
| `PelletLost`       | The pellet left the plate during the raise                                                                 |




## Heartbeat snapshot

Node → base on CAN ID `0x200 + nodeId`, sent every `kDefaultHeartbeatIntervalMs` (runtime-configurable via
`SetConfig`).


| Byte | Content                                                                       |
| ---- | ----------------------------------------------------------------------------- |
| 0    | Dispense state                                                                |
| 1–2  | Loaded milestone count (LE16)                                                 |
| 3    | Mouse presence (capacitive mouse-presence pad)                                |
| 4    | Sensor bits: `bit0` pellet present, `bit1` at load position, `bit2` dome open |
| 5    | Fault code                                                                    |
| 6–7  | Pellets taken (LE16)                                                          |


Carrying both counts means a node that reconnects mid-session reports delivery and consumption together, with
no need to replay the event log.

## What the base station can conclude

The pellet sensor turns several inferences into measurements:

- **Consumption, not delivery.** `Loaded` counts what the hardware offered; `PelletTaken` counts what
the mouse actually took. The gap between the two counts is the untouched pellets.
- **Unrewarded openings.** A dome bout that closes with the pellet sensor still asserted is an access that did
not end in retrieval — a distinct behavioral event from a successful one.
- **Retrieval latency.** The interval from `Loaded` to `PelletTaken` is a per-pellet measure of how
quickly the mouse engaged.
- **Consumption-paced reloading.** An experiment can wait for `PelletTaken` before dispensing again, so the
session follows the mouse rather than a fixed schedule. The built-in free-feeding template works this way.
- **Abandonment.** A pellet that reaches `Loaded` and is never taken is visible directly, which lets a task
stop reloading in front of a disengaged mouse instead of accumulating stale pellets.

