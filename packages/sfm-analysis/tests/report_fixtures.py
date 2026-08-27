"""report_fixtures.py — shared CSV builders for sfm_analysis.report tests.

Not a conftest.py (matching this test suite's no-conftest convention for
fixture *builders* — see conftest.py's own docstring) — import directly:
``from report_fixtures import row, write_session``.

Every row is keyed by the *real* ``sfm_analysis.logs.CSV_HEADER`` rather
than a hand-copied list. That constant is also what the SFM base
station's LogManager binds as its own CSV_HEADER (see log_manager.py in
the parent repo), so a schema change on either side breaks these tests
loudly instead of silently drifting out of sync.
"""

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from sfm_analysis.logs import CSV_HEADER
from sfm_analysis.protocol import CanEvent

HEADER = CSV_HEADER


def row(
    *,
    ts_ms: int,
    session: str = "S",
    run_id: int = 1,
    trial: int = 0,
    source: str = "CAN",
    direction: str = "RX",
    node_id: int = 0,
    frame_type: str = "EVENT",
    event_name: str = "",
    raw_id: int = 0,
    raw_data: bytes = b"",
    fields: Optional[Dict[str, Any]] = None,
    details: str = "",
) -> List[str]:
    """Build one row, keyed by LogManager.CSV_HEADER, as a list of strings."""
    from datetime import datetime

    values = {
        "timestamp_iso": datetime.fromtimestamp(ts_ms / 1000.0).isoformat(timespec="milliseconds"),
        "timestamp_ms": str(ts_ms),
        "elapsed_s": "",   # deliberately left blank/untrustworthy — loader must ignore it
        "session": session,
        "run_id": str(run_id),
        "trial": str(trial),
        "source": source,
        "direction": direction,
        "node_id": str(node_id),
        "frame_type": frame_type,
        "event_name": event_name,
        "raw_id_hex": f"0x{raw_id:03X}",
        "raw_data_hex": " ".join(f"{b:02X}" for b in raw_data),
        "fields_json": json.dumps(fields) if fields else "",
        "details": details,
    }
    return [values[col] for col in HEADER]


def can_event_row(
    ts_ms: int, node: int, event: CanEvent, extra: bytes = b"", **kw
) -> List[str]:
    """
    A CAN EVENT row for the given node/event, raw_data = event byte + extra.

    Mirrors app.py's Fault special-case (app.py:1448-1451): event_name
    becomes "Fault: <ServiceStatus name>" rather than plain "Fault", since
    that's what a real log actually contains.
    """
    from sfm_analysis.protocol import CAN_EVENT_DISPLAY_NAME, ServiceStatus

    if event == CanEvent.Fault and "event_name" not in kw:
        code_name = "unknown"
        if extra:
            try:
                code_name = ServiceStatus(extra[0]).name
            except ValueError:
                pass
        name = f"Fault: {code_name}"
    else:
        name = kw.pop("event_name", CAN_EVENT_DISPLAY_NAME.get(event, event.name))
    return row(
        ts_ms=ts_ms, node_id=node, frame_type="EVENT", source="CAN",
        event_name=name, raw_id=0x300 + node,
        raw_data=bytes([event.value]) + extra, **kw,
    )


def input_changed_row(ts_ms: int, node: int, input_id: int, active: bool, name: str, **kw) -> List[str]:
    """A CAN EVENT row for a raw InputChanged edge (raw byte0 = 0x06)."""
    return row(
        ts_ms=ts_ms, node_id=node, frame_type="EVENT", source="CAN",
        event_name=name, raw_id=0x300 + node,
        raw_data=bytes([CanEvent.InputChanged.value, input_id, 1 if active else 0]),
        **kw,
    )


def heartbeat_row(ts_ms: int, node: int, payload: bytes, **kw) -> List[str]:
    return row(
        ts_ms=ts_ms, node_id=node, frame_type="HEARTBEAT", source="CAN",
        event_name="", raw_id=0x200 + node, raw_data=payload, **kw,
    )


def exp_row(ts_ms: int, name: str, fields: Optional[Dict[str, Any]] = None, node: int = 0, trial: int = 0, **kw) -> List[str]:
    """An EXPERIMENT row (source=EXP)."""
    return row(
        ts_ms=ts_ms, node_id=node, trial=trial, frame_type="EXPERIMENT", source="EXP",
        direction="SYS", event_name=name, fields=fields or {}, **kw,
    )


def session_open_row(ts_ms: int, session: str, run_id: int, mode: str = "create") -> List[str]:
    return row(
        ts_ms=ts_ms, session=session, run_id=run_id, frame_type="SESSION_OPEN",
        source="CAN", direction="SYS", event_name="SessionOpen",
        details=f"session={session} run={run_id} mode={mode} path=/tmp/{session}.csv",
    )


def write_csv(path: Path, rows: List[List[str]]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return path


def write_session(tmp_path: Path, rows: List[List[str]], *, session: str = "S",
                   heartbeat_rows: Optional[List[List[str]]] = None) -> Path:
    """Write a session CSV (and optional heartbeats sibling) under tmp_path."""
    path = write_csv(tmp_path / f"{session}.csv", rows)
    if heartbeat_rows is not None:
        write_csv(tmp_path / f"{session}_heartbeats.csv", heartbeat_rows)
    return path


def legacy9_file(tmp_path: Path, name: str = "legacy") -> Path:
    """A file matching the old 9-column schema (no fields_json, no run_id)."""
    header = ["timestamp_iso", "timestamp_ms", "direction", "node_id", "frame_type",
              "event_name", "raw_id_hex", "raw_data_hex", "details"]
    path = tmp_path / f"{name}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerow(["2026-08-12T15:09:00.392", "1786565340392", "RX", "1",
                    "EVENT", "Loaded", "0x301", "02", ""])
    return path


