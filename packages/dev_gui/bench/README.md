# BNC timing: bench confirmation

**Status: not done yet.** The BNC timing code is built and unit-tested, but the
timing budget below has never been measured on real hardware. Until it is, treat
every number here as a target, not a result. Development of the GUI and gateway
does not depend on it; the numbers decide whether the timing design is enough or
needs more (see *If a target is missed*).

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
