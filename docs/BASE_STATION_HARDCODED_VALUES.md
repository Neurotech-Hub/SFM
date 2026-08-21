# Base station hardcoded / tunable values

Living note of Python timings and parameter defaults used by the GUI, experiment
templates, and behavior reports under `tools/dev_gui/`.
**Update this file when you change a default.** Source of truth remains the code;
this is the checklist.

Node firmware constants (dispense motion, sensor confirm windows, CAN opcodes)
live in [HARDCODED_VALUES.md](HARDCODED_VALUES.md). GUI-exposed experiment
parameters can be overridden at Start without editing code; values here are the
defaults those forms ship with, plus the ones that are *not* on a form.

---

## High-priority (likely to change)


| Value      | Constant / key              | Location | Notes |
| ---------- | --------------------------- | -------- | ----- |
| **90 s**   | `PRESENTATION_TIMEOUT_S`    | `experiment/kit.py` | Mechanical safeguard on the feed/mimic raise-sync gate. Must sit above firmware's 30 s feed timeout plus travel. Not a behavioral ITI |
| **120 s**  | `STALL_WARN_S`              | `experiment/script.py` | Repeat interval for `script_stalled` while a script wait has no timeout (presence-clear, plate-clear, take wait) |
| **60 s**   | `DEFAULT_HEARTBEAT_INTERVAL_S` | `app.py` / `node_registry.py` | Interval the GUI pushes to nodes via `SetConfig`. Firmware boot default is 5 s until this lands |
| **3×**     | `HEARTBEAT_OFFLINE_MULTIPLIER` | `node_registry.py` | Seconds without a heartbeat before OFFLINE = interval × 3 (180 s at the 60 s default) |
| **30 s**   | `DISCOVERY_IDLE_TIMEOUT_S`  | `discovery_manager.py` | No new ANNOUNCE/REJOIN for this long → discovery complete |
| **1 s**    | `iti_quiet_s` (all templates) | `experiments/*.json` | Presence must stay clear this long before the next trial when Advance = `presence_clear` |


---

## CAN, discovery, heartbeat


| Value | Constant | Location | Notes |
| ----- | -------- | -------- | ----- |
| 250 kbps | `bitrate` default | `run.py`, `can_manager.py`, `deploy/80-can.network` | Must match firmware TWAI and the HAT overlay |
| 2 | `--nodes` default | `run.py` | Expected node count on the setup screen |
| `can0` | `--interface` default | `run.py` | SocketCAN device; use `vcan0` for the simulator |
| 30 s | `DISCOVERY_IDLE_TIMEOUT_S` | `discovery_manager.py` | Idle window after the last ANNOUNCE/REJOIN |
| 3 s | `MAC_PING_RETRY_S` | `app.py` | Minimum gap between MAC-resolution Pings to the same node |
| 60 s | `DEFAULT_HEARTBEAT_INTERVAL_S` | `app.py` | Pushed to every node after discovery |
| 3× interval | `HEARTBEAT_OFFLINE_MULTIPLIER` | `node_registry.py` | GUI tile + experiment `is_online` share this |
| 1 s | `STALE_CHECK_INTERVAL` | `app.py` | How often the GUI sweeps nodes for missed heartbeats |
| 100 ms | `can.Bus.recv` poll | `can_manager.py` | RX thread poll; not a protocol timeout |
| 20 ms | `can.Bus.send` timeout | `can_manager.py` | Per-frame TX wait |


---

## HAT I/O and camera sync

Pins are BCM numbering, fixed by the custom CAN HAT. See
[deploy/README.md](../tools/dev_gui/deploy/README.md).


