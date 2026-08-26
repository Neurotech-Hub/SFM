# Custom report-design starter

A report **design** is three files that share a name with the experiment
template they describe. Copy them into the package — `resolve_design()`
loads every `designs/*.json`; `resolve_section("module.func")` imports
`sfm_analysis.report.sections.<module>.SECTIONS[func]`.

```
packages/sfm-analysis/src/sfm_analysis/report/
├── designs/
│   └── <name>.json          # section list; "matches": ["<experiment>"]
├── analyses/
│   └── <name>.py            # numbers (plain functions / dataclasses)
└── sections/
    └── <name>.py            # HTML; ends with SECTIONS = {"func": ...}
```

This folder is a copy-from pair for the `alternation` experiment starter
in `packages/dev_gui/examples/templates/`. After copying:

```bash
# from packages/sfm-analysis/
cp examples/report_design/designs/alternation.json src/sfm_analysis/report/designs/
cp examples/report_design/analyses/alternation.py src/sfm_analysis/report/analyses/
cp examples/report_design/sections/alternation.py src/sfm_analysis/report/sections/
```

`sfm-report` then picks this design for any session whose `session_start`
`experiment` field is `"alternation"` (the `name` of the experiment
template). Force it with `--design alternation`.

Without a matching design, the generic `default.json` still renders —
you do not have to ship a design on day one.

Walkthrough of every field and a second, analysis-API-only example:
[docs/ANALYSIS_GUIDE.md](../../docs/ANALYSIS_GUIDE.md#9-turning-an-analysis-into-a-report-section).
