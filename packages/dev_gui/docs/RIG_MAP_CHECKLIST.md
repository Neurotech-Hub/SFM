# Rig Map: where every control of the old GUI went

This is the Phase 4 gate: every control in the DearPyGui app has a home in the
Rig Map page, or is listed here as open or dropped on purpose. It was built by
reading `base_station/app.py`, not from memory.

## Open

| Old control | What it does | Status |
|---|---|---|
| Node tile **SetConfig** button (schedule dialog) | Per-node dispense schedule driven by the base station: off, every N minutes, or N minutes after another node dispenses. Lives in the engine (`ScheduleConfig`, `_schedules`). | **Not carried over. It was not in the plan. Needs a decision.** The engine side already exists, so adding it is a command, a state field and a small section in the Node tab. |

## Dropped on purpose

| Old control | Why |
|---|---|
| BNC OUT **Label** field | BNC OUT is now one chosen mapping, not a free-form trigger with a label. |
| BNC OUT **Enabled** checkbox | Replaced by the mapping "Off". |
| Heartbeat **On/Off** switch | The firmware has no off state; the interval can only be changed (0.1 to 65.535 s). |

## Connect (old setup screen)

| Old control | New home |
|---|---|
| Interface | Connect: can0 and vcan0 cards, can0 selected by default, re-scanned every 3 s |
| Bitrate | Connect: shown read-only, read from the kernel for hardware CAN |
| Check | Connect: Check |
| Number of nodes | Connect: stepper, 1 to 12; the nodes that answered are listed once connected |
| Log directory | Connect: Folder, with "Use external drive" |
| Auto-save to CSV | Connect: checkbox |
| Start Session | Connect: Connect, then Continue to experiment |
| (new) GPIO backend | Connect, after connecting. Warns when BNC is simulated. |

## Run header (old main screen top)

| Old control | New home |
|---|---|
| Re-discover | Connect (after connecting) and Run, I/O tab. Asks first. |
| Dispense All / Recover All / Ping All | Run header |
| Calibrate Presence, and its "cage is empty" dialog | Run header. Optional: nothing blocks starting without it. Not available while running. |
| Developer Menu | Run header: Developer (adds the Developer tab) |
| Experiment Start / Stop | Run header: Start session / Stop session. Stop asks first. |
| Experiment status text, pellet total | Run header: Elapsed, Trial, Nodes online, Taken / presented |

## Node tile

| Old control | New home |
|---|---|
| Label field | Node tab: Label |
| Status dot and text | Map ring and state text; Node tab |
| ID, MAC | Node tab: Hardware details (also firmware) |
| HB age (amber when late, red when overdue) | Node tab, under the state, same thresholds |
| Presence, Pellet, Load, Dome | Node tab sensors; map ring (halo, pellet dot, dome outline) |
| Pellet counts | Node tab: Pellets presented and taken |
| Fault text | Fault banner with Recover; Node tab state text |
| Dome-open warning | Node tab note |
| Dispense / Recover / Ping | Node tab |
| AssignId | Developer tab: Assign node ID |
| SetConfig (schedule) | **Open, see above** |

## BNC / Sync I/O

| Old control | New home |
|---|---|
| BNC IN label | I/O tab: Label |
| Rising / Falling action | I/O tab, Edge mode |
| Enabled | I/O tab |
| (new) Mode: Pulse / Level / Log only, active level, minimum width | I/O tab |
| BNC OUT pulse (ms), trigger | I/O tab, BNC OUT: mapping, width, node event |
| Manual Pulse | I/O tab (not while running) |
| (new) Arm start, Disarm | Run header and banner |

## Experiment panel

| Old control | New home |
|---|---|
| Template | Experiment step: template list |
| Parameters, node All / None | Experiment step: form built from the template's JSON |
| Session name | Experiment step, with the file path and resume notice |

## Event log

| Old control | New home |
|---|---|
| Node and type filters | Event log: node and type selects |
| Clear | Event log: Clear |
| Row count | Event log: "Showing N of M" |
| Columns | Time, Node, Dir, Type, Event, ID, Data, Details |
| (new) Show heartbeats, Export | Event log |