| Value | Constant | Location | Notes |
| ----- | -------- | -------- | ----- |
| GPIO 12 / 13 / 6 | `PIN_BNC_IN1` / `IN2` / `OUT` | `io_manager.py` | BNC IN inverted Schmitt; BNC OUT non-inverting + LED |
| GPIO 3 | `PIN_BTN` | `io_manager.py` | Front-panel button |
| GPIO 27 | `PIN_AEO` | `io_manager.py` | Address Enable Out to the first node in the daisy chain |
| 50 ms | `BUTTON_DEBOUNCE_S` | `io_manager.py` | Base-station button debounce |
| 500 ms | `DEFAULT_SYNC_FLASH_MS` | `protocol.py` | Status-LED hold + BNC OUT pulse at session start (`CanCmd.SyncFlash`). Clamped 50–5000 ms |
| 200 µs | `_PULSE_SPIN_MARGIN_S` | `io_manager.py` | Busy-spin tail of a BNC OUT pulse for edge timing |


---

## Presence (base-station side)

The threshold itself is calibrated and stored on the node. The GUI only ships
the multiplier used in `threshold = mean + factor × σ`.


| Value | Constant | Location | Notes |
| ----- | -------- | -------- | ----- |
| 3.0 | `DEFAULT_PRESENCE_FACTOR` | `dev_settings.py` | Dev-settings default; persisted under `~/.sfm/dev_settings.json` |
| 0.1–100 | `PRESENCE_FACTOR_MIN` / `MAX` | `protocol.py` | Clamp on `SetConfig` payload; mirrors firmware |


---

## Experiment engine

Shared by every `@exp.script` template (`kit.synchronized_cycle`,
`kit.next_trial_wait`, `kit.after_advance`).


| Value | Constant | Location | Notes |
| ----- | -------- | -------- | ----- |
| 90 s | `PRESENTATION_TIMEOUT_S` | `kit.py` | Raise-sync gate; on timeout the unpresented arm is Recover'd |
| 120 s | `STALL_WARN_S` | `script.py` | `script_stalled` cadence for unbounded waits |
| 64 | `MAX_ADVANCES_PER_TICK` | `script.py` | Cap on consecutive yield-resolves in one `step()` so a tight loop cannot spin the GUI |
| 100 ms | `_poll` / `_run` reschedule | `kit.after_advance` | Presence-clear / session-pause poll interval (free-feeding reloads) |
| 5 s | `resolve_advance` `default_delay_s` | `kit.py` | Fallback ITI if a template does not pass its own default |
| [1, 2, 3] | `kit.session` default nodes | `kit.py` | Used when a headless `build()` is called with `nodes=None`. GUI always passes the live roster |


Pellet-taken and presence-clear waits have **no timeout by design**. An animal
that parks on the pad stalls until `end_after` (duration / pellet cap) or Stop.
That stall is visible via `script_stalled`.

---

## Experiment parameter defaults

These are the JSON form defaults (`tools/dev_gui/experiments/*.json`). Changing
the JSON changes what the GUI Start form shows; Python `build()` kwargs are the
headless fallbacks (kept in sync).


### Shared across templates

| Parameter | Default | Notes |
| --------- | ------- | ----- |
| `next_trial_wait` | `presence_clear` | `fixed_delay` / `presence_clear`; Fixed+Random and Probability also offer `bnc` |
| `iti_quiet_s` | 1.0 | Settle time once presence is clear |
| `minutes` | 0 | 0 = no duration limit |
| `max_pellets` | 0 | 0 = no cap |


### Free feeding (`experiments/free_feeding.json`)

| Parameter | Default | Notes |
| --------- | ------- | ----- |
| `fixed_delay_s` | **30 s** | Reload wait after PelletTaken when Advance = `fixed_delay`. Python fallback `default_delay_s=30.0` |
| `mimic` | n/a | Every node reloads independently; no mimic arm |


### Two-armed bandit (`experiments/two_armed_bandit.json`)

| Parameter | Default | Notes |
| --------- | ------- | ----- |
| `block_size` | 50 | Trials per rich/lean block before the arms flip |
| `p_high` | 0.9 | Rich-arm delivery probability (lean = `1 − p_high`) |
| `mimic` | true | Empty arm runs `DispenseNoFeed` so both plates move |
| `fixed_delay_s` | **5 s** | Python fallback `default_delay_s=5.0` |


