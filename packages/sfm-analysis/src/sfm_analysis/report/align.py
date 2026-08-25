"""align.py — combined-report time alignment.

A combined report merges runs from different sessions (and possibly
different days), each with its own local ``t=0`` at its own session
start. Comparing them meaningfully needs a shared time origin — this
module computes a per-run offset (seconds to add to that run's rows'
``t``) without mutating the RunData itself, so alignment is a pure,
local computation any comparative section can apply to its own chart.

Modes:
  relative — no-op; each run keeps its own t=0 (session.split_runs's default).
  wall     — shift every run onto one shared wall-clock origin (the
             earliest t0_ms among the given runs), for simultaneous
             multi-cage rigs.
  trial    — not time-based; sections needing this use each row's own
             ``trial`` field directly instead of consulting an offset.
  event:<name> — shift each run so the first EXP row named <name> sits at
             t=0. This is the one that matters for a session like
             EXP-Test-02, where run 1 burned ~1441s before the first
             `trial` fired — `--align event:trial` makes it comparable to
             a clean run instead of dominating a `relative`-aligned plot.
"""

from __future__ import annotations

from typing import Dict, List

from .session import RunData


def compute_offsets(runs: List[RunData], align: str) -> Dict[int, float]:
    """{id(run): offset_seconds} — add this to a row's `.t` for the chosen alignment."""
    if not runs:
        return {}

    if align == "wall":
        global_t0 = min(r.t0_ms for r in runs)
        return {id(r): (r.t0_ms - global_t0) / 1000.0 for r in runs}

    if align.startswith("event:"):
        name = align.split(":", 1)[1]
        offsets: Dict[int, float] = {}
        for r in runs:
            match = next((row for row in r.rows if row.source == "EXP" and row.event_name == name), None)
            offsets[id(r)] = -match.t if match is not None else 0.0
        return offsets

    # "relative", "trial", or anything unrecognized: no shift.
    return {id(r): 0.0 for r in runs}
