# BNC timing: bench confirmation

**Status: measured on 2026-11-16** on the base station Pi (Raspberry Pi 5, Debian 13, kernel
6.18.60, libgpiod 2.2.0, BNC OUT cabled to BNC IN 0). The raw results are in
[`results/2026-11-16/`](results/2026-11-16/). The summary is below; every number is a
measurement, with the conditions it was taken under.

## Results

**What was confirmed**

- The HAT inverts the BNC input as the code assumes (a raw rising pin edge is a falling BNC edge).
  20 smoke pulses of 10 ms read back as 10.04 ms, none missed.
- The kernel timestamps on both ends behave as designed. Across about 12,000 pulses and events, none
  was missed.

**BNC OUT on its own** (1000 pulses per row, `bnc_loopback pulses`, nothing else running):

| Pulse | request to edge p50 / p99 | slowest | width error p99 (always positive, about +10 to +40 us) |
|---|---|---|---|
| 1 ms | 43 / 96 us | 5.8 ms (one pulse) | 26 us |
| 10 ms | 77 / 152 us | 1.1 ms | 100 us |
| 100 ms | 51 / 104 us | 4.6 ms (one pulse) | 64 us |

**Whole engine, node event to BNC OUT** (`engine_loopback`, 1000 events per row; frame sent to the
kernel's stamp on BNC OUT rising, read from the engine's own log; target p99 under 1 ms):

| Engine setup | machine | p99 | slowest |
|---|---|---|---|
| defaults | idle | 628 us | **6.0 ms** |
| GC freeze | idle | 510 us | 580 us |
| real-time priority | idle | 502 us | 624 us |
| real-time + GC freeze | idle | 447 us | 710 us |
| real-time + GC freeze | 4 busy programs | 426 us | 671 us |
| real-time + GC freeze | 100 CAN frames/s + 4 busy programs | 514 us | 855 us |
| real-time + GC freeze | 300 CAN frames/s + 4 busy programs | 765 and 805 us | 1.08 and 1.44 ms |
| real-time + GC freeze | 1000 CAN frames/s, otherwise idle | **1.30 ms (not met)** | 3.4 ms |

So the engine meets the 1 ms target at p99 at realistic loads, and with real-time priority and GC freeze
the slowest event of 1000 stays near or just over 1 ms. At 1000 CAN frames a second (about half the
bus capacity, far more than a rig produces) the engine's receive path is the limit and the target is
not met.

