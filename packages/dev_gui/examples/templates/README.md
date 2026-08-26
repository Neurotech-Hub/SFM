# Custom experiment starters

Copy-from templates for authoring your own task. They are **not** loaded
by the GUI — `resolve_builder()` only imports
`base_station.experiment.templates.<name>`. To make one appear in the
Experiment panel, copy the pair into the engine:

```
packages/dev_gui/
├── experiments/
│   └── <name>.json                         # GUI form + parameter schema
└── base_station/experiment/templates/
    └── <name>.py                           # def build(**kwargs) -> Experiment
```

`template` in the JSON must equal the Python module name. After copying,
the GUI lists it on the next launch; no registry edit is required. Optional:
re-export in `templates/__init__.py` if you want
`from base_station.experiment.templates import <name>`.

## File layout (what you add)

```
experiments/<name>.json          # name, label, template, parameters[]
templates/<name>.py              # build(nodes=None, *, name=..., **params)
tests/test_<name>.py             # optional: make_runner() + inject() (no CAN)
```

A matching report design (optional, in `packages/sfm-analysis`) uses the
same `<name>` in `"matches"` so `sfm-report` picks it automatically — see
`packages/sfm-analysis/examples/report_design/`.

## The two styles

| Starter | Style | Behavior |
|---------|--------|----------|
| [`alternation.py`](alternation.py) + [`.json`](alternation.json) | Event-based (`@exp.on_*`) | Round-robin: after each take, wait, then dispense the next node |
| [`bnc_paced.py`](bnc_paced.py) + [`.json`](bnc_paced.json) | Sequential (`@exp.script`) | BNC rising edge → dispense → wait for take → ITI → repeat |

API reference, fault handling, and `@exp.script` semantics:
[`base_station/experiment/README.md`](../../base_station/experiment/README.md).
