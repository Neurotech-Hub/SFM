# Rig Map web interface

The page the gateway serves at `http://127.0.0.1:8080/`. Plain JavaScript (ES
modules), SVG and CSS. No framework, no build step, and nothing is loaded from
the internet: the three fonts are in `static/fonts/` (see `LICENSES.md`).

```
index.html          the page shell
static/app.css      design tokens and components
static/js/
  main.js           boot, step navigation, state feed
  api.js            commands (POST) and the WebSocket
  store.js          small store; a state push that changed nothing repaints nothing
  dom.js            h() / s() builders and "update only if changed" setters
  nodes.js          node states, map layout, pad tracking (pure functions)
  views/            connect.js  experiment.js  run.js  map.js  panel.js  logview.js
```

Each view builds its elements once and then only updates what changed, so a
10 Hz state push does not redraw the page. The event log keeps a capped window
of rows; the full record is the CSV.

## Running it

```
python -m base_station.service      # the engine (owns CAN and the BNC lines)
python -m base_station.gateway      # this page, on 127.0.0.1:8080
```

The old DearPyGui app and the engine can't run together; they share a lock.

## Tests

`tests/test_ui_browser.py` drives the page in headless Chromium against a real
engine with a fake CAN bus (`tests/ui_rig.py`, `tests/browser.py`). It skips
when there is no Chromium. The checklist of old controls is in
`docs/RIG_MAP_CHECKLIST.md`.
