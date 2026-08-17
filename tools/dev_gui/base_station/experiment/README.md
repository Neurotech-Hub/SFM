# Writing Custom Experiment Templates (Python API)

This is the guide for authoring your own experiments for the SFM dev GUI. An
**experiment** automates the pellet-dispensing nodes over CAN: it decides *when*
to dispense, *which* node(s), and *how* to react to what the nodes and the BNC
sync inputs do.

You write experiments in Python. There is **no base class to inherit** — a
template is just a module with a `build(...)` factory that returns a configured
`Experiment` object. A small JSON file describes the tunable parameters so the
GUI can render a form for it.

Everything an experiment does flows through the same CAN bus the manual controls
use, so **the node tiles always show live status** while an experiment runs — you
don't have to do anything special to keep them in sync. The same is true of the
event log: `control.dispense` / `control.recover` (and their broadcast variants)
show up as **COMMAND**-type rows identical to a manually-clicked button —
filtering the log by COMMAND shows every command sent, regardless of whether a
human or a running experiment triggered it. Everything else you log with
`control.log(...)` (session lifecycle, per-cycle bookkeeping, etc.) shows up as
**EXPERIMENT** rows.

### Session naming and the unified log (GUI)

The GUI's Experiment panel requires a **session name** before Start is
enabled. That name resolves to one CSV, `<log_dir>/<sanitized_name>.csv`
(non-alphanumeric characters collapse to `_`) — every CAN frame, heartbeat,
BNC edge, and experiment row lands there with columns `run_id`, `trial`,
`source`, and a lossless `fields_json` alongside the human-readable
`details`. Starting a new run under a **name already used** appends rather
than overwriting, and `run_id` increments so runs can be told apart in
analysis — this is how a session picks back up across GUI restarts. All
GUI-hosted experiments log through this one file; passing `log_dir` to
`ExperimentController.start()` is a no-op from the GUI (`log_dir=None` is
always passed to `make_runner()`) so nothing is written twice. The
per-template `experiment_<name>_<timestamp>.csv` writer in
`ExperimentControl` still exists and is used by headless runs (§11) — it
just isn't wired up when the GUI hosts the runner.

At the moment a session actually **activates** (immediately if the template
has no `start_when`, later otherwise — see `ExperimentControl.on_session_start`),
the GUI broadcasts `CanCmd.SyncFlash` (every node holds its status LED solid
for ~500 ms) and fires a coincident BNC OUT pulse, so field-camera footage can
be aligned to session start without a manual clapperboard. A node latched in
`Fault` ignores the command (same guard the firmware uses for a solid fault
indication) and is called out by id in the logged `SYNC` row.

### Trial numbers

Call `control.next_trial()` once per delivery cycle — the built-in templates
that dispense on a timer/BNC trigger or per-node reload all do this, so
`trial` (and its mirror `counter("trials")`) means "one delivery decision"
consistently across templates, not just in `@exp.script`-based tasks like
`two_armed_bandit`. The GUI mirrors the running `control.trial` onto every
log row (CAN and experiment alike) each frame, so trial boundaries are
visible outside `control.log(...)` rows too.

---

## 1. Anatomy of a template

Two files per template:

```
base_station/experiment/templates/<name>.py   # behavior  (the build() factory)
experiments/<name>.json                   # parameters (drives the GUI form)
```

The Python module exposes one function:

```python
from ..runner import Experiment

def build(nodes=None, *, name="<name>", **params) -> Experiment:
    node_list = list(nodes) if nodes else [1, 2, 3]
    exp = Experiment(nodes=node_list, name=name)

    @exp.on_start
    def _start(control):
        for n in control.nodes:
            control.dispense(n)

    exp.end_after(minutes=30)
    return exp
```

- `nodes` is injected by the GUI from the **node multi-select** in the Experiment
  panel (or defaults inside the template). Templates iterate `control.nodes` —
  never a hard-coded list — so node selection "just works".
