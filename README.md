# VFM

Spatial Foraging Platform: node firmware, base-station developer GUI, and
session analysis. Each application lives in its own directory.

## Launch the GUI

```
cd Project/VFM/packages/dev_gui
python run.py
```

## Generate a behavior report

```
cd Project/VFM/packages/dev_gui
python run_report.py
```

With no arguments this opens an interactive session picker. For list/combine, date filters, designs, and other use cases, see [Behavior reports](packages/dev_gui/README.md#behavior-reports-session-csv--printable-html) in the GUI README.

Report generation itself is cross-platform — it doesn't need a Raspberry Pi or any base-station hardware library. It ships as its own package, [`sfm-analysis`](packages/sfm-analysis/), installable with `pip install sfm-analysis` on Windows, macOS, or Linux; `run_report.py` above is a thin wrapper around its `sfm-report` CLI. See [packages/sfm-analysis/README.md](packages/sfm-analysis/README.md).

## About

VFM is a firmware library for the Spatial Foraging Platform node. It provides non-blocking, service-oriented stepper-driven pellet dispensing, and talks to the base station over **CAN** (Controller Area Network) — the shared communication bus every node is wired onto. A **CAN event** is a message a node posted on that bus (Loaded, Pellet Taken, Fault, …).

The Arduino library is the `firmware/` folder (not the repo root). Copy or symlink `firmware/` into `Arduino/libraries/VFM`, or zip that folder and add it via *Sketch → Include Library → Add .ZIP Library…*.

## Documentation

| Doc | What it's for |
|-----|---------------|
| [packages/dev_gui/README.md](packages/dev_gui/README.md) | Base-station GUI: install, run, CAN bring-up, experiments, session logs, and behavior reports |
| [packages/dev_gui/deploy/README.md](packages/dev_gui/deploy/README.md) | One-time Raspberry Pi CAN HAT setup (`can0`) before running against real hardware |
| [packages/dev_gui/base_station/experiment/README.md](packages/dev_gui/base_station/experiment/README.md) | How to author custom experiment templates (Python API + JSON params) |
| [packages/sfm-analysis/README.md](packages/sfm-analysis/README.md) | Cross-platform session analysis and report generation — install, CLI, report design/section architecture, Python API |
| [packages/sfm-analysis/docs/ANALYSIS_GUIDE.md](packages/sfm-analysis/docs/ANALYSIS_GUIDE.md) | Analysis reference: domain vocabulary (bout, cycle, ready, …), every log column, event name, derived metric field, and tidy-table column, plus a worked example |
| [packages/sfm-analysis/docs/PYPI.md](packages/sfm-analysis/docs/PYPI.md) | Publishing `sfm-analysis` to PyPI (`pip install sfm-analysis`) |
| [firmware/docs/WIRING.md](firmware/docs/WIRING.md) | Firmware pin names → physical jobs (motors, sensors, CAN, LEDs, discovery) |
| [firmware/docs/DISPENSE_CYCLE.md](firmware/docs/DISPENSE_CYCLE.md) | How a node loads, raises, and reports pellet take / faults during a dispense |
| [firmware/docs/HARDCODED_VALUES.md](firmware/docs/HARDCODED_VALUES.md) | Tunable firmware constants checklist (timings, speeds, thresholds) |
| [packages/dev_gui/docs/BASE_STATION_HARDCODED_VALUES.md](packages/dev_gui/docs/BASE_STATION_HARDCODED_VALUES.md) | Base-station timings and defaults (GUI, experiments, reports) |

## Project structure

```
VFM/
├── firmware/                 # Arduino library (ESP32-S3 node)
│   ├── src/                  # VFM.h / services / pin definitions
│   ├── examples/             # Node sketch + hardware bring-up sketches
│   ├── docs/                 # Wiring, dispense cycle, firmware tunables
│   └── library.properties
├── packages/
│   ├── dev_gui/              # Raspberry Pi developer GUI + CAN tooling
│   │   ├── experiments/      # JSON schemas for built-in experiments
│   │   ├── examples/templates/  # Copy-from custom experiment starters
│   │   └── base_station/experiment/  # Engine + templates (see its README)
│   └── sfm-analysis/         # Cross-platform session analysis / report SDK
│       ├── docs/ANALYSIS_GUIDE.md
│       ├── docs/PYPI.md      # To publish a new sfm-analysis version (Developer only)
│       └── examples/         # Analysis recipes + a custom report-design starter
└── README.md
```
