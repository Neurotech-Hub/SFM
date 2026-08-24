# sfm-analysis

Cross-platform analysis and printable HTML behavior reports for
[VFM](https://github.com/Neurotech-Hub/VFM) / SFM feeder session logs.
Runs on Windows, macOS, and Linux — no Raspberry Pi, no hardware
libraries. Pure standard library: no jinja2, no pandas, no matplotlib. A
report is one self-contained HTML file with inline SVG charts, printable
via Ctrl+P.

This package is carved out of the VFM repository's `tools/dev_gui/`
(the Raspberry Pi base station that records the logs) precisely so that
analysis doesn't have to happen on the Pi. The base station's own
`run_report.py` is a thin wrapper around the CLI documented below.

## Install

```bash
pip install sfm-analysis
```

Or, before the first PyPI release (or to track `main`):

```bash
pip install "git+https://github.com/Neurotech-Hub/VFM.git#subdirectory=packages/sfm-analysis"
```

## Quick start

```bash
# Try it right now, with no rig, no log directory, and no real data:
sfm-report --demo --open

# List every session found in the default log directory
# ($SFM_LOG_DIR, else an external drive's sfm_logs/, else ~/sfm_logs):
sfm-report --list

# One session, opened in your browser when done:
sfm-report EXP-Test-02 --open

# Every session for a cohort, combined into one comparative report:
sfm-report "cohortA_*" --combine -o /tmp/cohortA.html
```

`--demo` renders a real, bundled two_armed_bandit session
(`sfm_analysis.report.demo`) — the fastest way to see what a report looks
like on a fresh install.

## The `sfm-report` CLI

```
sfm-report --help

  target...         Session name, shell glob, or path to a .csv (repeatable)
  --log-dir         Default: $SFM_LOG_DIR, else an external drive, else ~/sfm_logs
  --list            List discovered sessions and exit
  --list-designs    List report designs and exit
  --check-names     Show how each session name parses (subject/cohort/day) and exit
  --combine         One comparative report over all targets
  --all             Every session in --log-dir
  --demo            Render the bundled demo session (no rig or log dir needed)
  --since, --until  Filter by date (YYYY-MM-DD)
  --run             Only this run_id (a file can hold several)
  --design          Force a report design instead of resolving by experiment
  --align           Combined-report time alignment: relative | wall | trial | event:<name>
  --out, -o         Output file (single report) or directory (multiple)
  --open            Open the result in your default browser when done
```

A **session** is one CSV written by the base station's unified 15-column
log format (`timestamp_iso, timestamp_ms, ..., fields_json, details`).
Files in an older schema are skipped with a warning rather than crashing
the run — see `report.loader.sniff_schema`.

## How a report is put together

A report **design** (`report/designs/<name>.json`) declares which
**sections** to render and in what order; sections are plain Python
functions registered under `report/sections/`. This mirrors the VFM
experiment engine's own JSON-schema/Python split (`base_station/experiment/`
in the parent repo — `matches: []` in a design plays the same role as
`build(**kwargs)` in an experiment template).

The design is picked automatically from the session's `experiment` field
(the fields_json on its `session_start` row), falling back to
`default.json` for anything without a matching design or with no
`session_start` at all — see `resolve_design`. Force one explicitly with
`--design <name>` or `build_session_report(..., design=...)`.

Metrics (retrieval latency, presence bouts, dispense cycles, bandit
choice/WSLS/reversal, …) live in `report/analyses/` and
`report/metrics.py`, separate from the HTML that renders them — the
metrics are plain dataclasses, computed once per run
(`compute_run_metrics`) and handed to every section read-only. This is
what makes the numbers themselves testable without parsing HTML (see
`tests/test_report_metrics.py`, which never touches `render_report_html`
at all).

Add a new comparative view or per-experiment section by writing a
function that returns a `SectionResult` (`report/schema.py`) and adding
its `module.function` ref to the relevant `report/designs/*.json` — no
changes to the CLI or the render pipeline needed. A section that raises
never kills the whole report; it renders a visible error block with the
traceback instead (`schema.run_section`), so one broken section can't take
the rest of the report down with it.

## Python API

### Custom analysis: `sfm_analysis.analysis`

This is the entry point for asking your own question of a session — one
import, tidy `list[dict]` tables, and a small generic interval algebra
for "was X happening while Y" questions, rather than five imports across
four modules or a bespoke query language.