- Register behavior with the `@exp.on_*` decorators (§4), or write the task as
  one sequential generator with `@exp.script` (§5) — you can mix both.
- Call `exp.end_after(...)` for automatic stop conditions.
- Return the `Experiment`.

For duration/pellet-cap plumbing, the shared next-trial wait (`fixed_delay` /
`presence_clear` / `bnc`), and the usual fault/recover logging, see
[`kit.py`](kit.py) — small helpers the built-in templates share
(`kit.session(...)`, `kit.wire_advance(...)`, `kit.next_trial_wait(...)`,
`kit.after_advance(...)`, `kit.log_faults(...)`, `kit.log_cycle_summary(...)`).
Using them is optional but saves boilerplate.

### Registering the template

Templates need no central registration. `resolve_builder()` in
[`schema.py`](schema.py) dynamically imports
`base_station.experiment.templates.<name>.build` — dropping a new
`templates/<name>.py` (with a matching `experiments/<name>.json`) is enough. If
you want it importable as `from .templates import my_template`, add one line to
[`templates/__init__.py`](templates/__init__.py):

```python
from .my_template import build as my_template
```

### The JSON parameter file

```json
{
  "name": "my_template",
  "label": "My Template",
  "template": "my_template",
  "description": "One-line summary shown in the GUI.",
  "parameters": [
    { "key": "interval_s", "label": "Interval (s)", "type": "float",
      "default": 10.0, "min": 0, "max": 3600, "help": "..." }
  ]
}
```

- `template` must match the module name under `templates/`.
- Each parameter's `key` becomes a keyword argument to `build(...)`.
- Convention: `max_pellets` and duration-in-`minutes` of `0` mean "no limit".

Every parameter value passes through `coerce_param_value` before reaching
`build(...)`, so you receive it already typed. **The GUI form is generated
entirely from this JSON — you never write per-parameter UI code.**

### Parameter types

| `type` | Widget | Value passed to `build(...)` |
|--------|--------|------------------------------|
| `int` / `float` | number input (honors `min`/`max`) | `int` / `float` |
| `bool` | checkbox | `bool` |
| `str` | text input | `str` |
| `choice` | dropdown (needs `"options": [...]`) | `str` (one of the options) |
| `nodes` | one checkbox per node + **All/None** | *(drives the `nodes=` argument, below)* |
| `node_number` | one number input per node (honors `min`/`max`) | `dict {node_id: number}` — `0` = node inactive |
| `node_choice` | one dropdown per node (needs `"options"`) | `dict {node_id: str}` — **first option = inactive** |

The three node types render **one control per node** automatically — that is how
`fixed_and_random` (a role dropdown per node) and `probability_delivery` (a %
per node) are built, with **no template-specific GUI code**.

**Active nodes / the `nodes=` argument.** `build(nodes=...)` always receives the
active node set, derived in this order: a `nodes`-type param's checked list →
else a `node_number` param's keys with value > 0 → else a `node_choice` param's
keys whose value ≠ the first (inactive) option → else all nodes. So per-node
assignment *is* the active/inactive selection — a separate "active nodes" row is
only needed for templates that have no per-node param (like `free_feeding`,
which declares a `nodes` param).

### Conditional display — `visible_when`

Any param may declare `"visible_when": {"<other_key>": <value | [values]>}`; it
is shown (and collected) only when the controlling param currently equals one of
the values. Hidden params fall back to their JSON `default`. Example — show the
delay only for `fixed_delay`, the settle time only for `presence_clear`, the
channel only for BNC:

```json
{ "key": "next_trial_wait", "type": "choice",
  "options": ["fixed_delay", "presence_clear", "bnc"], "default": "fixed_delay" },
{ "key": "fixed_delay_s", "type": "float", "default": 10.0,
  "visible_when": {"next_trial_wait": "fixed_delay"} },
{ "key": "iti_quiet_s", "type": "float", "default": 1.0,
  "visible_when": {"next_trial_wait": "presence_clear"} },
{ "key": "bnc_channel", "type": "int", "default": 0, "min": 0, "max": 1,
  "visible_when": {"next_trial_wait": "bnc"} }
```

