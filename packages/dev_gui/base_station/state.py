"""
state.py — a JSON-safe picture of the engine, for front-ends that live in another process.

``build_snapshot(engine)`` reads the engine and returns plain dicts, lists,
strings, numbers and booleans, so it can go straight to ``json.dumps``. It must
run on the thread that owns the engine (``EngineHost`` does this); it only
reads, and never changes anything.

It knows the engine's private attributes on purpose. It is part of the same
package and changes together with it, which keeps the engine free of any
display code.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .log_manager import LogEntry

SNAPSHOT_VERSION = 1


def _round(value: Optional[float], places: int = 1) -> Optional[float]:
    return None if value is None else round(float(value), places)


def node_dict(node: Any, tally: Any = None, position: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """One node, as the Rig Map and node panel need it."""
    return {
        "id": node.node_id,
        "label": node.label,
        "mac": node.mac_str if node.mac else None,
        "firmware": node.firmware_version,
        "discovery": node.discovery_state,
        "status": node.status_label,
        "online": bool(node.online),
        "fault": node.fault_code.name,
        "presence": bool(node.presence),
        "pellet": bool(node.pellet),
        "load_position": bool(node.load_position),
        "dome_open": bool(node.dome_open),
        "dome_open_warning": bool(node.dome_open_warning),
        "presented_empty": bool(node.presented_empty),
        # Whole seconds: a finer value would make every push differ and every page repaint.
        "heartbeat_age_s": None if node.heartbeat_age_s is None else int(node.heartbeat_age_s),
        "presence_threshold": node.presence_threshold,
        "presence_cal_ok": node.presence_cal_ok,
        "presence_factor": node.presence_factor,
        "pellets_presented": None if tally is None else tally.presented,
        "pellets_taken": None if tally is None else tally.taken,
        # Where the operator put it on the map (fractions of the map), or None.
        "position": position,
    }


def bnc_input_dict(which: str, cfg: Any, level: Optional[bool]) -> Dict[str, Any]:
    return {
        "which": which,
        "label": cfg.label,
        "enabled": bool(cfg.enabled),
        "mode": cfg.mode,
        "action": cfg.action,
        "release_action": cfg.release_action,
        "rising_action": cfg.rising_action,
        "falling_action": cfg.falling_action,
        "active_high": bool(cfg.active_high),
        "min_width_ms": cfg.min_width_ms,
        "level": level,
    }


def bnc_out_dict(cfg: Any) -> Dict[str, Any]:
    return {
        "label": cfg.label,
        "mapping": cfg.mapping,
        "trigger": cfg.trigger,
        "pulse_width_us": cfg.pulse_width_us,
        "sync_width_us": cfg.sync_width_us,
        "periodic_interval_s": cfg.periodic_interval_s,
    }


def _notify_dict(notifier: Any) -> Dict[str, Any]:
    """Whether alerts can go out, and how the last send went. The topic is deliberately not here."""
    last = notifier.last_result
    return {
        "enabled": bool(notifier.settings.enabled),
        "configured": bool(notifier.settings.topic.strip()),
        "last": None if last is None else {"when": last.when, "ok": bool(last.ok), "detail": last.detail},
    }


def log_row_dict(entry: LogEntry) -> Dict[str, Any]:
    """One log row for the event table. The full record stays in the CSV."""
    return {
        "seq": entry.seq,
        "time": entry.timestamp_str,
        "ts": entry.timestamp,
        "node": entry.node_id,
        "dir": entry.direction,
        "type": entry.frame_type,
        "event": entry.event_name,
        "id": entry.raw_id_hex,
        "data": entry.raw_data_hex,
        "details": entry.details,
        "source": entry.source,
    }


def _level(io: Any, which: str) -> Optional[bool]:
    """Current logical level of a BNC input, or None when there is no GPIO."""
    if not getattr(io, "is_available", False):
        return None
    try:
        return bool(io.read_bnc_in1() if which == "IN1" else io.read_bnc_in2())
    except Exception:
        return None


def build_snapshot(engine: Any) -> Dict[str, Any]:
    """The whole engine state, now."""
    registry = engine._registry
    exp = engine._exp
    log = engine._log
    runner = exp.runner

    tallies = engine._experiment_pellet_tallies() or {}
    presented = sum(t.presented for t in tallies.values())
    taken = sum(t.taken for t in tallies.values())

    running = bool(exp.is_running)
    session: Dict[str, Any] = {
        "running": running,
        "armed": bool(engine.start_armed),
        "status": exp.status_line(),
        "experiment": None,
        "name": log.session_name if log is not None else "",
        "run_id": log.run_id if log is not None else 0,
        "elapsed_s": None,
        "trial": 0,
        "pellets_presented": presented if tallies else None,
        "pellets_taken": taken if tallies else None,
        "stop_reason": None,
        # What the operator chose to start it with: {"experiment", "values", "session_name"}, or None.
        "setup": getattr(engine, "session_setup", None),
    }
    if runner is not None:
        session["experiment"] = getattr(runner.experiment, "name", None)
        session["elapsed_s"] = _round(runner.ctx.elapsed())
        session["trial"] = getattr(log, "_trial", 0) if log is not None else 0
        if runner.is_finished:
            session["stop_reason"] = runner.ctx.stop_reason or None

    nodes: List[Dict[str, Any]] = []
    if registry is not None:
        layout = engine._layout
        nodes = [
            node_dict(n, tallies.get(n.node_id), layout.get(n.mac) if layout is not None else None)
            for n in registry.all_nodes()
        ]

    io = engine._io
    notice = engine.last_notice
    return {
        "version": SNAPSHOT_VERSION,
        "time": time.time(),
        "connected": engine._can is not None,
        "interface": getattr(engine._can, "_interface", None) if engine._can is not None else None,
        "gpio_backend": getattr(io, "backend", None),
        "heartbeat_interval_s": engine._hb_interval_s,
        "session": session,
        "nodes": nodes,
        "nodes_online": sum(1 for n in nodes if n["online"]),
        "bnc": {
            "inputs": [
                bnc_input_dict("IN1", engine._bnc_in1_cfg, _level(io, "IN1")),
                bnc_input_dict("IN2", engine._bnc_in2_cfg, _level(io, "IN2")),
            ],
            "out": bnc_out_dict(engine._bnc_out_cfg),
        },
        "notice": None if notice is None else {"level": notice[0], "text": notice[1], "seq": engine.notice_seq},
        "notify": _notify_dict(engine._notifier),
        "log": {
            "dir": engine._exp_log_dir,
            "last_seq": log.last_seq if log is not None else 0,
            "csv_path": str(log.csv_path) if log is not None and log.csv_path else None,
            "write_error": str(log.write_error) if log is not None and log.write_error else None,
        },
    }