**CSV timestamps against the real edge** (`engine_loopback`, 1000 events, read from the written CSV, 2026-11-16; BNC OUT row against the kernel's stamp of the same edge on BNC IN 0):

| Condition | `logged_minus_edge` p50 / p99 / worst | CSV `timestamp_ms` difference |
|---|---|---|
| idle | -23 / 101 / 235 us | 0 at p99, never more than 1 ms (one-millisecond rounding) |
| 4 busy programs | -20 / 81 / 301 us | 0 at p99, never more than 1 ms |

The time written for a BNC OUT pulse is the middle of the pin write, because nothing stamps an output edge; `rise_uncertainty_us` in the row's `fields_json` is half the write time (a few microseconds, wider only if the thread was held off the CPU mid-write). `timestamp_ms` is rounded, and `timestamp_iso` is made from it, so the two agree. BNC IN rows carry the kernel's edge stamp. How late the engine processes an event never changes the time in its row; it only changes when the pulse goes out (the table above). Not covered: node-side delay, CAN bus time, an external DAQ.

**What causes the occasional slow event, and what fixes it**

| Cause | Evidence | Fix |
|---|---|---|
| Python's garbage collector pauses every thread (a 7 ms stall at the same event number in three runs; gone with it off) | `--gc off` / `freeze` runs | `--gc freeze`, now the engine's default |
| Other programs holding a timing thread off the CPU | 4 busy programs: pulse width error up to 14 ms without real-time priority, 0.1 ms with | real-time priority (`--realtime`, opt-in) |
| The engine's own other threads being held off while a timing thread waits for the interpreter lock behind them (priority inversion) | with only the timing threads raised, one second of load delayed pulses by 50 ms | the engine's other threads run just below the timing threads (set once, inherited) |
| CPU running slow when idle (`ondemand` governor) | `performance` governor cut the idle worst case from 3.9 ms to 0.33 ms | not changed; real-time priority makes it unnecessary |

Not helpful, measured and left alone: pinning to one CPU core (no gain, and it does not protect the
pulse width), and a shorter Python thread switch interval (no clear gain at realistic loads).

**A CPU-bound Python thread inside the engine process delays every pulse by about 10 ms** (two 5 ms
waits). That is the interpreter lock, not the browser. Keep heavy computation off the engine's threads.

**Limits of these measurements.** One cable, one Pi, 1000 samples per row. Run-to-run variation in
the slowest event is large (two identical runs gave 1.05 and 2.07 ms), so read p99 and the order of
magnitude of the worst case, not a single maximum. The virtual CAN bus has no bus time, and nodes
add their own delay: the end-to-end time from a node's sensor to the pulse is not measured here.
Frames sent by a normal-priority test sender can be delayed by busy programs and mistaken for engine
delay, which is why the busy-machine rows run the sender at real-time priority.

## What is being confirmed

Three measurements, each with a proposed limit on the 99th percentile (p99) over
at least 1,000 samples. The limits are proposals to check against; change them if
your science needs tighter or can accept looser.

| Measurement | What it is | Proposed limit |
|---|---|---|
| `request_to_edge_us` | From asking for a BNC OUT pulse to the line rising on the wire | 1,000 µs |
| `width_error_us` | Measured pulse width minus the width asked for | 200 µs (either direction) |
| `event_to_pulse_us` | From a CAN frame's kernel arrival stamp to BNC OUT rising | 1,000 µs |

Both ends are timed by the kernel (libgpiod edge stamps, SocketCAN arrival
stamp), so the numbers describe the wire, not what Python believes happened.

Not included: the time a node takes to send an event, and the CAN bus time
(about 0.5 ms a frame at 250 kbit/s). Those are separate, and are measured with
the node, not with this tool.

## What you need

- The Raspberry Pi 5 base station with the CAN HAT fitted.
- One BNC cable from **BNC OUT** to **BNC IN 0** (a short jumper at the
  connectors is fine). Nothing else on either connector.
- `gpiod` (the Python bindings, version 2) installed. `RPi.GPIO` is not enough:
  it gives no edge timestamps and the tool will refuse to run.
- A terminal on the Pi, in `packages/dev_gui`.
- Optional, for a cross-check: a DAQ or oscilloscope that can record BNC OUT.

## The two tools

`python -m bench.bnc_loopback pulses|event` times the BNC code on its own (this document's steps).
Options for finding what disturbs the timing: `--load-procs N` (N busy programs on other cores),
`--load` (a busy thread inside the test: it competes for the interpreter lock), `--rt PRIO` (real-time
priority, needs privileges), `--pin-cpu N`, `--gc off|freeze`, `--switch-interval-us N`. Every run
keeps its raw samples in the `--out` file and prints how many were slower than 0.25, 0.5, 1, 2 and
5 ms, and which ones were slowest.

`python -m bench.engine_loopback` times the real engine end to end. It starts its own engine with
scratch settings (your saved BNC settings, node map and logs are not touched), connects it to a virtual
CAN bus, sends node events and reads the delays from the engine's own log. Options: `--realtime PRIO
--grant-rt` (start the engine with real-time priority and the limit it needs), `--gc`,
`--can-traffic-fps N`, `--load-procs N`, `--sender-rt PRIO`. It refuses to use a real CAN interface.
Stop the engine service first (`systemctl --user stop sfm-engine`) and start it again afterwards.

## Before you start

1. **Nothing else may hold the GPIO lines.** Close the DearPyGui GUI and any
   other base-station process first. Both would fight the tool for the pins.
2. Note down, for the report:
   ```
   uname -r
   cat /etc/os-release | head -2
   python3 -c "import gpiod; print(gpiod.__version__)"
   free -h
   vcgencmd get_throttled        # 0x0 means no throttling
   ```
3. Check the GPIO chip is the right one and BNC OUT starts low. The boot config
   needs `gpio=6=op,dl` (see `deploy/boot-config-can.append.txt`).
   ```
   gpioinfo | grep -E "pinctrl|GPIO6\b|GPIO12\b|GPIO13\b"
   ```
4. Confirm the cable is on **BNC IN 0** (the tool's default; `--in-channel 1`
   uses BNC IN 1).

## Steps

Save every run with `--out` so the numbers can be sent back. A name such as
`pulses-10ms-idle.json` is enough.

### 1. Smoke test and polarity (2 minutes)

```
python -m bench.bnc_loopback pulses --count 20 --width-ms 10 --out smoke.json
```

Check the report:

- `missed` is 0. If not, the cable or channel is wrong.
- `observed_width_us` is close to 10,000. **If it is close to the gap between
  pulses instead, the input polarity is the other way round** from what the code
  assumes (the HAT inverts the signal, so a raw rising pin edge is a falling BNC
  edge). Stop and report this; nothing else is meaningful until it is settled.

### 2. Output accuracy, idle machine

```
python -m bench.bnc_loopback pulses --count 1000 --width-ms 10  --out pulses-10ms-idle.json
python -m bench.bnc_loopback pulses --count 1000 --width-ms 1   --out pulses-1ms-idle.json
python -m bench.bnc_loopback pulses --count 1000 --width-ms 100 --out pulses-100ms-idle.json
```

The 10 ms width is the one that matters most (it is what other equipment will
usually send and sense). 1 ms shows how short a pulse can be trusted.

### 3. Event to pulse (the headline number)

Use a virtual bus so nothing real receives the test frames:

```
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan    # skip if it already exists
sudo ip link set up vcan0
python -m bench.bnc_loopback event --can vcan0 --count 1000 --width-ms 10 --out event-idle.json
```

Do **not** run the event test on `can0` with nodes connected: the tool sends
frames that look like events from node 1, and the nodes and the base station
would act on them.

### 4. Under load

The point is to see whether anything else on the machine disturbs the timing.
Repeat steps 2 (10 ms only) and 3 under each condition that applies:

| Condition | How |
|---|---|
| CPU busy | add `--load` to the command |
| Heavy CAN traffic | run `python node_simulator.py --interface vcan0 --nodes 6` in another terminal, then the event test |
| Browser open | with Chromium showing a busy page (once the new GUI exists, use it) |
| Display on | the real monitor attached and awake |

Name the files to match, e.g. `event-load.json`, `event-simtraffic.json`.

### 5. A long run

The short runs can miss rare stalls. Start this one and leave it:

```
python -m bench.bnc_loopback pulses --count 3600 --width-ms 10 --gap-ms 950 --out pulses-1h.json
vcgencmd get_throttled
```

Look at `max` as well as p99. A single 20 ms outlier in an hour is a finding.

### 6. Optional: cross-check with an independent recorder

Record BNC OUT on a DAQ or scope while running step 2 at 10 ms. The width it
sees should match `observed_width_us`. This catches a fault in the measurement
itself, such as a wrong polarity or a clock problem.

## Reading the result

For each file, the tool prints a table and `met` / `NOT met` per limit. A file
passes when every limit is met and `missed` is 0.

- **All files meet their limits:** the design is enough. Record the numbers in
  the plan as the confirmed budget. Reduce the limits if the margin is large and
  you want a tighter promise.
- **Idle passes but load fails:** the load is what to fix, not the code. Try, in
  order: run the engine with real-time priority and a fixed core (systemd
  `CPUSchedulingPolicy=fifo`, `CPUSchedulingPriority=50`, `CPUAffinity=3`),
  keep the browser off that core, then `isolcpus=3` on the kernel command line.
  Repeat the failing step after each change.
- **Idle itself misses by a small margin:** the same real-time settings first.
- **Still missing after those:** Python in userspace is the limit. The options
  are a small C helper that owns the BNC lines, or a microcontroller on the HAT
  that does the timing and talks to the base station over CAN or serial. That
  needs a decision and a hardware or firmware change; bring the numbers.
- **Misses (`missed` > 0):** edges are being lost. Check the cable, then
  whether `missed_edges` appears in the log (the kernel sequence number gap).

## What to send back

- The `.json` files from each step.
- The `uname -r`, OS, `gpiod` version, `free -h` and `vcgencmd get_throttled`
  output from *Before you start*.
- Anything odd you saw, including the polarity check from step 1.

With those, the budget goes into the plan and the documentation as a measured
figure, and the limits in `bnc_loopback.py` can be updated to match.