A per-node role dropdown looks like:

```json
{ "key": "node_roles", "label": "Node role", "type": "node_choice",
  "options": ["off", "fixed", "random"], "default": "random" }
```

Your `build(node_roles=...)` then receives `{1: "fixed", 2: "off", 3: "random"}`.
While running, the whole form (template selector + every param) is **locked**;
it re-enables on Stop or when the run ends on its own.

BNC channels are **0-based**: `0` = first BNC input (IN1), `1` = second (IN2).

---

## 2. Two styles: loop-based vs event-based

Both use the **same** callback API — the difference is only what drives the
actions. A third style, sequential scripts, is covered in §5 — reach for it when
a task is naturally a series of trials rather than an open-loop reaction to
events.

### Loop-based (timer-driven)

Cycles are paced by timers on the runner clock. `free_feeding` is the canonical
example: dispense on start, then re-dispense a node some delay after it takes
the pellet.

```python
@exp.on_start
def _start(control):
    for n in control.nodes:
        control.dispense(n)

@exp.on_pellet_taken
def _reload(control, event):
    # Node-scoped timer: cancelled automatically if this node faults.
    control.after(2.0, lambda: control.dispense(event.node_id), node=event.node_id)
```

### Event-based (reacts to events)

Actions fire off events — a BNC pulse, a pellet loaded, a dome closing, etc.

```python
@exp.on_bnc_in
def _on_pulse(control, event):
    if event.data.get("edge") == "rising":
        control.dispense(control.nodes[0])   # dispense on every BNC rising edge
```

You can freely mix both in one template (e.g. a timer heartbeat plus BNC
overrides).

---

## 3. The `control` object (`ExperimentControl`)

`control` is passed as the first argument to every callback and to your
`@exp.script` generator. It is your entire action surface — see
[`context.py`](context.py).

### Actions

