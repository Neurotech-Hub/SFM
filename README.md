# VFM

## Launch the GUI

```
cd Project/VFM/tools/dev_gui
python run.py
```

## Generate a behavior report

```
cd Project/VFM/tools/dev_gui
python run_report.py
```

With no arguments this opens an interactive session picker. For list/combine, date filters, designs, and other use cases, see [Behavior reports](tools/dev_gui/README.md#behavior-reports-session-csv--printable-html) in the GUI README.

## About

VFM is a firmware library for the Spatial Foraging Platform node. It provides non-blocking, service-oriented stepper-driven pellet dispensing, CAN bus communication with a base-station command/event/heartbeat protocol.

## Documentation

| Doc | What it's for |
|-----|---------------|
| [tools/dev_gui/README.md](tools/dev_gui/README.md) | Base-station GUI: install, run, CAN bring-up, experiments, session logs, and behavior reports |
| [tools/dev_gui/deploy/README.md](tools/dev_gui/deploy/README.md) | One-time Raspberry Pi CAN HAT setup (`can0`) before running against real hardware |
| [tools/dev_gui/base_station/experiment/README.md](tools/dev_gui/base_station/experiment/README.md) | How to author custom experiment templates (Python API + JSON params) |
| [docs/WIRING.md](docs/WIRING.md) | Firmware pin names → physical jobs (motors, sensors, CAN, LEDs, discovery) |
| [docs/DISPENSE_CYCLE.md](docs/DISPENSE_CYCLE.md) | How a node loads, raises, and reports pellet take / faults during a dispense |
| [docs/HARDCODED_VALUES.md](docs/HARDCODED_VALUES.md) | Tunable firmware constants checklist (timings, speeds, thresholds) |

## Project structure

```
VFM/
├── src/                  # Arduino library (ESP32-S3 node firmware)
│   ├── VFM.h / VFM.cpp   # Library entry point
│   ├── hardware/         # Pin definitions
│   └── services/         # CAN bus, dispenser, node identity, presence, LED services
├── examples/             # Arduino sketches
│   ├── Node/             # Main node firmware sketch
│   └── Troubleshooting/  # Hardware bring-up / diagnostic sketches
├── docs/                 # Wiring, dispense cycle, and hardcoded-value reference docs
├── tools/dev_gui/        # Python base-station GUI + CAN tooling (see tools/dev_gui/README.md)
└── library.properties    # Arduino library metadata
```