```python
from sfm_analysis.analysis import load_session

s = load_session("EXP-Test-02")      # name, glob, or path — same resolution as the CLI
cycles = s.cycles_table()            # list[dict], one row per dispense cycle
bouts = s.bouts_table()              # presence / dome / fault intervals, unified

import pandas as pd
df = pd.DataFrame(cycles)            # or: from sfm_analysis.analysis import to_dataframe
df.groupby("node")["retrieval_latency"].median()
```

`load_session` resolves a target exactly the way `sfm-report` does
(`$SFM_LOG_DIR`, then an external drive, then `~/sfm_logs`, or pass
`log_dir=...`), loads the CSV, splits it into runs, and precomputes
metrics for every run once. If the file holds more than one run — a
9-row aborted restart followed by the real session, say — `s.run()`
picks the run with the most rows by default; pass `run_id=` anywhere to
be explicit instead.

**Tidy tables** (`s.cycles_table()`, `s.bouts_table()`, `s.events_table()`,
`s.trials_table()`) are the fastest way to get real freedom: every row is
a plain dict with the same keys, ready for `pandas.DataFrame`, `csv.DictWriter`,
or a plain list comprehension. `to_dataframe(rows)` is the explicit,
optional pandas handoff (`pip install sfm-analysis[pandas]`) — the core
package stays pandas-free.

**Interval algebra** (`sfm_analysis.analysis.intervals`) answers the
class of question tidy tables alone don't: `overlap`, `subtract`,
`merge`, `around`, `count_in`, `rate_in`, all plain functions over
`(t0, t1)` second-pairs.

| Question | One-liner |
|---|---|
| Windows where dome-open overlapped presence | `overlap(dome_iv, presence_iv)` |
| Takes within 30s after a fault started | `count_in(take_times, around(fault_starts, (0, 30)))` |
| Pellet-take rate during a time window | `rate_in(take_times, [(t0, t1)])` (takes/second) |
| Time NOT covered by any dome-open bout | `subtract([(0, run.duration_s)], dome_iv)` |

```python
from sfm_analysis.analysis import intervals as iv

run = s.run()
m = s.metrics_for()
presence_iv = iv.as_intervals(m.presence[0], run_end=run.duration_s)
dome_iv = iv.as_intervals(m.dome[0], run_end=run.duration_s)
len(iv.overlap(dome_iv, presence_iv))     # dome-open bouts that overlapped presence
```

`as_intervals` is the one deliberately strict step: a bout still open
when the run ended (`censored=True`) has `t1=None`, and this refuses to
guess whether that means zero-length or unbounded — pass `run_end=` to
resolve it explicitly, or filter censored bouts out yourself first.

### Lower-level: `sfm_analysis.report`

`sfm_analysis.analysis` is built entirely on public functions in
`sfm_analysis.report` — nothing is hidden from you, and dropping down is
one import away if you need something the curated API doesn't expose yet
(a report design's own analysis module, e.g. `report.analyses.bandit`,
for instance). This is also how the report itself gets built
programmatically:

```python
from pathlib import Path
from sfm_analysis.report import build_session_report, build_combined_report, load_runs

build_session_report(Path("EXP-Test-02.csv"), out_path=Path("report.html"))
build_combined_report([Path("A.csv"), Path("B.csv")], design="two_armed_bandit")

runs = load_runs(Path("EXP-Test-02.csv"))   # what load_session uses internally
```

### Five things to know before trusting a number

Each of these is enforced or worked around somewhere in this codebase,
but none of it is obvious from the CSV alone — `sfm_analysis.analysis`
handles #1, #3, and #4 for you (via `Session`/`load_runs`,
`bouts_table`/`dome_bouts`, and `as_intervals`'s required `run_end`,
respectively), but they're worth understanding regardless of which layer
you work at:

1. **Scope everything to `(session, run_id)`.** A single CSV can hold
   several runs — reopening a session name appends to the same file and
   increments `run_id`. A cumulative count or an inter-trial interval that
   crosses a run boundary is meaningless. `split_runs` is the one place
   that groups rows this way; always work from its output, not the raw
   `LogRow` list.
2. **Never read `elapsed_s` directly.** It's *run-relative* and resets to
   0 every time a session is reopened. Use `RunData` rows' `.t`
   (run-relative seconds recomputed from `timestamp_ms` by `split_runs`)
   instead.