### Probability delivery (`experiments/probability_delivery.json`)

| Parameter | Default | Notes |
| --------- | ------- | ----- |
| `fixed_delay_s` | **10 s** | Python fallback `default_delay_s=10.0` |
| `mimic` | true | Non-winning weight > 0 nodes run `DispenseNoFeed` |
| `bnc_channel` | 0 | Used when Advance = `bnc` (0 = BNC IN 1) |
| `probabilities` | empty | Empty → uniform over session nodes |


### Fixed + random (`experiments/fixed_and_random.json`)

| Parameter | Default | Notes |
| --------- | ------- | ----- |
| `fixed_delay_s` | **10 s** | Python fallback `default_delay_s=10.0` |
| `random_prob` | 0.5 | Chance that one Random node feeds this cycle |
| `node_roles` default | `random` | Per-node dropdown; Off / Fixed / Random |
| `mimic` | true | Non-feeding Random nodes run `DispenseNoFeed` |
| `bnc_channel` | 0 | Used when Advance = `bnc` |


---

## Logging and storage


| Value | Where | Notes |
| ----- | ----- | ----- |
| `~/sfm_logs` | `storage.py` | Fallback when no writable external drive is mounted under `/media/<user>/*` |
| 1000 | `LogManager(max_entries=…)` | In-memory ring only; CSV is unbounded |
| 64 chars | `sanitize_session_name` | Session-name filesystem cap |
| `~/.sfm/mac_id_registry.json` | `mac_id_registry.py` | Persistent MAC ↔ Node ID map |
| `~/.sfm/dev_settings.json` | `dev_settings.py` | Presence factor, etc. |
| `~/.sfm/report_settings.json` | `report/naming.py` | Session-name parse patterns |


---

## Behavior reports

Defaults used when a report design does not override them in `reports/*.json`.
These shape analysis, not the live session.


| Value | Constant / option | Location | Notes |
| ----- | ----------------- | -------- | ----- |
| 2 s | `visit_grace_s` | `report/analyses/bandit.py` | Extra window after trial end when attributing first visit / first dome |
| 60 s | `default_trial_window_s` | `report/analyses/bandit.py` | If `bandit_trial_end` is missing, look this far past `arm_presented` |
| 5 min | `meal_gap_s` = 300 | `report/analyses/free_feeding.py`, `reports/free_feeding.json` | Split free-feeding takes into meals when the inter-take gap exceeds this. Modeling choice, not a measured constant |
| 0.25 s | `dedup_window_s` | `report/metrics.py` `dome_bouts` | Collapse `DomeOpened` milestone + InputChanged edge for the same physical open |
| 600 s | `window_s` | `reports/*.json` timeline | Raster detail-window length (free feeding uses 900 s) |
| 5 / 15 | `pre` / `post` | `reports/two_armed_bandit.json` | Reversal-curve trials before/after a block flip |
| 5 | `rolling_window` | `reports/two_armed_bandit.json` | Block-curve smoothing window (trials) |
| 8 | `_MIN_TRIALS_FOR_CURVE` | `report/sections/bandit.py` | Skip choice/reversal charts below this many analyzed trials |
| 3 / 15 | `streak` / `max_post` | `report/analyses/bandit.py` `trials_to_criterion` | Criterion = 3 consecutive new-rich choices, searched within 15 post-flip trials |
| `first_visit` | `choice_source` | `reports/two_armed_bandit.json` | Choice = first MousePresence after both arms presented (not the take) |


---

## How to change

1. Edit the named constant, JSON `"default"`, or function kwarg listed above.
2. If the value is a GUI form default, keep `experiments/<name>.json` and the
   Python `build()` / `default_delay_s` fallback in agreement.
3. Update the corresponding row in this document.
4. Prefer a JSON / GUI parameter over a new magic number whenever the value is
   session- or site-specific.