| Call | Effect |
|------|--------|
| `control.dispense(node)` | Dispense on one node. **No-op while that node is halted, mid-cycle from a previous dispense, or already has a pellet on the plate** (all three logged — see below). |
| `control.dispense(node, feed=False, dwell_s=6.0)` | Run the identical dispense motion with **no pellet**: lower, hold at the drop position for `dwell_s`, then raise an empty plate. Use it so an inactive arm still moves/sounds the same as a fed one (e.g. a bandit task where the animal shouldn't be able to tell arms apart by sound). Same vetoes apply. |
| `control.recover(node)` | Send Recover to one node (stop motion + clear its fault). |
| `control.broadcast_dispense()` / `control.broadcast_recover()` | Same, to all nodes. If any node isn't clear (occupied plate or a cycle in flight), `broadcast_dispense()` can't withhold the command from just that one node in a single CAN frame, so it falls back to individual `dispense(n)` calls (each carrying its own vetoes) and returns `False`. |
| `control.bnc_pulse(duration_us=100)` | Pulse the BNC OUT line. |
| `control.set_heartbeat_interval(node, ms)` | Reconfigure a node's heartbeat rate. |

**Three standardized, top-priority vetoes (system-wide, every template, zero wiring needed).** `dispense()` is the single choke point every dispense in the codebase goes through — templates, scripts, the GUI — so these three checks apply everywhere automatically, in this order, before anything is transmitted:

1. **Halted** (`is_halted(node)`) — a faulted node; logs `dispense_skipped_halted`.
2. **Cycle in flight** (`is_dispensing(node)`) — a previous `dispense()`/`dispense(feed=False)` on this node hasn't resolved yet (no `LOADED`/`NO_FEED_PRESENTED`/`FEED_SKIPPED`/fault/offline event since it was sent). This is what stops a fast timer/BNC-triggered template from re-picking the *same* node while it's still mid-motion — the plate-occupied check below can't catch that window, since the pellet hasn't reached the plate yet. Logs `dispense_skipped_in_flight` (`warning=1`).
3. **Pellet already on the plate** (`pellet_on_plate(node)`) — logs `dispense_skipped_pellet_present` (`warning=1`).

All three return `False` with no command sent. Firmware's own occupancy guard (`DispenserService::dispense()`/`dispenseNoFeed()` — see `FeedSkipped`) remains a backstop for the small race between these client-side checks and the command reaching the bus, but the common case never reaches it. `control.pellet_clear(nodes=None)` / `control.is_dispensing(node)` let you gate your own wait on either signal — see `kit.next_trial_wait`'s `"presence_clear"` mode, which checks pellet state first, unconditionally, before presence.

### Per-node fault handling (sticky)

| Call | Effect |
|------|--------|
| `control.halt_node(node_id)` | Latch a node: cancel its timers, make its `dispense` a no-op. (The engine calls this automatically on a FAULT event — you rarely call it yourself.) |
| `control.recover_node(node_id)` | Clear the halt and send Recover (clears the firmware fault). |
| `control.is_halted(node_id)` → `bool` | Is this node latched? |
| `control.halted_nodes` | Sorted list of halted node IDs. |

### Timers (runner clock, not wall clock)

| Call | Effect |
|------|--------|
| `control.after(seconds, cb, node=0)` | One-shot. Pass `node=` to tie it to a node so it is cancelled if that node faults. |
| `control.every(seconds, cb, node=0)` | Repeating. |
| `control.cancel_timer(timer)` / `control.cancel_node_timers(node_id)` / `control.cancel_all_timers()` | Cancel. |

### Per-node sensor state (for `@exp.script` conditions, but usable anywhere)

| Call | Effect |
|------|--------|
| `control.quiet_for(seconds, node=None)` → `bool` | No PG/dome/presence/pellet-taken/BNC activity on the given node(s) — or all session nodes if omitted — for `seconds`. Ignores heartbeats and the node's own dispense-phase events, so it reflects the *animal*, not our own commands. |
| `control.domes_closed(nodes=None)` → `bool` | All the given (or all session) domes currently closed. |
| `control.dome_open(node)` / `control.pellet_on_plate(node)` / `control.is_online(node)` → `bool` | Current per-node sensor snapshot. |
| `control.pellet_clear(nodes=None)` → `bool` | No pellet on the plate on every given (or all session) node. Mirrors `presence_clear()`/`domes_closed()` — this is what `dispense()`'s own veto checks. |
| `control.is_dispensing(node)` → `bool` | True from the moment `dispense(node)` sends a command until that cycle resolves. This is what `dispense()`'s "cycle already in flight" veto checks — see above. |
| `control.presence(node)` → `bool` / `control.presence_clear(nodes=None)` → `bool` | Animal on the capacitive presence pad; clear on every given (or all session) node. Defaults `False`/clear until the first reading arrives for a node — gate on `is_online(node)` first if you need to tell "confirmed clear" from "not yet known". |
| `control.presented_pellet(node)` / `control.presented_empty(node)` → `bool` | What the last **completed** dispense cycle on this node raised — a real pellet, or an empty plate from `dispense(node, feed=False)`. At most one is ever `True`. |
| `control.presentation(node)` → `str` | `'pellet'` \| `'empty'` \| `'none'` (`'none'` while a cycle is in flight). |
| `control.presentation_done(node)` → `bool` | `True` once the cycle has finished raising, either way. Stays `True` through the take — call `clear_presentation()` to reset it. `wait_until` this across several nodes to gate a response window on **all** of them being ready before acting on any (see `two_armed_bandit.py`). |
| `control.clear_presentation(node)` | Reset a node's presentation state — call at the start of a new trial so a stale reading can't be mistaken for this trial's result. |

### Counters, trials, RNG, time, logging, stop

| Call | Effect |
|------|--------|
| `control.counter(name)` / `control.incr(name, amount=1)` / `control.set_counter(name, v)` | Named integer counters. |
| `control.next_trial()` → `int` | Advance and return the trial counter (also mirrored in `counter("trials")`, logged as a `trial` row). For sequential trial-based tasks. |
| `control.trial` | Current trial number (0 before the first `next_trial()`). |
| `control.chance(p)` / `control.pick(seq)` / `control.shuffled(seq)` | Session-seeded RNG helpers. See `control.seed` — generated and logged in `session_start` if you didn't supply one, so any run can be replayed. |
| `control.elapsed()` | Seconds since the session became active. |
| `control.log(name, node=0, **fields)` | Write an experiment log row (to the GUI log + CSV). |
| `control.stop(reason="...")` | End the whole session as soon as possible (cancels all timers). |
| `control.nodes` | The node IDs this session runs on. |

The engine auto-increments the `"pellets"` counter on every **`LOADED`**
event (a fully delivered pellet) — a no-feed cycle's `NO_FEED_PRESENTED` does
**not** increment it, so `end_after(pellets=...)` only counts real deliveries.
For a per-node tally, keep your own counter:

```python
@exp.on_loaded
def _p(control, event):
    control.incr(f"pellets_{event.node_id}")
```

---

## 4. Events you can handle

Register with `@exp.on(EventKind.X)` or the sugar decorators. Handlers receive
`(control, event)` where `event` is a `NodeEvent(kind, node_id, timestamp, data)`.

| Sugar decorator | EventKind | `event.data` notes |
|-----------------|-----------|-----------------|
| `@exp.on_start` / `@exp.on_end` | `SESSION_START` / `SESSION_END` | `(control)` only — no `event`. |
| `@exp.on_on_plate` | `ON_PLATE` | `pellet_count` |
| `@exp.on_loaded` | `LOADED` | `pellet_count` |
| `@exp.on_pellet_taken` | `PELLET_TAKEN` | `pellet_count`, `dome_open` |
| `@exp.on_feed_skipped` | `FEED_SKIPPED` | plate already occupied when dispensed |
| `@exp.on_no_feed_presented` | `NO_FEED_PRESENTED` | a no-feed dispense finished raising an empty plate |
| `@exp.on_dome_opened` / `@exp.on_dome_closed` | `DOME_OPENED` / `DOME_CLOSED` | derived from the dome sensor |
| `@exp.on_fault` | `FAULT` | `fault_code` (`FeedTimeout` / `ActuatorTimeout` / `Jam` / `PelletLost`) |
| `@exp.on_recover` | `NODE_RECOVERED` | fired when an operator recovers a node |
| `@exp.on_bnc_in` | `BNC_IN` | `channel` (0/1), `edge` ("rising"/"falling"), `high` |
| `@exp.on_presence_changed` | `PRESENCE_CHANGED` | |

Other kinds available via `@exp.on(...)`: `LOWERING`, `LOADING`, `DWELLING`,
`RAISING`, `DOME_OPEN_WARNING`, `PG_CHANGED`, `HEARTBEAT`, `NODE_ONLINE`,
`NODE_OFFLINE`. See [`events.py`](events.py) for the full list.

### Start / end conditions

```python
exp.start_when(lambda control: control.counter("armed") > 0)   # defer SESSION_START
exp.end_when(lambda control: control.elapsed() > 600)          # custom end
exp.end_after(hours=0, minutes=30, seconds=0, pellets=100)     # duration and/or cap
```

---

## 5. Sequential tasks — `@exp.script`

Some tasks are naturally a series of trials: do something, wait for a specific
outcome, wait again, repeat — a two-armed bandit is the canonical example. That
reads far more clearly as one generator than as a handful of `@exp.on_*`
handlers threading state through a dict. `@exp.script` runs *alongside* the
callback API (both may be used on the same `Experiment`) — there is exactly one
script per experiment.

```python
@exp.script
def run(control):
    while True:
        trial = control.next_trial()
        control.dispense(fed_node)
        control.dispense(other_node, feed=False)   # motion, no pellet

        result = yield control.wait_for("pellet_taken", node=fed_node, timeout=30.0)
        if result.faulted:
            yield control.wait(5.0)
            continue

        yield control.wait_until(
            lambda c: c.domes_closed() and c.quiet_for(5.0),
            timeout=30.0,
            label="iti_quiet",
        )
```

### The three things you can `yield`

- **`control.wait_for(kind, node=None, timeout=None)`** — wait for the next
  matching event. `kind` is an event name (`"pellet_taken"`, case-insensitive)
  or an `EventKind`. `node` restricts to one id or a list; omit for any node.
- **`control.wait_until(predicate, timeout=None, node=None, label=None)`** — wait until
  `predicate(control)` returns True (checked every tick). Pass `node=` only if
  this wait should also abort when that node faults. Pass `label=` so
  `script_stalled` / `script_timeout` name the wait (`"plates_clear"`) instead
  of logging an unlabeled lambda as `condition`.
- **`control.wait(seconds)`** — wait a fixed duration on the runner clock.

Each `yield` returns the same object, with fields filled in once it resolves:
`.ok` (`bool` — succeeded cleanly), `.timed_out`, `.faulted` / `.faulted_node`,
and `.event` (the `NodeEvent` that satisfied a `wait_for`, if any). Ignoring the
return value is the common case and costs nothing.

### What "wait" actually means

- Two lines like `control.dispense(fed)` then `yield control.wait_for(...)` can
  never race — the engine is single-threaded, so no reply can arrive before the
  next tick regardless of how the two lines are written.
- `@exp.on_*` handlers for an event always run **before** any script code that
  same event unblocks (they fire earlier in the same dispatch batch).
- **Faults never raise inside your script.** If the node a pending `wait_for`
  is scoped to faults, the wait resolves with `.faulted=True` instead of
  hanging or throwing — the script decides what to do (usually: log it, wait a
  bit, and move to the next trial). This matches the callback engine's own
  behavior: a fault halts only that node; the rest of the session keeps going.
- A generator that **returns** ends the session (`stop(reason="script_finished")`
  — when your `run()` function is done, the experiment is done). A generator
  that **raises** also ends the session, with the error and your code's own
  file/line logged as `script_error` — unlike `@exp.on_*` handlers, whose
  errors are isolated so one broken handler can't take down the rest.
- **Every loop must yield.** `while True:` with no `yield` inside is an
  uninterruptible Python loop and will hang the whole engine — there is no
  runtime protection against this, only against loops that yield awaits which
  keep resolving immediately (bounded to 64 resumes per tick).

See [`script.py`](script.py) for the full scheduler and
[`templates/two_armed_bandit.py`](templates/two_armed_bandit.py) for a complete
worked example.

---

## 6. Faults are sticky, per node

When a node reports a **jam or timeout**:

1. The firmware halts that node's motors and refuses further dispenses until a
   Recover — it is already sticky at the hardware level.
2. The engine calls `control.halt_node(node_id)`: cancels that node's timers and
   makes its `control.dispense(...)` a no-op. **The other nodes keep running.**
3. The node stays latched until an operator presses **Recover** on its tile (or a
   BNC IN action / `runner.recover_node`). Recovery sends Recover (clears the fault)
   and fires your `@exp.on_recover` handler so you can re-arm the node.

This means your template usually needs **no fault bookkeeping** — keep calling
`control.dispense(n)` for every node and halted ones simply stop receiving
pellets. Use `@exp.on_recover` to resume a node's cycle (or `kit.log_faults(exp)`
for the common log-only case):

