# VFM

## Launch the GUI

```
cd Project/VFM/tools/dev_gui
python run.py
```

## About

VFM is a firmware library for the Spatial Foraging Platform node. It provides non-blocking, service-oriented stepper-driven pellet dispensing, CAN bus communication with a base-station command/event/heartbeat protocol.

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