3. **`"Dome Opened"` is two different wire events.** The
   `CanEvent.DomeOpened` milestone (raw byte 0 = `0x03`, carries a pellet
   count) and a renamed `InputChanged` dome edge (raw byte 0 = `0x06`)
   both render as the same `event_name`. Pairing opens/closes by name
   alone double-counts every opening. Use `LogRow.can_event` to
   disambiguate (see `metrics.dome_bouts`, which already does this dedup —
   prefer it over rolling your own).
4. **A bout still open when the run ends is `censored`, not zero-length.**
   `Bout.dur` is `None` for a censored bout on purpose. Filter deliberately
   rather than letting a `None` silently become a `0` in an average.
5. **Two different "pellets presented" counts exist, on purpose.**
   `PelletAccounting.presented` counts rows actually seen in the log;
   `.presented_total` (what you almost always want) uses the base-station
   ledger's gap-corrected total when available, which also counts pellets
   whose own event frame never arrived. `.presented_total` /
   `.taken_total` are the ledger-corrected numbers; the bare `.presented`
   / `.taken` fields are what a naive `by_event("Loaded")` count gets you.

## Recipes

`examples/analysis/` has runnable, commented scripts for each cookbook
question above — copy one as a starting point rather than writing from
scratch:

| Script | Question |
|---|---|
| [`dome_openings_during_presence.py`](examples/analysis/dome_openings_during_presence.py) | Windows where the dome was open while the mouse was present |
| [`retrieval_latency_by_node.py`](examples/analysis/retrieval_latency_by_node.py) | Per-node summary stats, stdlib-only and pandas paths side by side |
| [`takes_after_fault.py`](examples/analysis/takes_after_fault.py) | Pellet takes within a window after each fault interval started |

Every one of them runs standalone against the bundled demo session, no
rig or `--log-dir` needed:

```bash
python examples/analysis/dome_openings_during_presence.py
```

They're also executed in CI (`tests/test_examples.py`), with their
output pinned against the demo session's known ground truth — a change
that breaks a documented recipe fails the build instead of being found
by the next person who copies one.

## Testing your own analysis code

The test suite's own fixture builders are the fastest way to get a
synthetic session without a real rig — `tests/report_fixtures.py`:

```python
from report_fixtures import bandit_run, write_session
from sfm_analysis.report.loader import load_rows
from sfm_analysis.report.session import split_runs

def test_my_analysis(tmp_path):
    rows = bandit_run(n_trials=5, session="synthetic")
    csv_path = write_session(tmp_path, rows, session="synthetic")
    loaded, _, _ = load_rows(csv_path)
    runs = split_runs(loaded, [], csv_path)
    # ... assert against my_analysis_fn(runs[0])
```

There's also a real field-recorded fixture bundled with the package
itself, `sfm_analysis.report.demo` (the same session `--demo` renders) —
a genuine two_armed_bandit session with a fault interval, a dome
milestone/edge dedup case, and post-`session_end` rows, independently
verified against the raw CSV (see `test_report_smoke.py`'s docstring for
the exact numbers):

```python
from sfm_analysis.analysis import load_session
from sfm_analysis.report.demo import DEMO_SESSION_PATH

s = load_session(str(DEMO_SESSION_PATH))
```

Point the smoke test at a different real file with
`SFM_SMOKE_CSV=/path/to/session.csv`.

```bash
cd packages/sfm-analysis
pip install -e ".[dev]"
pytest
```

## Design notes worth knowing

- **Self-contained is a hard invariant.** No `<script>`, no `http(s)://`
  reference — the whole report must render and print with the network
  unplugged (`report/render.py`'s module docstring; enforced by
  `tests/test_report_render.py`).
- **Every chart binds color + dash pattern + marker glyph + hatch texture
  to the same ordinal "key"** (`report/style.py`), so a series never
  depends on hue alone — this matters most exactly when a report is
  printed in grayscale.
- **A section function must not touch the filesystem.** `SectionContext`
  is built once and handed to every section read-only; all the data a
  section could need is already on `ctx.runs` / `ctx.metrics`.

## Firmware/SDK version skew

`sfm_analysis.protocol` is a Python mirror of the VFM firmware's
`src/services/ServiceTypes.h` and `src/services/CanService.h` (parent
repo). Because this package now versions and ships independently of the
firmware, it's possible for a base station running old firmware to be
paired with a newer `sfm-analysis`, or vice versa. There's no version
check yet — if you're seeing CAN events fail to decode, check that the
firmware and this package were built from compatible commits.