```python
@exp.on_fault
def _fault(control, event):
    control.log("fault", node=event.node_id, fault_code=event.data.get("fault_code"))

@exp.on_recover
def _recovered(control, event):
    control.dispense(event.node_id)   # resume this node
```

---

## 7. BNC sync I/O

BNC is a base-station feature (Raspberry Pi GPIO); nodes are not involved.

**BNC IN** — the GUI dispatches an action per **edge** (rising / falling), each
independently optional. Configure it in the BNC panel (e.g. rising →
`start_experiment`, falling → `stop_experiment`). Inside a running experiment you
also get every edge as a `BNC_IN` event, so a template can react directly:

```python
@exp.on_bnc_in
def _edge(control, event):
    if event.data["edge"] == "rising" and event.data["channel"] == 0:
        control.dispense(control.nodes[0])
```

**BNC OUT** — set the BNC OUT **Trigger** in the GUI to a CAN event name (e.g.
`Loaded`) and it pulses whenever that event arrives from any node, during
manual use *and* during an experiment. To pulse from template code directly, call
`control.bnc_pulse(width_us)` (e.g. on each dispense).

---

## 8. Node selection

The Experiment panel has per-node checkboxes plus **All / None**. The checked set
is passed to `build(nodes=...)`, so `control.nodes` is exactly the subset the
operator chose. Always iterate `control.nodes`; never assume `[1, 2, 3]`.

