"""
commands.py — everything an operator can ask the base station to do.

Each command is ``fn(engine, **args)`` and runs on the engine thread (see
``host.EngineHost``). It returns JSON-safe data, or raises ``CommandError``
with a message meant for the operator. ``run_command`` looks one up by name,
checks its arguments, and wraps the outcome as ``{"ok": ..., ...}`` so a
gateway can send it straight back.

Nothing here knows about DearPyGui, HTTP or sockets.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from . import storage
from .experiment_form import FormError, collect, describe
from .gui_settings import MAX_NODES
from .log_manager import sanitize_session_name
from .node_registry import HEARTBEAT_MAX_S, HEARTBEAT_MIN_S
from .state import log_row_dict
from .protocol import (
    PRESENCE_FACTOR_MAX,
    PRESENCE_FACTOR_MIN,
    CanCmd,
    build_setconfig_presence_factor,
)

NodeRef = Union[int, str]  # a node number, or "all"


class CommandError(Exception):
    """The command can't be done. The message is for the operator."""


def _need_bus(engine: Any) -> None:
    if engine._can is None or engine._registry is None:
        raise CommandError("Connect to the bus first.")


def _node_id(engine: Any, node: Any) -> int:
    _need_bus(engine)
    try:
        node_id = int(node)
    except (TypeError, ValueError):
        raise CommandError(f"'{node}' is not a node number.") from None
    if engine._registry.get(node_id) is None:
        raise CommandError(f"Node {node_id} is not in this setup.")
    return node_id


def _node_ids(engine: Any) -> List[int]:
    return [n.node_id for n in engine._registry.all_nodes()] if engine._registry else []


def _experiment_def(engine: Any, name: str) -> Any:
    for exp_def in engine._exp_defs:
        if exp_def.name == name:
            return exp_def
    raise CommandError(f"Unknown experiment: {name}.")


# ---------------------------------------------------------------------------
# Before connecting: what is there, and what the form starts at
# ---------------------------------------------------------------------------

NET_SYSFS = Path("/sys/class/net")
ARPHRD_CAN = 280  # the "type" a SocketCAN interface reports
DEFAULT_BITRATE = 250_000


def _can_bitrate(name: str) -> Optional[int]:
    """The bitrate the kernel has set on a hardware CAN interface, or None if it can't be read."""
    try:
        out = subprocess.run(
            ["ip", "-j", "-d", "link", "show", name], capture_output=True, text=True, timeout=1.5
        )
        if out.returncode != 0:
            return None
        bitrate = json.loads(out.stdout)[0]["linkinfo"]["info_data"]["bittiming"]["bitrate"]
        return int(bitrate)
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return None


