# Analysis Guide

A reference for designing your own analysis on top of `sfm_analysis`: the
nouns this package uses (bout, cycle, ready, censored, …), what's actually
in a log, what every derived field means (including its `None` semantics),
and a worked example that goes from a question to a tested answer.

This is the **reference** half of the documentation. The
[README](../README.md) is the **task-oriented** half — install, the CLI,
quick-start snippets, and a one-liner cookbook. Read that first if you just
want to run a report or answer a question that's already in the cookbook;
come here when you need to know what a term means, exactly what a field
means, or you're building something the cookbook doesn't cover.

## Contents

- [Vocabulary](#vocabulary)
1. [Which layer to work at](#1-which-layer-to-work-at)
2. [The log files](#2-the-log-files)
3. [Event vocabulary](#3-event-vocabulary)
4. [Session parameters](#4-session-parameters)
5. [Derived metrics reference](#5-derived-metrics-reference)
6. [Tidy-table columns](#6-tidy-table-columns)
7. [Time, timezone, and time-of-day](#7-time-timezone-and-time-of-day)
8. [Designing your own analysis: a worked example](#8-designing-your-own-analysis-a-worked-example)
9. [Turning an analysis into a report section](#9-turning-an-analysis-into-a-report-section)
   - [File layout](#file-layout)
   - [Complete example (custom experiment → custom design)](#complete-example-custom-experiment--custom-design)

## Vocabulary

The rest of this guide names objects (`Bout`, `Cycle`, `ready_t`, …) as if
they were already obvious. They are not. This section is the map from the
physical feeder to those objects, so you can pick the right one before
writing code. Field-by-field `None` semantics live in
[§5](#5-derived-metrics-reference); the sensing model the firmware actually
runs is [firmware/docs/DISPENSE_CYCLE.md](../../../firmware/docs/DISPENSE_CYCLE.md).

### The apparatus

- **Node** — one feeder on the CAN bus. `node_id` is which one. `0` is
  broadcast / session-scope, not a real feeder — skip it when averaging
  per-animal numbers. Configured nodes for a run are `run.nodes`
  (`session.split_runs`).
- **Plate** — the pellet platform. A latching pellet sensor reports
  occupancy from the moment a pellet lands until it leaves. The node will
  not stack a second pellet on an occupied plate (`FeedSkipped`).
- **Dome** — the spring-returned lid over the plate. Every access is a
  clean lift-and-release, so one physical opening is one bout. Logged as
  `Dome Opened` / `Dome closed` (see the `"Dome Opened"` trap in
  [§3](#3-event-vocabulary) before pairing those names yourself).
- **Presence pad** — a capacitive pad on the node, independent of the
  pellet sensor. Firmware thresholds it (`raw > threshold`) with a 100 ms
  debounce, so one approach produces one trigger and one clear. Logged as
  `MousePresence Detected` / `MousePresence Cleared`. This is what
  `metrics.presence_bouts` pairs.
- **BNC** — a photogate / TTL input on the *base station*, not a node
  sensor. Rows have `source == "BNC"`. Some experiment templates wait on a
  rising edge to start a trial.

### Units of recording

- **Session** — one named CSV (`session` column). The file you pass to
  `load_session`.
- **Run** — one open/stop of that session. Reopening the same session name
  appends to the same file and increments `run_id`. A count or interval
  that crosses a run boundary is meaningless.
  `session.split_runs` is the one function that groups rows this way;
  always work from its `RunData`, not the raw `LogRow` list. See also
  [README — Five things to know, #1](../README.md#five-things-to-know-before-trusting-a-number).
- **Trial** — an experiment-template boundary (choice, block, …), not a
  dispense. The CSV `trial` column is a convenience copy; the
  authoritative marker is the `trial` EXP event. Use `trials_table()` for
  those markers, or `report.analyses.*` for template-specific structure.

`.t` on a `RunData` row is run-relative seconds, recomputed from
`timestamp_ms` by `split_runs`. That is the clock for every latency and
duration below. Never read the raw CSV `elapsed_s` column — it resets
when a session is reopened ([README trap #2](../README.md#five-things-to-know-before-trusting-a-number)).

### Units of behavior

A **dispense cycle** is apparatus-driven. A **bout** is animal-driven (or
a fault interval). Both are intervals on the same `.t` axis; they are
not the same object.

- **Dispense cycle** (`metrics.Cycle`, produced by `metrics.build_cycles`)
  — one attempt to present a pellet (or an empty plate) and wait for an
  outcome. Opens on a `Dispense` / `DispenseNoFeed` command that actually
  ran; closes on `Pellet Taken`, the next dispense command for that node,
  or run end. A broadcast command (`node_id == 0`) opens one pending
  cycle per node in `run.nodes`.
- **Ready** (`ready_t`) — the pellet (or empty plate) is at the top and
  available. Set by `Loaded` (real pellet) or `NoFeedPresented` (no-feed
  cycle). This is the zero point for approach, dome, and retrieval
  latency. A cycle with `ready_t is None` never presented — dispense
  failed or was still in progress when the run ended. Do not treat it as
  latency 0.
- **Take** (`taken_t`) — the pellet left the plate (`Pellet Taken`). The
  cycle's outcome, not a bout.
- **No-feed cycle** (`fed is False`) — the node ran the same motion with
  the feed wheel off, so the plate rises empty. Used so a choice task can
  move every arm without the sound of the wheel giving the baited one
  away. There is nothing to take; `taken_t` stays `None`.
- **Bout** (`metrics.Bout`, produced by `metrics.bouts`) — one continuous
  episode from a paired open/close edge, per node, in time order.
  `t0` is the open, `t1` is the close, `dur = t1 - t0`. Presence bouts
  (`Detected`/`Cleared`), dome bouts (`Opened`/`closed`), and fault
  intervals all share this shape; `bouts_table()` unifies them under
  `kind` (`"presence"` | `"dome"` | `"fault"`).
- **Censored** — still open when the run ended, so the outcome was never
  observed. `Bout.dur` / a missing `taken_t` is `None` on purpose, never
  `0`. Filter censored rows out of an average; do not fill them.
  [README trap #4](../README.md#five-things-to-know-before-trusting-a-number).

The diagram below is the first complete cycle of the bundled demo session
(`Bandit_test_01`, run 2, node 1) — the same file `sfm-report --demo`
uses. The cycle is the apparatus interval; the two bouts are the animal
on top of it. The take happens while the dome is still up; presence
clears a second later.

```
t (s)     20.0              30.2         37.8   39.9          47.1   48.3
          |                 |            |      |             |      |
cycle     [==== dispense ===][======= pellet available =======] take
          cmd_t             ready_t                       taken_t = end_t
presence                                 [======= at feeder =========]
                                         Detected                    Cleared
dome                                            [===== lid lifted ===]
                                                Opened          closed
```

That same cycle as a `cycles_table()` row (phase timestamps omitted):

```python
{
  "session": "Bandit_test_01", "run_id": 2, "node": 1, "index": 0,
  "fed": True,
  "cmd_t": 20.017, "ready_t": 30.235,
  "first_presence_t": 37.802, "first_dome_t": 39.918, "taken_t": 47.069,
  "end_t": 47.069, "censored": False,
  "cycle_duration": 10.218,          # ready_t - cmd_t
  "approach_latency": 7.567,         # first_presence_t - ready_t
  "dome_latency": 9.683,             # first_dome_t - ready_t
  "handling_time": 7.151,            # taken_t - first_dome_t
  "retrieval_latency": 16.834,       # taken_t - ready_t
}
```

And the overlapping presence bout from `bouts_table()`:

```python
{
  "session": "Bandit_test_01", "run_id": 2, "kind": "presence", "node": 1,
  "t0": 37.802, "t1": 48.268, "dur": 10.466,
  "censored": False, "code": None, "inferred_close": None,
}
```

Print them yourself with `load_session(str(DEMO_SESSION_PATH))` then
`s.cycles_table()` / `s.bouts_table()` — same grounding rule as
[§8](#8-designing-your-own-analysis-a-worked-example).

### Derived numbers

All four latencies are `None` unless every timestamp they need is present
**and** in causal order (a take that precedes ready, e.g. from a stray
sensor bounce, yields `None` rather than a negative number).

| Name | = | Zero point | Question it answers |
|---|---|---|---|
| `cycle_duration` | `ready_t - cmd_t` | command | How long did the apparatus take to present? |
| `approach_latency` | `first_presence_t - ready_t` | ready | How long until the mouse arrived? |
| `dome_latency` | `first_dome_t - ready_t` | ready | How long until the lid lifted? |
| `handling_time` | `taken_t - first_dome_t` | first dome | How long from lid-up until the pellet left? |
| `retrieval_latency` | `taken_t - ready_t` | ready | How long from available until taken? |

- **Funnel** (`metrics.InteractionFunnel`) — per-node conversion counts
  built from cycles that reached ready: presented → approached (presence
  while ready) → dome opened → taken, plus `approach_without_dome` /
  `dome_without_take`. Use it for "what fraction of presentations got a
  take", not for durations.
- **Take rate** (`PelletAccounting.take_rate`) —
  `taken_total / presented_total`, `None` if nothing was presented.
- **Ledger vs seen counts** — `presented` / `taken` count rows *actually
  in this log*. `presented_total` / `taken_total` use the base-station
  ledger's gap-corrected total when available, so they also count pellets
  whose own event frame never arrived. Prefer `*_total`.
  [README trap #5](../README.md#five-things-to-know-before-trusting-a-number).
- **Actogram** (`metrics.activity_by_day` / `timeline.actogram`) — one row
  per rig-local calendar day, ticks at time-of-day of chosen CAN events
  (default: presence onsets). Renders only when a run spans 2+ distinct
  days. No light/dark shading: the rig does not record the facility's
  light schedule.
- **`BoutIssues`** (`orphan_closes`, `duplicate_opens`, `censored`) —
  unpaired edges `metrics.bouts` refused to silently drop. A nonzero
  value is a data-quality signal, not an internal counter. Surface it
  before trusting bout durations.

### Which object answers the question

| Question | Table / object | Field |
|---|---|---|
| How long was the mouse at the feeder? | `bouts_table()`, `kind=="presence"` | `dur` |
| How long was the dome open? | `bouts_table()`, `kind=="dome"` | `dur` |
| How fast did the mouse retrieve the pellet? | `cycles_table()` | `retrieval_latency` |
| How long from pellet-available to first approach? | `cycles_table()` | `approach_latency` |
| How long from lid-up until the pellet left? | `cycles_table()` | `handling_time` |
| Did it approach but never open the dome? | `cycles_table()` | `approached_without_dome` |
| Was this cycle still open when the run ended? | `cycles_table()` | `censored` |
| How long was a node in a fault? | `bouts_table()`, `kind=="fault"` | `dur` |
| How many pellets were presented (best count)? | `metrics.pellets` | `presented_total` |
| What fraction of presentations were taken? | `metrics.pellets` | `take_rate` |
| When in the day did activity happen? | `metrics.activity_by_day` | `times` |
| What experiment-engine events did this template log? | `events_table(source="EXP")` | `event_name` |

## 1. Which layer to work at

Every layer below is built directly on the one before it — nothing is
hidden, and dropping down a layer is always available, but the tidy tables
answer most real questions with the least code.

| Layer | Type | Gets you | Module |
|---|---|---|---|
| CSV | text | The recorded truth. Every row VFM logged. | `report/demo/*.csv`, or your own session file |
| `LogRow` | dataclass | One row, typed: `fields_json` decoded to `.fields`, hex decoded to `.raw_data`, `.t` filled in | `report.loader` |
| `RunData` | dataclass | Rows scoped to one `(session, run_id)`, correctly time-ordered, `.t` recomputed from `timestamp_ms` | `report.session` |
| `RunMetrics` | dataclass bundle | Reconstructed cycles, bouts, faults, pellet accounting — the numbers, computed once | `report.metrics` |
| Tidy tables | `list[dict]` | `RunMetrics` flattened to one row per cycle/bout/event, ready for `pandas.DataFrame` | `sfm_analysis.analysis.tables` |

**Start at tidy tables.** `s.cycles_table()` / `s.bouts_table()` cover most
questions with zero knowledge of the layers underneath. Drop to `RunMetrics`
(`s.metrics_for()`) when you need a field a table doesn't expose yet, or to
raw rows (`s.events_table()`, or `run.rows` directly) for something no
existing metric computes at all — see [§5](#5-derived-metrics-reference) and
[§6](#6-tidy-table-columns) for what's already there before you reach for
raw rows.

## 2. The log files

A session is one CSV in the **unified 15-column schema**
(`sfm_analysis.logs.CSV_HEADER`), plus an optional sibling
`<name>_heartbeats.csv` in the *same* 15-column schema, filtered to
`frame_type == "HEARTBEAT"` rows — there is only one CSV schema, not two;
the heartbeat file is a separate sink for a high-frequency frame type so it
doesn't bloat the main log.

| Column | Holds | Notes |
|---|---|---|
| `timestamp_iso` | rig-local wall-clock string, e.g. `2025-06-01T14:32:07.123` | Written with `datetime.fromtimestamp()` **on the rig**, so it's already correct local time — see [§7](#7-time-timezone-and-time-of-day) |
| `timestamp_ms` | epoch milliseconds | Used to recompute `.t` on load; don't trust it for wall-clock display without knowing the rig's real-world offset |
| `elapsed_s` | seconds since the row's *originating process* started | **Never read this directly** — it resets across a reopened session. Use `RunData` rows' `.t` instead ([Vocabulary](#vocabulary); [README trap #2](../README.md#five-things-to-know-before-trusting-a-number)) |
| `session` | the session name | Groups rows into files; combined with `run_id` for run-scoping |
| `run_id` | increments each time a session is reopened | A single CSV can hold several runs — [session vs run](#units-of-recording); [README trap #1](../README.md#five-things-to-know-before-trusting-a-number) |
| `trial` | current trial number, `0` outside a trial | Convenience column; the authoritative trial boundary is the `trial` EXPERIMENT event |
| `source` | `CAN` \| `EXP` \| `BNC` \| `SYS` | `CAN` = a **CAN event**: a message on the communication bus all nodes share (node hardware). `EXP` = experiment-engine. `BNC` = base-station photogate/beam-break. `SYS` = base-station lifecycle |
| `direction` | `TX` \| `RX` \| `SYS` \| `LOCAL` | Bus direction for CAN frames; not meaningful for EXP rows |
| `node_id` | which node (`0` = broadcast / session-scope, not a real node) | |
| `frame_type` | `EVENT` \| `COMMAND` \| `HEARTBEAT` \| `PELLET_AUDIT` \| ... | What kind of frame this is, independent of `event_name` |
| `event_name` | the human-readable event/command name | See [§3](#3-event-vocabulary) |
| `raw_id_hex` | CAN arbitration ID, hex | Only meaningful for `source == "CAN"` |
| `raw_data_hex` | CAN payload bytes, hex | Decoded by `protocol.py` — heartbeats via `parse_heartbeat()`, events via their own per-type decoder |
| `fields_json` | JSON object of structured data | Decoded into `LogRow.fields`; `{}` for rows that carry none (mostly raw CAN frames) |
| `details` | free-text human note | Rare; mostly empty |

## 3. Event vocabulary

**CAN** (Controller Area Network) is the shared communication bus every feeder
node is wired onto. A **CAN event** is a frame a node posted on that bus —
`Loaded`, `Pellet Taken`, `Fault`, a sensor edge — as opposed to an
experiment-engine (`EXP`) row the base station invented.

### CAN events (`source == "CAN"`, `protocol.CanEvent`)

One node-hardware event per row, received on the CAN bus. `event_name` is the *display* name
(`protocol.CAN_EVENT_DISPLAY_NAME`), not the enum member name — use
`LogRow.can_event` when you need the underlying enum back (see the dome
trap below).

| `CanEvent` | Display name (`event_name`) | Payload (`raw_data`) |
|---|---|---|
| `OnPlate` (`0x01`) | `Pellet OnPlate` | — |
| `Loaded` (`0x02`) | `Loaded` | — |
| `DomeOpened` (`0x03`) | `Dome Opened` | count (LE16) + pellet_present |
| `Fault` (`0x04`) | `Fault: <code>` | `raw_data[1]` = `ServiceStatus` |
| `Pong` (`0x05`) | `Pong` | — |
| `InputChanged` (`0x06`) | varies (see below) | input id, active flag |
| `Lowering` (`0x07`) | `Lowering` | — |
| `Loading` (`0x08`) | `Loading` | — |
| `Raising` (`0x09`) | `Raising` | — |
| `DomeOpenWarning` (`0x0A`) | `DomeOpenWarning` | non-sticky, dome open >30s |
| `PelletTaken` (`0x0B`) | `Pellet Taken` | count (LE16) + dome_open flag |
| `FeedSkipped` (`0x0C`) | `FeedSkipped` | plate was occupied on Dispense |
| `Seeking` (`0x0D`) | `Seeking` | — |
| `NoFeedPresented` (`0x0E`) | `NoFeedPresented` | count (LE16), **not incremented** as a real pellet |
| `Dwelling` (`0x0F`) | `Dwelling` | holding at drop position |
| `PresenceCalResult` (`0x10`) | `PresenceCalResult` | ok flag, threshold (LE32), samples (LE16) |
| `ConfigApplied` (`0x11`) | `ConfigApplied` | config type, ok flag, value (LE32) |

**The `"Dome Opened"` trap.** `CanEvent.DomeOpened` (`0x03`, a real
milestone with a pellet count) and a renamed `InputChanged` (`0x06`) dome
edge both render as the *same* `event_name`, `"Dome Opened"`. Pairing
opens/closes by `event_name` alone double-counts every physical opening.
`metrics.dome_bouts()` already deduplicates this correctly (see its
`dedup_window_s` parameter) — use it, or `LogRow.can_event` if you're
working from raw rows directly.

**Presence/dome/load-position edges** also arrive as `InputChanged`
(`0x06`) but are *renamed* in the log for readability — e.g. `"MousePresence
Detected"` / `"MousePresence Cleared"` — via
`protocol.behavioral_input_log_name`. These renamed edges are the ones
`presence_bouts()` and the dome dedup consume; the raw `InputChanged` name
itself should rarely appear in a well-formed log.

### Experiment events (`source == "EXP"`)

Logged by the experiment engine (`ExperimentControl`, script runtime, and
per-template code), not the node hardware. Two groups:

**Core, present in every session regardless of experiment template:**

| `event_name` | Fires | `fields_json` carries |
|---|---|---|
| `session_start` | once, session open | `experiment`, `nodes`, `seed`, `utc_offset_s` (recent logs) |
| `<experiment>_start` | once, e.g. `two_armed_bandit_start` | template-specific config: node roles, block size, probabilities, ... |
| `trial` | once per trial boundary | `trial` (the trial number) |
| `session_end` | once, session close (if a clean close happened) | template-specific end summary |
| `script_stalled` | script runtime detects no forward progress | — |
| `fault` / `node_halted` | a node enters a fault state | `node`, `fault_code` (a `ServiceStatus` int) |
| `node_recovered` / `recovered` | a node's fault clears | `node` |
| `paused_for_fault` / `resumed_after_fault` | session-level pause/resume around a fault | `node` |
| `feed_skipped` | a dispense was vetoed (plate occupied) | `node` |
| `stop_requested` | operator/script requested stop | — |

**Template- and script-runtime-specific** (varies by experiment; not
exhaustive here — see below for how to discover them on your own file):
per-trial structure like `bandit_trial` / `bandit_trial_end` /
`bandit_trial_aborted` / `arm_presented` (two-armed bandit),
`probability_pick` / `random_dispense` (probability delivery),
`reload_dispense` (free feeding), plus script-runtime diagnostics
(`script_bad_yield`, `script_predicate_error`, `script_close_error`,
`script_advance_cap`, `timer_error`, `callback_error`,
`start_when_error`, `end_when_error`, `dispense_skipped_halted`,
`dispense_skipped_in_flight`, `broadcast_dispense_partial_pellet_present`,
`plates_clear`, `bnc_pulse`).

Rather than trusting this list to stay exhaustive, discover what a
*specific* file actually contains:

```python
from sfm_analysis.analysis import load_session

s = load_session("EXP-Test-02")
names = sorted({row["event_name"] for row in s.events_table(source="EXP")})
```

[`examples/analysis/exp_events_by_name.py`](../examples/analysis/exp_events_by_name.py)
runs that inventory against the bundled demo session (and is pinned in CI).
Copy it as the first script you write against a new custom template.

`report.metrics.APPARATUS_HEALTH_EVENTS` is the curated subset the report's
"Apparatus Health" section surfaces — a good starting list of the
diagnostic (as opposed to trial-structure) events worth watching.

## 4. Session parameters

Everything `session.split_runs` copies onto `RunData` from a run's
`session_start` / `<experiment>_start` / `session_end` rows:

| Field | From | Meaning |
|---|---|---|
| `run.experiment` | `session_start.fields["experiment"]` | The template name (`"two_armed_bandit"`, ...); `"unknown"` if absent |
| `run.nodes` | `session_start.fields["nodes"]` | Node IDs configured for this run |
| `run.seed` | `session_start.fields["seed"]` | RNG seed, if the template logged one; `None` otherwise |
| `run.utc_offset_s` | `session_start.fields["utc_offset_s"]` | Seconds east of UTC; `None` on logs recorded before this was captured — see [§7](#7-time-timezone-and-time-of-day) |
| `run.params` | `<experiment>_start.fields` | Whatever the template's start event logged — block size, probabilities, roles, ... |
| `run.end_fields` | `session_end.fields` | Template-specific end summary, `{}` if absent |
| `run.end_reason` | `session_end.fields.get("reason")`-ish | Why the session ended, when logged |
| `run.has_session_end` | whether a `session_end` row was found | `False` means the file ends mid-session (crash, power loss, GUI closed without Stop) |
| `run.notes` | data-quality notes `split_runs` itself generates | Surfaced verbatim; check these before trusting an edge case |

## 5. Derived metrics reference

Everything below lives on `RunMetrics` (`report.metrics.RunMetrics`,
returned by `compute_run_metrics(run)` — or `s.metrics_for()` at the
`analysis` layer). Field-by-field, with `None` semantics spelled out,
since a silent `None → 0` is the easiest way to quietly corrupt an average.

### `Cycle` (`metrics.cycles`) — one dispense-and-retrieve cycle

What a cycle *is* (and how it differs from a bout) is in
[Vocabulary](#vocabulary). Every timing metric in the report is a
difference between two `Cycle` timestamps.

| Field | Type | `None` means |
|---|---|---|
| `node`, `trial`, `index` | `int` | never `None` — `index` is 0-based within this node's run |
| `cmd_t` | `float?` | the cycle was *inferred* from a `Loaded`/phase event with no preceding command row (e.g. the very first cycle, whose command predates this run's window) |
| `fed` | `bool` | `True` = real pellet (`Dispense`), `False` = `DispenseNoFeed` |
| `lowering_t` / `loading_t` / `on_plate_t` / `raising_t` / `seeking_t` / `dwelling_t` | `float?` | that dispense phase was never logged for this cycle (not necessarily an error — some phases are skipped depending on state) |
| `ready_t` | `float?` | the cycle never reached `Loaded`/`NoFeedPresented` — dispense failed or was still in progress when the run ended |
| `first_presence_t` | `float?` | no presence detected while this cycle was open |
| `first_dome_t` | `float?` | dome never opened while this cycle was open |
| `taken_t` | `float?` | pellet never taken (censored, or a no-feed cycle) |
| `end_t` | `float` | when this cycle closed — by `Pellet Taken`, the next dispense command, or run end |
| `censored` | `bool` | `True` = still open when the run ended, no outcome observed — [Vocabulary](#vocabulary); [README trap #4](../README.md#five-things-to-know-before-trusting-a-number) |

Derived properties (all `None` unless every timestamp they need is present
**and** in causal order — a `taken_t` that precedes `ready_t`, e.g. from a
stray sensor bounce, yields `None` rather than a negative latency):

| Property | = |
|---|---|
| `cycle_duration` | `ready_t - cmd_t` |
| `approach_latency` | `first_presence_t - ready_t` |
| `dome_latency` | `first_dome_t - ready_t` |
| `handling_time` | `taken_t - first_dome_t` |
| `retrieval_latency` | `taken_t - ready_t` |
| `approached_without_dome` | `bool`: presence detected but dome never opened |
| `dome_without_take` | `bool`: dome opened but no take followed |

### `Bout` + `BoutIssues` (`metrics.presence`, `metrics.dome`)

A bout is one continuous episode from a paired open/close edge — see
[Vocabulary](#vocabulary). Fields: `node`, `t0`, `t1` (`None` if censored
— still open at run end), `censored: bool`, `.dur` property (`None` when
`t1` is `None`, never `0`).

`BoutIssues`: `orphan_closes` (a close with no matching open), `duplicate_opens`
(an open while already open), `censored` (count still-open at run end) — a
nonzero value here is a data-quality signal worth surfacing, not just an
internal counter.

### `FaultInterval` (`metrics.faults`)

`node`, `code` (a `ServiceStatus`: `Ok`, `NotInitialized`, `Jam`,
`InvalidData`, `PelletLost`, `FeedTimeout`, `ActuatorTimeout`, ...), `t0`,
`t1` (`None` if censored), `censored: bool`, `inferred_close: bool` (`True`
when no explicit recovery event was logged and the close was inferred from
the node's next successful `Loaded` instead), `.dur` property.

### `PelletAccounting` (`metrics.pellets`, per node)

Two independent pairs of counts exist **on purpose**
([Vocabulary — ledger vs seen](#derived-numbers);
[README trap #5](../README.md#five-things-to-know-before-trusting-a-number)):

| Field | Meaning |
|---|---|
| `presented` / `taken` | rows *actually seen* in this log |
| `presented_total` / `taken_total` | **what you almost always want** — the base-station ledger's gap-corrected total when available (falls back to `presented`/`taken` on older logs with no ledger) |
| `no_feed_presented` | `NoFeedPresented` count — not a real pellet |
| `feed_skipped` | dispenses vetoed (plate occupied) |
| `ledger_presented` / `ledger_taken` | the raw ledger totals; `None` on logs from before the ledger existed |
| `missed_frames` | event frames the ledger proved were dropped (so `presented` undercounts `presented_total`) |
| `counter_restarts` | node power-cycles seen mid-run |
| `.take_rate` | `taken_total / presented_total`, `None` if `presented_total == 0` |
| `.bus_loss_presented` / `.bus_loss_taken` | heartbeat-vs-event-count gap, `None` if fewer than 2 heartbeats seen for that node |

### `InteractionFunnel` (`metrics.funnel`, per node)

Built directly from cycles: `presented` → `approached` (presence while
ready) → `dome_opened` (dome opened while ready) → `taken`, plus
`approach_without_dome` / `dome_without_take` counts — the same booleans as
`Cycle`, aggregated.

### `LatencySummary`

`n`, `median`, `q1`, `q3` (all `None` if `n == 0`), `values` (the cleaned
list — `None`s already dropped). Used wherever the report shows a latency
distribution.

### `ThroughputPoint` (`metrics.throughput`)

`t`, cumulative `presented`, cumulative `taken` — one point per pellet
event, in order. Feed straight to a step chart.

### `ActogramDay` (`metrics.activity_by_day`)

`date` (a `datetime.date`, rig-local calendar day), `times` (hours-since-
midnight, sorted) — see [§7](#7-time-timezone-and-time-of-day) for why this
needs no UTC offset.

`activity_by_day(run, event_names=...)` selects which CAN EVENT display
names count as ticks (default: `("MousePresence Detected",)`). The
printed actogram (`timeline.actogram`) passes the same knob through from
design JSON `options.event_names`. Multiple names are pooled into one
series. Days with zero matching events are omitted, not drawn as a blank
row. The section title names those events (`Actogram — Pellet Taken`),
as does the figure's own SVG `<desc>`.

The actogram draws no light/dark shading: the rig doesn't record the
facility's light schedule, so shading it would render a fixed
clock-time assumption as though it were measured data. Time of day is
on the axis — apply your own light cycle to it if you need one.

A pip-installed user does not edit the package:

```bash
python -m sfm_analysis.examples.actogram_by_event
python -m sfm_analysis.examples.actogram_by_event /path/to/MySession.csv
sfm-report MySession --design actogram_takes --open
```

`actogram_takes` ships in the wheel and is opt-in only (it is not
auto-selected). To plot a different event, copy
[`examples/report_design/designs/actogram_takes.json`](../examples/report_design/designs/actogram_takes.json)
next to your logs, edit `event_names`, and pass the path to
`--design`. See the README section *Customize the actogram*.

## 6. Tidy-table columns

`sfm_analysis.analysis.tables` — every function returns `list[dict]`, one
row per item, ready for `pandas.DataFrame(...)` or plain iteration.

**`cycles_table()`** — every `Cycle` field from [§5](#5-derived-metrics-reference)
verbatim, plus `session`/`run_id`, under the same names: `node`, `trial`,
`index`, `fed`, `cmd_t`, `lowering_t`, `loading_t`, `on_plate_t`,
`raising_t`, `seeking_t`, `dwelling_t`, `ready_t`, `first_presence_t`,
`first_dome_t`, `taken_t`, `end_t`, `censored`, and the five derived
latencies (`cycle_duration`, `approach_latency`, `dome_latency`,
`handling_time`, `retrieval_latency`) plus the two booleans
(`approached_without_dome`, `dome_without_take`).

**`bouts_table()`** — presence bouts, dome bouts, and fault intervals,
unified under one `kind` column (`"presence"` \| `"dome"` \| `"fault"`):
`session`, `run_id`, `kind`, `node`, `t0`, `t1`, `dur`, `censored`, `code`
(fault name, `None` for presence/dome), `inferred_close` (`None` for
presence/dome).

**`events_table(source=, frame_type=, event_name=)`** — the escape hatch:
every raw `LogRow`, filterable. Columns: `session`, `run_id`, `t`, `iso`,
`node_id`, `trial`, `source`, `direction`, `frame_type`, `event_name`,
`fields` (the decoded dict), `details`, `post_session`. Watch the `"Dome
Opened"` trap if you filter on `event_name` directly here — prefer
`bouts_table(kind="dome")`.

**`trials_table()`** — one row per generic `trial` boundary marker:
`session`, `run_id`, `t`, `trial`, `fields`. For template-specific trial
structure (choice, reward, block), see `report.analyses.*` instead (e.g.
`analyses.bandit.build_trials` for two-armed bandit).

## 7. Time, timezone, and time-of-day

Four different "when" values exist on a row; they answer different
questions:

| Value | Meaning | Trap |
|---|---|---|
| `RunData` rows' `.t` | run-relative seconds, recomputed from `timestamp_ms` by `split_runs` | **This is what you want for any timing/latency math.** |
| `elapsed_s` (raw CSV column) | seconds since the *originating process* started | Resets across a reopened session — never read directly ([Vocabulary](#vocabulary); [README trap #2](../README.md#five-things-to-know-before-trusting-a-number)) |
| `timestamp_ms` | epoch milliseconds | Correct for ordering; needs `utc_offset_s` to mean anything as wall-clock |
| `timestamp_iso` (`row.iso`) | rig-local wall-clock string | Already correct local time, with **no offset needed** — see below |

`report.timezones` gives you three functions, all reading `row.iso`
directly rather than reconstructing from epoch ms — because
`datetime.fromtimestamp()`/`.astimezone()` with no arguments implicitly use
*your own machine's* timezone, which is wrong the moment analyst and rig
are in different zones:

- **`time_of_day(row)`** → hours since local midnight (`0.0`–`24.0`). No
  offset needed: `timestamp_iso` was already written in rig-local time.
- **`local_date(row)`** → the calendar date this row falls on, rig-local.
- **`wall_clock(row, run)`** → the *only* one of the three that needs
  `run.utc_offset_s`. Returns a timezone-**aware** `datetime` when the
  offset is known, a naive one otherwise — the naive case is not a
  failure, the value is still correct rig-local time, it's just unsafe to
  compare against another timezone's data or pass to `.astimezone()`.

`utc_offset_s` is `None` on any log recorded before the base station
started capturing it in `session_start`. That degrades `wall_clock()`
gracefully (naive instead of aware) — everything else in this list is
unaffected, since none of it ever needed the offset in the first place.

## 8. Designing your own analysis: a worked example

**The question:** *did the pellet take-rate change in the minutes right
after a fault, compared to the session overall?* Not in the README's
cookbook, and it needs the interval algebra plus the tidy tables together.

**Pick the layer.** `cycles_table()` has `taken_t` per cycle already;
`m.faults` (a `RunMetrics` field, [§5](#5-derived-metrics-reference)) has
fault intervals. No raw-row work needed.

**Write it**, against the bundled demo session (`sfm-report --demo`'s
data — has exactly one real fault, verified ground truth in
`tests/test_report_smoke.py`'s docstring):

```python
from sfm_analysis.analysis import load_session
from sfm_analysis.analysis import intervals as iv
from sfm_analysis.report.demo import DEMO_SESSION_PATH

s = load_session(str(DEMO_SESSION_PATH))
run = s.run()
m = s.metrics_for()

takes = [c["taken_t"] for c in s.cycles_table() if c["taken_t"] is not None]

fault = m.faults[0]                       # this session's one fault interval
window_after = [(fault.t1, fault.t1 + 120)]   # 2 minutes after recovery

overall_rate = iv.rate_in(takes, [(0, run.duration_s)])
post_fault_rate = iv.rate_in(takes, window_after)

print(f"overall: {overall_rate:.4f} takes/s, post-fault: {post_fault_rate:.4f} takes/s")
```

**Validate against known ground truth** before trusting the number on a
new file. On the demo session this prints:

```
overall: 0.0132 takes/s, post-fault: 0.0250 takes/s
```

which checks out against the file's independently-verified numbers: 17
takes over the run's 1291.8s (`17/1291.8 ≈ 0.0132`), and 3 of those 17 fall
in the 120s after the one fault interval closes at `t=200.404`
(`3/120 = 0.0250`). If your own first run of a new analysis doesn't reduce
to arithmetic you can check by hand like this against a file you already
understand, don't trust the number yet.

**Test it** the same way the test suite does — either build a synthetic
run with `tests/report_fixtures.py` (see the README's "Testing your own
analysis code" section) so you control the exact fault timing and takes,
or pin the assertion against the demo session's known numbers directly, as
above.

## 9. Turning an analysis into a report section

An analysis that's worth seeing on every report for a given experiment
becomes a **section**: a plain function `(ctx: SectionContext) ->
Optional[SectionResult]` (`report.schema`), registered in some
`report/sections/<module>.py`'s `SECTIONS` dict, referenced by
`"module.function"` in a design JSON.

### File layout

Same split as an experiment template (JSON declares *what*, Python holds
*behavior*), with one extra file because one experiment often has several
related figures:

```
packages/sfm-analysis/src/sfm_analysis/report/
├── designs/
│   └── <name>.json       # "matches": ["<experiment>"] + ordered section refs
├── analyses/
│   └── <name>.py         # numbers: functions / dataclasses, no HTML
└── sections/
    └── <name>.py         # HTML; SECTIONS = {"func": fn, ...}
```

`resolve_design(experiment)` picks a design by `name` or `matches`, else
`default.json`. `resolve_section("module.func")` imports
`sections.<module>` and returns `SECTIONS["func"]`. Copy-from starter for
the `alternation` experiment example:
[`examples/report_design/`](../examples/report_design/).

### Complete example (custom experiment → custom design)

Pair this with `packages/dev_gui/examples/templates/alternation.py`, which
logs `alternation_advance` (`from_node`, `to_node`) on every switch. After
copying the three files below into the tree above, `sfm-report` on an
alternation session renders a switch table at the end of the generic
sections.

`designs/alternation.json` (abridged — the starter file includes timeline
+ generic sections too):

```json
{
  "name": "alternation",
  "matches": ["alternation"],
  "sections": [
    { "ref": "generic.provenance" },
    { "ref": "generic.retrieval_latency" },
    { "ref": "alternation.switch_table" }
  ],
  "combined_sections": [
    { "ref": "compare.cohort_table" },
    { "ref": "alternation.switch_table" }
  ]
}
```

`analyses/alternation.py`:

```python
from collections import Counter
from typing import Dict, List, Tuple
from ..session import RunData

def switches(run: RunData) -> List[Tuple[int, int, float]]:
    out = []
    for row in run.exp("alternation_advance"):
        src, dst = row.fields.get("from_node"), row.fields.get("to_node")
        if src is not None and dst is not None:
            out.append((int(src), int(dst), row.t))
    return out

def switch_counts(run: RunData) -> Dict[Tuple[int, int], int]:
    return Counter((a, b) for a, b, _ in switches(run))
```

`sections/alternation.py`:

```python
from typing import Optional
from ..analyses.alternation import switch_counts, switches
from ..charts import escape_text
from ..schema import SectionContext, SectionResult

def switch_table_section(ctx: SectionContext) -> Optional[SectionResult]:
    parts = []
    for run, m in zip(ctx.runs, ctx.metrics):
        rows = switches(run)
        if not rows:
            continue
        counts = switch_counts(run)
        body = "".join(
            f"<tr><td>{a} → {b}</td><td>{n}</td></tr>"
            for (a, b), n in sorted(counts.items())
        )
        parts.append(f"<table><tr><th>From → to</th><th>Count</th></tr>{body}</table>")
    if not parts:
        return SectionResult(section_id="alternation.switch_table",
                             title="Alternation switches", html="", empty=True)
    return SectionResult(section_id="alternation.switch_table",
                         title="Alternation switches", html="".join(parts))

SECTIONS = {"switch_table": switch_table_section}
```

Minimal section shape, if you are not pairing with a new experiment:

```python
def my_section(ctx: SectionContext) -> Optional[SectionResult]:
    figs = []
    for run, m in zip(ctx.runs, ctx.metrics):
        ...  # ctx.runs / ctx.metrics only -- see the rule below
    if not figs:
        return SectionResult(section_id="mymodule.my_section", title="My Analysis", html="", empty=True)
    return SectionResult(section_id="mymodule.my_section", title="My Analysis", html="".join(figs))
```

- **A section must not touch the filesystem.** `SectionContext` (`ctx.runs`,
  `ctx.metrics`, `ctx.opts` for this section's JSON `options` block,
  `ctx.active_refs` for which other sections are in this document) is
  built once and handed to every section read-only.
- **A section that raises never kills the report** — `schema.run_section`
  catches it and renders a visible error block with the traceback instead,
  so one broken section can't take the rest down.
- Add `{"ref": "mymodule.my_section"}` to the relevant
  `report/designs/*.json`'s `sections` (or `combined_sections`) list — no
  CLI or render-pipeline change needed.
- If the section needs to be **interactive** (canvas, real zoom — like
  `timeline.explorer`), it can set `SectionResult.extra_css` /
  `.extra_js`: `render.py` collects and deduplicates these across every
  active section into one `<style>` block and at most one inline
  `<script>` for the whole document, so a report never carries more than
  one script element regardless of how many interactive sections it has.
  See `sections/timeline.py`'s `explorer_section` for the pattern —
  including its print-only fallback for the static equivalent, since print
  can't run canvas JS.