---

## 9. Worked examples in this repo

- **[`templates/free_feeding.py`](templates/free_feeding.py)** — loop-based:
  continuous reload after each pellet is taken, using the shared advance
  (`next_trial_wait`: `fixed_delay` default 30 s, or `presence_clear`);
  per-node sticky fault + recover. (`reload_delay_s=` is a headless alias
  of `fixed_delay_s`.)
- **[`templates/fixed_and_random.py`](templates/fixed_and_random.py)** — each
  node has a **role** (`off` / `fixed` / `random`) from a `node_choice` param.
  `fixed` nodes dispense every cycle, `random` nodes dispense with `random_prob`,
  `off` nodes are inactive. `build(node_roles={1:"fixed", 2:"off", ...})`.
  Advance with the same `next_trial_wait` as two-armed bandit (`fixed_delay` /
  `presence_clear`), plus `bnc` for an external rising edge. (A legacy
  `fixed_nodes` string and `trigger="timer"` / `interval_s=` are still
  accepted for headless runs.)
- **[`templates/probability_delivery.py`](templates/probability_delivery.py)** —
  each cycle delivers on **one** node, chosen by an independent **weighted
  random draw** (`random.choices`) — e.g. weights `20,80` mean node 1 has a 20%
  chance and node 2 an 80% chance *on every single cycle*; it is not a fixed
  20-of-100 allocation or round-robin, and over many cycles the observed split
  converges to the configured weights. `0` means that node is never picked.
  A `node_number` param supplies the per-node weights as `{node_id: pct}`
  (a comma-separated string is accepted for headless runs). Same shared
  `next_trial_wait` as `fixed_and_random`.