def list_interfaces(engine: Any) -> Dict[str, Any]:
    """The CAN interfaces on this machine, with whether each is up and its bitrate."""
    found: List[Dict[str, Any]] = []
    try:
        entries = sorted(NET_SYSFS.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        try:
            if int((entry / "type").read_text().strip()) != ARPHRD_CAN:
                continue
        except (OSError, ValueError):
            continue
        virtual = "/virtual/" in os.path.realpath(entry)
        found.append({
            "name": entry.name,
            "virtual": virtual,
            "up": bool(engine._is_can_interface_up(entry.name)),
            "bitrate": None if virtual else _can_bitrate(entry.name),
        })
    return {"interfaces": found}


def get_defaults(engine: Any) -> Dict[str, Any]:
    """Fixed choices and limits the forms are built from."""
    from .engine import BNC_IN_ACTIONS
    from .io_manager import BNC_IN_MODES, BNC_OUT_MAPPINGS, MAX_PULSE_US
    return {
        "log_dir": storage.default_log_dir(),
        "bitrate": DEFAULT_BITRATE,
        "max_nodes": MAX_NODES,
        "bnc": {
            "input_modes": list(BNC_IN_MODES),
            "input_actions": list(BNC_IN_ACTIONS),
            "out_mappings": list(BNC_OUT_MAPPINGS),
            "max_pulse_us": MAX_PULSE_US,
        },
        "heartbeat_range_s": [HEARTBEAT_MIN_S, HEARTBEAT_MAX_S],
        "presence_factor_range": [PRESENCE_FACTOR_MIN, PRESENCE_FACTOR_MAX],
    }


def get_gui_settings(engine: Any) -> Dict[str, Any]:
    """The Connect-step values remembered from last time."""
    return {"settings": engine.gui_settings().all()}


def set_gui_settings(engine: Any, **values: Any) -> Dict[str, Any]:
    """Remember Connect-step values (interface, num_nodes, log_dir, auto_save, last_experiment)."""
    try:
        engine.gui_settings().update(values)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    except OSError as exc:
        raise CommandError(f"The settings could not be saved: {exc}") from None
    return {"settings": engine.gui_settings().all()}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def check_interface(engine: Any, interface: str) -> Dict[str, Any]:
    """Is the SocketCAN interface there and up?"""
    interface = (interface or "").strip()
    return {"interface": interface, "up": bool(engine._is_can_interface_up(interface))}


def connect(
    engine: Any,
    interface: str,
    bitrate: int = 250_000,
    num_nodes: int = 2,
    log_dir: str = "",
    auto_save: bool = True,
) -> Dict[str, Any]:
    """Open the bus, the log and discovery. Does nothing if already connected."""
    if engine._can is not None:
        raise CommandError("Already connected. Use Change connection to connect again.")
    if not 1 <= int(num_nodes) <= 12:
        raise CommandError("Number of nodes must be between 1 and 12.")
    error = engine.connect(interface, int(bitrate), int(num_nodes), log_dir, bool(auto_save))
    if error:
        raise CommandError(error)
    # After the connect, so a saved value that can't be used is reported in the log.
    engine.enable_bnc_persistence()
    engine.enable_node_layout()
    # Probe the bus now: every node already running answers with a Pong that
    # carries its live MAC, so the map shows MACs and online status at once
    # instead of after the next heartbeat. The DearPyGui app does the same.
    engine._broadcast(CanCmd.Ping)
    return {"interface": interface.strip(), "num_nodes": int(num_nodes)}


def disconnect(engine: Any) -> Dict[str, Any]:
    """Close the bus and the log so the Connect step can be changed. The session must be stopped."""
    error = engine.disconnect()
    if error:
        raise CommandError(error)
    return {}


def rediscover(engine: Any) -> Dict[str, Any]:
    """Wipe the MAC-to-ID map and discover again. Node IDs are reassigned from 1."""
    _need_bus(engine)
    if engine._exp.is_running:
        raise CommandError("Stop the session before re-discovering nodes.")
    engine._on_rediscover()
    return {}


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

def list_experiments(engine: Any) -> Dict[str, Any]:
    node_ids = _node_ids(engine) or list(range(1, 3))
    return {"experiments": [describe(d, node_ids) for d in engine._exp_defs]}


def preview_session(engine: Any, session_name: str) -> Dict[str, Any]:
    """Where a session of this name will be written, and whether it resumes an earlier one."""
    if engine._log is None:
        raise CommandError("Connect to the bus first.")
    info = engine._log.preview_session_path(session_name, engine._exp_log_dir)
    path = info.get("path")
    return {**info, "path": str(path) if path is not None else None,
            "name": sanitize_session_name(session_name)}


def _collector(engine: Any, exp_def: Any, values: Optional[Dict[str, Any]]) -> Callable[[], Any]:
    node_ids = _node_ids(engine)

    def read() -> Any:
        try:
            return collect(exp_def, values, node_ids)
        except FormError as exc:
            raise CommandError(str(exc)) from None
    return read


def _remember_setup(engine: Any, experiment: str, session_name: str, values: Optional[Dict[str, Any]]) -> None:
    """Keep what a session was set up with, so a page opened mid-session can show it."""
    engine.session_setup = {
        "experiment": experiment,
        "session_name": sanitize_session_name(session_name),
        "values": dict(values or {}),
    }


def start_session(
    engine: Any,
    experiment: str,
    session_name: str,
    values: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _need_bus(engine)
    exp_def = _experiment_def(engine, experiment)
    if engine._exp.is_running:
        raise CommandError("A session is already running.")
    collector = _collector(engine, exp_def, values)
    started, message = engine.start_experiment(exp_def, session_name, collector)
    if not started:
        raise CommandError(message or "The session did not start. See the event log for the reason.")
    _remember_setup(engine, experiment, session_name, values)
    return {"name": sanitize_session_name(session_name), "experiment": experiment}


def stop_session(engine: Any) -> Dict[str, Any]:
    engine.stop_experiment()
    return {}


def arm_start(
    engine: Any,
    experiment: str,
    session_name: str,
    values: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    exp_def = _experiment_def(engine, experiment)
    error = engine.arm_start(exp_def, session_name, _collector(engine, exp_def, values))
    if error:
        raise CommandError(error)
    _remember_setup(engine, experiment, session_name, values)
    return {}


def disarm_start(engine: Any) -> Dict[str, Any]:
    engine.disarm_start()
    return {}


# ---------------------------------------------------------------------------
# Node actions
# ---------------------------------------------------------------------------

def dispense(engine: Any, node: NodeRef) -> Dict[str, Any]:
    return _node_action(engine, node, CanCmd.Dispense)


def ping(engine: Any, node: NodeRef) -> Dict[str, Any]:
    return _node_action(engine, node, CanCmd.Ping)


def recover(engine: Any, node: NodeRef) -> Dict[str, Any]:
    _need_bus(engine)
    if node == "all":
        engine._broadcast(CanCmd.Recover)
    else:
        engine._on_recover_node(_node_id(engine, node))
    return {}


def _node_action(engine: Any, node: NodeRef, cmd: CanCmd) -> Dict[str, Any]:
    _need_bus(engine)
    if node == "all":
        engine._broadcast(cmd)
    else:
        engine._send_cmd(_node_id(engine, node), cmd)
    return {}


def set_label(engine: Any, node: int, label: str) -> Dict[str, Any]:
    node_id = _node_id(engine, node)
    engine._on_label_change(node_id, str(label)[:32])
    return {}


def assign_node_id(engine: Any, node: int, new_id: int) -> Dict[str, Any]:
    node_id = _node_id(engine, node)
    try:
        new_id = int(new_id)
    except (TypeError, ValueError):
        raise CommandError("The new ID must be a number from 1 to 254.") from None
    if not 1 <= new_id <= 254:
        raise CommandError("The new ID must be from 1 to 254.")
    if engine._exp.is_running:
        raise CommandError("Stop the session before assigning a node ID.")
    target = engine._registry.get(node_id)
    if target is None or not target.mac or target.mac == bytes(6):
        raise CommandError("This node's MAC is not known yet. Re-discover first.")
    engine.assign_node_id(node_id, new_id)
    return {}


def calibrate_presence(engine: Any, cage_empty: bool = False) -> Dict[str, Any]:
    """Recalibrate every node's presence pad. Needs the operator to confirm the cages are empty."""
    _need_bus(engine)
    if engine._exp.is_running:
        raise CommandError("Calibration is not available while a session runs.")
    if not cage_empty:
        raise CommandError("Confirm that every cage is empty before calibrating.")
    engine._broadcast(CanCmd.CalibratePresence)
    return {}


# ---------------------------------------------------------------------------
# Node positions on the map
# ---------------------------------------------------------------------------

def get_layout(engine: Any) -> Dict[str, Any]:
    """Every remembered position, by MAC."""
    return {"positions": engine._layout.all() if engine._layout is not None else {}}


def set_node_position(engine: Any, node: int, x: float, y: float) -> Dict[str, Any]:
    """
    Remember where a node was dropped (fractions of the map, 0 to 1). A node
    whose MAC isn't known yet can't be saved, so it is reported rather than
    kept under its ID, which Re-discover can reassign.
    """
    node_id = _node_id(engine, node)
    if engine._layout is None:
        raise CommandError("The node layout is not available.")
    target = engine._registry.get(node_id)
    if target is None or not target.mac or target.mac == bytes(6):
        return {"saved": False, "reason": "This node's MAC is not known yet."}
    try:
        engine._layout.set(target.mac, x, y)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    except OSError as exc:
        raise CommandError(f"The position could not be saved: {exc}") from None
    return {"saved": True}


def clear_layout(engine: Any) -> Dict[str, Any]:
    """Forget every position; the map auto-arranges again."""
    if engine._layout is not None:
        try:
            engine._layout.clear()
        except OSError as exc:
            raise CommandError(f"The layout could not be cleared: {exc}") from None
    return {}


# ---------------------------------------------------------------------------
# Developer settings
# ---------------------------------------------------------------------------

def set_heartbeat(engine: Any, seconds: float) -> Dict[str, Any]:
    """Tell every node how often to send a heartbeat (SetConfig 0x05, sub-type 0x01)."""
    _need_bus(engine)
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        raise CommandError("The heartbeat interval must be a number of seconds.") from None
    if not HEARTBEAT_MIN_S <= seconds <= HEARTBEAT_MAX_S:
        raise CommandError(
            f"The heartbeat interval must be from {HEARTBEAT_MIN_S:g} s to {HEARTBEAT_MAX_S:g} s."
        )
    engine._sync_heartbeat_policy(seconds, broadcast=True)
    return {"heartbeat_interval_s": engine._hb_interval_s}


def set_presence_factor(engine: Any, factor: float) -> Dict[str, Any]:
    _need_bus(engine)
    try:
        factor = float(factor)
    except (TypeError, ValueError):
        raise CommandError("The presence factor must be a number.") from None
    if not PRESENCE_FACTOR_MIN <= factor <= PRESENCE_FACTOR_MAX:
        raise CommandError(
            f"The presence factor must be from {PRESENCE_FACTOR_MIN:g} to {PRESENCE_FACTOR_MAX:g}."
        )
    engine._broadcast(CanCmd.SetConfig, build_setconfig_presence_factor(factor))
    engine._dev_settings.set_presence_factor(factor)
    return {"presence_factor": factor}


def get_dev_settings(engine: Any) -> Dict[str, Any]:
    return {
        "presence_factor": engine._dev_settings.presence_factor,
        "presence_factor_range": [PRESENCE_FACTOR_MIN, PRESENCE_FACTOR_MAX],
        "heartbeat_interval_s": engine._hb_interval_s,
        "heartbeat_range_s": [HEARTBEAT_MIN_S, HEARTBEAT_MAX_S],
    }


# ---------------------------------------------------------------------------
# BNC
# ---------------------------------------------------------------------------

_BNC_INPUT_FIELDS = (
    "label", "enabled", "mode", "action", "release_action",
    "rising_action", "falling_action", "active_high", "min_width_ms",
)


def set_bnc_input(engine: Any, which: str, **fields: Any) -> Dict[str, Any]:
    unknown = sorted(set(fields) - set(_BNC_INPUT_FIELDS))
    if unknown:
        raise CommandError(f"Unknown BNC input setting: {', '.join(unknown)}.")
    error = engine.set_bnc_input(which, **fields)
    if error:
        raise CommandError(error)
    return {}


def set_bnc_out(
    engine: Any,
    mapping: str,
    trigger: Optional[str] = None,
    width_us: Optional[int] = None,
    sync_width_us: Optional[int] = None,
    interval_s: Optional[float] = None,
) -> Dict[str, Any]:
    error = engine.set_bnc_out_mapping(
        mapping, trigger=trigger, width_us=width_us,
        sync_width_us=sync_width_us, interval_s=interval_s,
    )
    if error:
        raise CommandError(error)
    return {}


def bnc_out_triggers(engine: Any) -> Dict[str, Any]:
    return {"triggers": engine.bnc_out_trigger_names()}


def bnc_pulse(engine: Any) -> Dict[str, Any]:
    """Fire BNC OUT once by hand. Not while a session runs."""
    if engine._exp.is_running:
        raise CommandError("A manual pulse is not available while a session runs.")
    engine.manual_bnc_pulse()
    return {}


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

MAX_LOG_ROWS_PER_REQUEST = 1000


def log_rows(
    engine: Any,
    after: int = 0,
    limit: int = 200,
    node: Optional[int] = None,
    frame_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Log rows numbered above ``after``, oldest first. ``last_seq`` is the newest row's number."""
    if engine._log is None:
        return {"rows": [], "last_seq": 0}
    try:
        after, limit = int(after), max(1, min(int(limit), MAX_LOG_ROWS_PER_REQUEST))
        node = None if node in (None, "", "all") else int(node)
    except (TypeError, ValueError):
        raise CommandError("after, limit and node must be numbers.") from None
    rows = engine._log.rows_after(after, limit, node, frame_type or None)
    return {"rows": [log_row_dict(e) for e in rows], "last_seq": engine._log.last_seq}


def heartbeat_rows(engine: Any) -> Dict[str, Any]:
    """The last few heartbeat rows. Heartbeats go to their own CSV and are not in the log window."""
    if engine._log is None:
        return {"rows": []}
    return {"rows": [log_row_dict(e) for e in engine._log.recent_heartbeats()]}


def log_clear(engine: Any) -> Dict[str, Any]:
    """Clear the on-screen log. The CSV is not touched."""
    if engine._log is not None:
        engine._log.clear()
    return {}


def log_export(engine: Any) -> Dict[str, Any]:
    """
    Save what the log window holds to a new CSV in the log directory.

    The name has ``_export_`` in it, which the analysis loader skips so an
    export is never read as a session.
    """
    if engine._log is None:
        raise CommandError("Connect to the bus first.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(engine._exp_log_dir).expanduser() / f"sfm_export_{stamp}.csv"
    try:
        written = engine._log.export(str(path))
    except OSError as exc:
        raise CommandError(f"Could not write the export: {exc}") from None
    return {"path": str(written)}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

COMMANDS: Dict[str, Callable[..., Dict[str, Any]]] = {
    fn.__name__: fn
    for fn in (
        check_interface, connect, disconnect, rediscover,
        list_experiments, preview_session, start_session, stop_session, arm_start, disarm_start,
        dispense, ping, recover, set_label, assign_node_id, calibrate_presence,
        get_layout, set_node_position, clear_layout,
        set_heartbeat, set_presence_factor, get_dev_settings,
        set_bnc_input, set_bnc_out, bnc_out_triggers, bnc_pulse,
        log_rows, heartbeat_rows, log_clear, log_export,
        list_interfaces, get_defaults, get_gui_settings, set_gui_settings,
    )
}


def run_command(engine: Any, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Run a command by name. Always returns a dict: ``{"ok": True, ...result}`` or
    ``{"ok": False, "error": message}``. Run it on the engine thread.
    """
    fn = COMMANDS.get(name)
    if fn is None:
        return {"ok": False, "error": f"Unknown command: {name}."}
    args = dict(args or {})
    try:
        inspect.signature(fn).bind(engine, **args)
    except TypeError as exc:
        return {"ok": False, "error": f"Bad arguments for {name}: {exc}"}
    try:
        result = fn(engine, **args)
    except CommandError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result}