def exp6_file(tmp_path: Path, name: str = "experiment_free_feeding") -> Path:
    """A file matching the legacy 6-column experiment CSV schema."""
    header = ["timestamp_iso", "timestamp_ms", "elapsed_s", "name", "node_id", "fields"]
    path = tmp_path / f"{name}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerow(["2026-08-12T15:09:00.392", "1786565340392", "0.000",
                    "session_start", "0", "experiment=free_feeding nodes=[1, 2]"])
    return path


def heartbeat_payload(
    state: int = 0, presented: int = 0, presence: bool = False,
    pellet: bool = False, load_position: bool = False, dome_open: bool = False,
    fault: int = 0, taken: int = 0,
) -> bytes:
    """Build an 8-byte heartbeat payload matching protocol.parse_heartbeat's layout."""
    sensor_bits = (int(pellet) << 0) | (int(load_position) << 1) | (int(dome_open) << 2)
    return bytes([
        state, presented & 0xFF, (presented >> 8) & 0xFF, int(presence),
        sensor_bits, fault, taken & 0xFF, (taken >> 8) & 0xFF,
    ])


def bandit_run(
    *,
    t0_ms: int = 1_700_000_000_000,
    session: str = "Bandit01",
    run_id: int = 1,
    n_trials: int = 5,
    block_size: int = 3,
    arm_a: int = 1,
    arm_b: int = 2,
    seed: int = 12345,
) -> List[List[str]]:
    """
    A minimal, self-consistent two_armed_bandit run: session_start, per-trial
    bandit_trial/arm_presented/bandit_trial_end rows with a presence event
    right after arm_presented so first_visit_node is derivable, and
    session_end. Trial k alternates rich arm every block_size trials.
    """
    rows: List[List[str]] = []
    t = t0_ms

    def adv(ms: int) -> int:
        nonlocal t
        t += ms
        return t

    rows.append(session_open_row(t, session, run_id))
    rows.append(exp_row(t, "session_start", {
        "experiment": "two_armed_bandit", "nodes": [arm_a, arm_b], "seed": seed,
    }, trial=0, session=session, run_id=run_id))
    rows.append(exp_row(adv(10), "two_armed_bandit_start", {
        "nodes": [arm_a, arm_b], "arm_a": arm_a, "arm_b": arm_b,
        "block_size": block_size, "p_high": 0.9,
        "next_trial_wait": "fixed_delay", "seed": seed,
    }, session=session, run_id=run_id))

    for trial in range(1, n_trials + 1):
        block = (trial - 1) // block_size
        rich, lean = (arm_a, arm_b) if block % 2 == 0 else (arm_b, arm_a)
        rows.append(exp_row(adv(1000), "trial", {"trial": trial},
                             trial=trial, session=session, run_id=run_id))
        rows.append(exp_row(adv(50), "bandit_trial", {
            "trial": trial, "block": block, "rich": rich, "lean": lean,
            "fed": rich, "empty": lean, "fed_accepted": 1, "empty_accepted": 1,
        }, trial=trial, session=session, run_id=run_id))
        # both arms present at ~the same instant; presented_t is the LATER one
        rows.append(exp_row(adv(5), "arm_presented", {
            "node": rich, "trial": trial, "role": "fed", "presented": "pellet", "delivered": 1,
        }, node=rich, trial=trial, session=session, run_id=run_id))
        t_presented = adv(2)
        rows.append(exp_row(t_presented, "arm_presented", {
            "node": lean, "trial": trial, "role": "empty", "presented": "empty", "delivered": 0,
        }, node=lean, trial=trial, session=session, run_id=run_id))
        # animal visits the rich (correct) arm first, every trial in this fixture
        visit_t = adv(300)
        rows.append(can_event_row(visit_t, rich, CanEvent.OnPlate,
                                   session=session, run_id=run_id))
        rows.append(input_changed_row(visit_t + 5, rich, 4, True, "MousePresence Detected",
                                       session=session, run_id=run_id))
        take_t = adv(500)
        rows.append(can_event_row(take_t, rich, CanEvent.PelletTaken, bytes([trial & 0xFF, 0, 1]),
                                   session=session, run_id=run_id))
        rows.append(input_changed_row(take_t + 200, rich, 4, False, "MousePresence Cleared",
                                       session=session, run_id=run_id))
        rows.append(exp_row(adv(20), "bandit_trial_end", {
            "trial": trial, "block": block, "rich": rich, "lean": lean, "fed": rich, "empty": lean,
            "delivered_node": rich, "baited_arms": 1, "taken_node": rich,
            "outcome": "taken", "valid": 1,
        }, trial=trial, session=session, run_id=run_id))

    rows.append(exp_row(adv(50), "two_armed_bandit_end", {
        "trials": n_trials, "trials_invalid": 0, "pellets": n_trials, "elapsed_s": (t - t0_ms) / 1000.0,
    }, session=session, run_id=run_id))
    rows.append(exp_row(adv(5), "session_end", {
        "elapsed_s": (t - t0_ms) / 1000.0, "pellets": n_trials,
    }, session=session, run_id=run_id))
    return rows