- **[`templates/two_armed_bandit.py`](templates/two_armed_bandit.py)** —
  `@exp.script`-based: exactly two nodes (the arms). Each trial, both arms run
  the dispense motion (`control.dispense(fed)` / `control.dispense(empty,
  feed=False)`), but only one delivers, chosen by a reward probability that
  flips which arm is "rich" every `block_size` trials. Sweeps both plates
  clear before trial 1, gates the response window on **both** arms finishing
  their raise (`presentation_done`) so neither one tips off the animal by
  finishing first, then waits — with no timeout — for the pellet to be taken.
  Advances to the next trial either after a fixed delay or once the animal is
  off both presence pads (`next_trial_wait`). If a no-feed arm's plate turns
  out already occupied (firmware presents the leftover pellet honestly rather
  than discarding it), the trial is logged invalid and the session keeps
  running — see the module docstring for the full mutual-exclusion and sync
  analysis. A good reference for writing your own trial-based `@exp.script`
  template: see `control.presented_pellet`/`presented_empty`/`presentation_done`/
  `clear_presentation`/`presence`/`presence_clear` in [`context.py`](context.py)
  and `kit.next_trial_wait` in [`kit.py`](kit.py).

Both `fixed_and_random` and `probability_delivery` accept `seed=` for
reproducible runs (used by the tests); `two_armed_bandit` does too, and also
logs whatever seed it used (generated if you didn't supply one) so any session
can be replayed. Because all three use the declarative node param types above,
none needs any per-template GUI code — the form (per-node dropdowns / % inputs,
plus the advance-gated delay / settle / BNC fields) is generated entirely from their
JSON.

---

## 10. Testing your template

Run headless against synthetic events — no CAN hardware needed:

```python
from base_station.experiment import EventKind, NodeEvent
from base_station.experiment.templates.my_template import build

exp = build(nodes=[1, 2], interval_s=5.0, seconds=60)
runner = exp.make_runner()          # no CAN/IO bound → dry-run
runner.start(now=0.0)
runner.step(now=5.0)                 # advance the clock; timers fire

# Inject events:
runner.inject(NodeEvent(EventKind.FAULT, node_id=1, timestamp=6.0,
                        data={"fault_code": 1}))

# Inspect what was sent:
print(runner.ctx.commands_sent)      # list of (node_id, CanCmd, payload)
```

`runner.ctx.commands_sent` records every command in dry-run mode, and
`runner.ctx.is_halted(...)`, `runner.ctx.counter(...)`, and the log entries let you
assert behavior. `runner.ctx` and `runner.control` are the same object — use
whichever reads better. See
[`../../tests/test_experiment.py`](../../tests/test_experiment.py) and
[`../../tests/test_script.py`](../../tests/test_script.py) for patterns,
including how to drive a `@exp.script` template through `inject()`/`step()`.

---

## 11. Running headless (no GUI)

The GUI is one host for the engine; you don't need it. There are two ways to run
an experiment against a live SocketCAN interface (`can0`, or `vcan0` with the node
simulator) — both go through `Experiment.run()` and write the same per-session CSV.

**A. A plain Python script** — configure a template (or write behavior inline)
and call `exp.run(...)`, which blocks until the session ends (duration / pellet
cap / Ctrl+C):

```python
from base_station.experiment.templates.free_feeding import build

exp = build(nodes=[1, 2, 3], reload_delay_s=30, minutes=10)
exp.run(interface="vcan0", log_dir="~/sfm_logs", use_io=False)  # use_io=False off-Pi
```

This is all a custom experiment needs: your own module with a
`build(nodes=None, *, ...) -> Experiment` factory (or just an `exp = Experiment(...)`),
then `exp.run(...)`. See [`../../example_experiment.py`](../../example_experiment.py)
for a runnable version (including an inline-callbacks variant with no template file).

**B. The `run_experiment.py` CLI** — run a built-in template using its JSON values
(the same schema the GUI form uses), a `.json` schema file, or **your own `.py`
script**, with optional overrides:

```bash
# from tools/dev_gui/
python run_experiment.py --list                                  # discover templates + params
python run_experiment.py free_feeding -i vcan0 --minutes 10
python run_experiment.py probability_delivery --set probabilities=1:20,2:80
python run_experiment.py fixed_and_random --set node_roles=1:fixed,2:off,3:random
python run_experiment.py two_armed_bandit --set block_size=50,p_high=0.9
python run_experiment.py path/to/my_task.py --nodes 1,2,4        # your own template/script
python run_experiment.py free_feeding --dry-run                  # build only, no CAN
```

`--set key=value` accepts a scalar (`minutes=10`), a list (`x=1,2,3`), or a
**per-node map** (`x=1:fixed,2:off`) — the map/list forms feed the `node_choice` /
`node_number` / `nodes` params exactly as the GUI's per-node widgets do. The CLI
stops on the duration / pellet cap you configure (or Ctrl+C).
