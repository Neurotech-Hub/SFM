"""
engine.py — the SFM base-station engine, with no GUI in it.

Everything that has to keep running while an experiment runs lives here:
draining the CAN bus, classifying and logging frames, discovery, heartbeat
policy, pellet accounting, BNC input and output, and hosting the experiment
runner. ``SFMApp`` (app.py) is one front-end on top of it. A front-end drives
the engine by calling ``tick()`` regularly and by calling the public methods
below; it observes the engine by overriding the ``_refresh_*`` and ``_ui_*``
hooks, which do nothing here.

Threading: the engine is not thread-safe. Its modules share state without
locks and rely on being called from one thread. A front-end that runs on
another thread must hand its requests to the engine thread.
"""

from __future__ import annotations

import argparse
import queue
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


from . import storage
from .can_manager import CanManager
from .discovery_manager import DiscoveryManager
from .io_manager import BNCInputConfig, BNCOutputConfig, IOManager
from .log_manager import LogEntry, LogManager, sanitize_session_name
from .mac_id_registry import DEFAULT_REGISTRY_PATH, MacIdRegistry
from .dev_settings import DevSettings
from .node_registry import NodeRegistry, offline_timeout_for_heartbeat
from .experiment import ExperimentController, load_experiment_defs
from .experiment.schema import DEFAULT_EXPERIMENTS_DIR, ExperimentDef
from .pellet_ledger import NodeTally, PelletLedger
from .protocol import (
    CanCmd,
    CanEvent,
    DispenseState,
    classify_frame,
    format_mac,
    fault_user_message,
    node_id_from_event_id,
    node_id_from_hb_id,
    parse_event,
    parse_event_context,
    parse_fault_code,
    parse_input_changed,
    parse_heartbeat,
    parse_discovery,
    parse_presence_cal,
    parse_config_applied,
    parse_pellet_reload,
    parse_firmware_info,
    PELLET_RELOAD_MAX,
    config_applied_factor,
    build_setconfig_heartbeat,
    build_sync_flash,
    DEFAULT_SYNC_FLASH_MS,
    CAN_EVENT_DISPLAY_NAME,
    CAN_CMD_PURPOSE,
    behavioral_input_log_name,
    normalize_event_match_key,
    CONFIG_HEARTBEAT_INTERVAL,
    CONFIG_PRESENCE_FACTOR,
    CONFIG_TYPE_NAME,
    CAN_ID_ANNOUNCE,
    CAN_ID_ASSIGN,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STALE_CHECK_INTERVAL = 1.0  # seconds between staleness sweeps
UI_PAINT_INTERVAL = 0.5     # status line and pellet counters, not every frame
DEFAULT_HEARTBEAT_INTERVAL_S = 60  # default node heartbeat interval (seconds)
MAC_PING_RETRY_S = 3.0  # min seconds between MAC-resolution Pings to the same node
DEFAULT_EXPERIMENT_NAME = "free_feeding"  # matches experiments/free_feeding.json "name"
SYNC_FLASH_MS = DEFAULT_SYNC_FLASH_MS  # camera-sync status LED hold + BNC OUT pulse duration

# Commands that must never be sent to a node whose plate is already occupied
# — a real pellet is never delivered twice, system-wide, whether the command
# comes from a running experiment (see ExperimentControl.dispense()) or a
# manually-clicked GUI button.
_PELLET_GATED_CMDS = {CanCmd.Dispense, CanCmd.DispenseNoFeed}

# Short purpose strings for COMMAND rows in the event log.
COMMAND_PURPOSE = CAN_CMD_PURPOSE

BNC_IN_ACTIONS = [
    "(none)",
    "dispense_all",
    "recover_all",
    "ping_all",
    "reqstatus_all",
    "start_experiment",
    "stop_experiment",
]


# ---------------------------------------------------------------------------
# Base-station-side dispense scheduler
# ---------------------------------------------------------------------------
# "SetConfig" on a node tile configures a base-station-driven dispense
# schedule for that node — either a fixed interval, or "chained" to fire a
# set delay after another node dispenses. This does NOT touch the node's
# NVS/firmware config; it purely drives CanCmd.Dispense from the GUI.

@dataclass
class ScheduleConfig:
    mode: str = "off"                  # "off", "interval", "chained"
    interval_minutes: float = 10.0
    chained_node_id: int = 1
    chained_delay_minutes: float = 5.0
    next_fire_time: Optional[float] = None   # absolute time.time(), "interval" mode
    armed_fire_time: Optional[float] = None  # absolute time.time(), "chained" mode

    @property
    def summary(self) -> str:
        if self.mode == "interval":
            return f"Every {self.interval_minutes:g} min"
        if self.mode == "chained":
            return f"{self.chained_delay_minutes:g} min after Node {self.chained_node_id}"
        return "Off"

    @property
    def due_time(self) -> Optional[float]:
        """Absolute deadline for the next dispense, if one is pending."""
        if self.mode == "interval":
            return self.next_fire_time
        if self.mode == "chained":
            return self.armed_fire_time
        return None

    def countdown_str(self, now: Optional[float] = None) -> str:
        """
        Human-readable remaining time until the next dispense.

        Returns "" when Off, "waiting" when chained but not yet armed,
        otherwise "Xm YYs" (or "Xh Xm YYs" for longer intervals).
        """
        if self.mode == "off":
            return ""
        due = self.due_time
        if due is None:
            return "waiting" if self.mode == "chained" else ""
        remaining = max(0.0, due - (now if now is not None else time.time()))
        # Ceil so the display only hits 0m 00s when the schedule is truly due.
        total_s = int(remaining + 0.999) if remaining > 0 else 0
        hours, rem = divmod(total_s, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds:02d}s"
        return f"{minutes}m {seconds:02d}s"

    def display_line(self, now: Optional[float] = None) -> str:
        """Full schedule label for the node tile, including live countdown."""
        if self.mode == "off":
            return "Schedule: Off"
        countdown = self.countdown_str(now)
        if not countdown:
            return f"Schedule: {self.summary}"
        return f"Schedule: {self.summary} · {countdown}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BaseStationEngine:
    """CAN, logging, discovery, BNC and experiment hosting, without a GUI."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._can: Optional[CanManager] = None
        self._registry: Optional[NodeRegistry] = None
        self._discovery: Optional[DiscoveryManager] = None
        self._log: Optional[LogManager] = None
        self._mac_registry = MacIdRegistry(DEFAULT_REGISTRY_PATH)
        self._last_stale_check = 0.0

        # Base station owns the pellet numbers that reach the log and the
        # report; node counters run from power-on and are read only as a
        # dropped-frame witness. Reset on every open_session().
        self._pellets = PelletLedger()
        # What the Experiment panel and node tiles show: the ledger read live
        # for the run's nodes while it runs, then frozen when it ends (the
        # ledger keeps counting manual dispenses between runs).
        self._pellet_run_nodes: List[int] = []
        self._pellet_snapshot: Optional[Dict[int, NodeTally]] = None

        # Developer Menu values (presence factor) persisted across restarts —
        # see dev_settings.py.
        self._dev_settings = DevSettings()

        # IOManager owns all base-station GPIO except CAN (BNC I/O).
        # Created up front (not tied to CAN session) since it degrades to a
        # harmless no-op when no GPIO hardware is present.
        self._io = IOManager()

        # BNC configuration — deliberately free-form placeholders; see
        # io_manager.BNCInputConfig / BNCOutputConfig docstrings.
        self._bnc_in1_cfg = BNCInputConfig(label="BNC IN 0")
        self._bnc_in2_cfg = BNCInputConfig(label="BNC IN 1")
        self._bnc_out_cfg = BNCOutputConfig(label="BNC OUT")
        self._bnc_edge_queue: "queue.Queue[tuple[str, float]]" = queue.Queue(maxsize=256)

        self._last_panel_paint = 0.0
        self._last_display_generation = -1
        self._hb_interval_s = float(DEFAULT_HEARTBEAT_INTERVAL_S)
        # Nodes that have already received (or been covered by) a SetConfig
        # HeartbeatInterval push this session. Cleared on Re-discover so a
        # rebooted node at firmware's 5s default gets reconfigured.
        self._hb_configured_nodes: set = set()
        self._hb_last_push_ts: float = 0.0
        # True after SetConfig was pushed because every expected node slot
        # has been recognized (discovery MAC present). Cleared on Re-discover.
        self._hb_roster_configured: bool = False

        # Throttle for MAC-resolution Pings (node_id -> last-sent time.time()).
        self._mac_ping_sent: Dict[int, float] = {}

        # Base-station-side dispense scheduler (per node_id)
        self._schedules: Dict[int, ScheduleConfig] = {}

        # Experiment templates (JSON schema → Python templates)
        self._exp_defs = load_experiment_defs(DEFAULT_EXPERIMENTS_DIR)
        self._exp = ExperimentController()
        self._exp_log_dir = str(Path("~/sfm_logs").expanduser())

    # ------------------------------------------------------------------
    # Front-end hooks. A front-end overrides these to show what changed.
    # The engine never depends on them doing anything.
    # ------------------------------------------------------------------

    def _refresh_tile(self, node_id: int) -> None:
        """A node's state changed."""

    def _refresh_all_tiles(self) -> None:
        """Every node's state may have changed."""

    def _refresh_schedule_text(self, node_id: int, now: Optional[float] = None) -> None:
        """A node's dispense schedule changed."""

    def _refresh_bnc_dots(self) -> None:
        """Called every tick, after BNC edges were handled."""

    def _refresh_experiment_status(self) -> None:
        """Called a few times a second, and when an experiment starts or stops."""

    def _refresh_pellet_counters(self) -> None:
        """Called a few times a second, and just before an experiment stops."""

    def _refresh_log_table(self, *_) -> None:
        """New non-heartbeat rows reached the log."""

    def _read_hb_interval_input_s(self) -> int:
        """Heartbeat interval wanted by the front-end, in seconds."""
        return max(1, int(self._hb_interval_s))

    def _ui_bnc_edge(self, which: str, edge: str, ts: float) -> None:
        """A BNC input edge was handled."""

    def _ui_bnc_out_pulsed(self) -> None:
        """BNC OUT was just pulsed."""

    def _on_experiment_start(self, *_args) -> None:
        """A BNC input asked to start an experiment. The front-end decides which."""

    def _on_experiment_stop(self, *_args) -> None:
        """A BNC input asked to stop an experiment."""

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(
        self,
        interface: str,
        bitrate: int,
        num_nodes: int,
        log_dir: str,
        auto_save: bool,
    ) -> str:
        """
        Open CAN, GPIO, the node registry, the log and discovery.

        Returns "" on success, otherwise a message for the operator. Nothing is
        left half-open before the CAN step succeeds.
        """
        interface = interface.strip()
        if not interface:
            return "Interface name cannot be empty."

        # Validate log directory
        log_dir = (log_dir or "").strip()
        if not log_dir:
            log_dir = storage.default_log_dir()
        is_valid, error_msg = storage.validate_log_dir(log_dir)
        if not is_valid:
            return f"Log directory issue: {error_msg}"

        self._exp_log_dir = log_dir

        # Open CAN
        try:
            self._can = CanManager(interface=interface, bitrate=bitrate)
            self._can.start()
        except Exception as exc:
            return f"CAN error: {exc}"

        # Bring up base-station GPIO (BNC I/O, button, AEO). Degrades to a
        # no-op automatically when no GPIO hardware is present.
        self._io.begin()
        self._io.on_bnc_in1_edge(lambda: self._bnc_edge_queue.put(("IN1", time.time())))
        self._io.on_bnc_in2_edge(lambda: self._bnc_edge_queue.put(("IN2", time.time())))

        # Create subsystems
        self._registry = NodeRegistry(num_nodes)
        self._registry.set_offline_timeout(
            offline_timeout_for_heartbeat(self._hb_interval_s)
        )
        self._hb_configured_nodes.clear()
        self._hb_roster_configured = False
        self._log = LogManager(log_dir=log_dir, auto_save=auto_save)
        # Reload persistent MAC↔ID map in case it was edited on disk
        self._mac_registry.load()
        self._discovery = DiscoveryManager(self._can, self._io, self._mac_registry)
        self._discovery.on_node_discovered(self._on_node_discovered)
        self._discovery.on_complete(self._on_discovery_complete)

        self._discovery.start(start_id=1)
        if self._log and len(self._mac_registry) > 0:
            self._log.add(LogEntry(
                timestamp=time.time(),
                direction="SYS",
                node_id=0,
                frame_type="REGISTRY",
                event_name="Loaded",
                raw_id=0,
                raw_data=b"",
                details=(
                    f"{len(self._mac_registry)} MAC↔ID mapping(s) from "
                    f"{self._mac_registry.path}"
                ),
            ))
        return ""

    # ------------------------------------------------------------------
    # The engine step
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """
        One engine step. Call it as often as the front-end can, at least every
        few tens of milliseconds.

        Order matters and is the order the GUI has always used: drain CAN,
        drain BNC, step the experiment, tick discovery, sweep for stale nodes,
        run the dispense schedulers, then tell the front-end the log changed.
        """
        if self._can is None:
            return

        # 1. Drain RX queue
        messages = self._can.poll_rx()
        for msg in messages:
            self._dispatch_rx(msg)

        # 1b. Drain BNC edge queue (populated by IOManager's GPIO callback threads)
        bnc_events = []
        while True:
            try:
                bnc_events.append(self._bnc_edge_queue.get_nowait())
            except queue.Empty:
                break
        for which, ts in bnc_events:
            self._handle_bnc_edge(which, ts)
        self._refresh_bnc_dots()

        # 1c. Host experiment runner with the same drained CAN frames
        now = time.time()
        if self._exp.is_running:
            self._exp.step(messages, now=now)
            if self._log and self._exp.runner is not None:
                self._log.set_context(trial=self._exp.runner.ctx.trial)
        # Switch back to the daily CSV on the same frame a run ends.
        # Widget repaints stay on UI_PAINT_INTERVAL.
        if not self._exp.is_running:
            self._resume_daily_log()
        if now - self._last_panel_paint >= UI_PAINT_INTERVAL:
            self._last_panel_paint = now
            self._refresh_experiment_status()
            self._refresh_pellet_counters()

        # 2. Tick discovery timeout
        if self._discovery:
            self._discovery.tick()

        # 3. Staleness check (once per second)
        if now - self._last_stale_check >= STALE_CHECK_INTERVAL:
            self._last_stale_check = now
            if self._registry:
                self._registry.check_staleness()
            self._refresh_all_tiles()

        # 3b. Base-station dispense scheduler (interval + chained modes)
        self._tick_schedulers(now)

        # 4. Repaint the event strip only when a non-heartbeat row arrived
        #    or the operator changed a filter (that path calls refresh directly).
        if self._log is not None and self._log.display_generation != self._last_display_generation:
            self._last_display_generation = self._log.display_generation
            self._refresh_log_table()

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------

    def start_experiment(
        self,
        exp_def: ExperimentDef,
        session_name: str,
        collect: Callable[[], Tuple[Dict[str, Any], List[int]]],
    ) -> Tuple[bool, str]:
        """
        Start ``exp_def`` as a named session.

        ``collect`` returns ``(params, nodes)``. It is called only after the
        session name has passed, so a bad name never reads the form.

        Returns ``(started, message)``. ``message`` is for the operator and is
        empty when there is nothing to say; ``(False, "")`` means the request
        was ignored (no CAN, or already running) or the experiment declined to
        start and has said why itself.
        """
        if exp_def is None or self._can is None or self._registry is None:
            return False, ""
        if self._exp.is_running:
            return False, ""

        sanitized = sanitize_session_name(session_name)
        if not sanitized:
            if self._log:
                self._log.add(LogEntry(
                    timestamp=time.time(), direction="LOCAL", node_id=0, frame_type="ERROR",
                    event_name="ExperimentStartRejected", raw_id=0, raw_data=b"",
                    details="Session name is required to start an experiment.",
                ))
            return False, "Enter a session name before starting."

        params, nodes = collect()
        if not nodes:
            return False, "Enable at least one node to run."

        run_id = 0
        if self._log:
            run_id = self._log.open_session(sanitized, self._exp_log_dir)
            self._log.set_context(session=sanitized, run_id=run_id, trial=0, session_start_ts=time.time())

        # Continue the pellet numbers when this session name already has a
        # log. Nodes keep counting from power-on; the baseline is what makes
        # the next pellet one past the last run instead of pellet 1 again.
        resume = self._log.resume_state if self._log is not None else None
        baseline = None
        if resume is not None and resume.from_run:
            nodes_seen = set(resume.node_pellets) | set(resume.node_taken)
            baseline = {
                n: (resume.node_pellets.get(n, 0), resume.node_taken.get(n, 0))
                for n in nodes_seen
            }
        self._pellets.reset(baseline)

        # Re-push GUI heartbeat interval so experiment runs never inherit the
        # firmware 5s default, and align experiment offline detection to 3× HB.
        self._sync_heartbeat_policy(broadcast=True)
        def _firmware_for(node_id: int) -> Optional[str]:
            node = self._registry.get(node_id) if self._registry is not None else None
            if node is None:
                return None
            return node.firmware_version

        ok = self._exp.start(
            exp_def,
            params=params,
            nodes=nodes,
            can=self._can,
            io=self._io,
            log=self._log,
            on_session_start=self._fire_sync_marker,
            online_timeout_s=offline_timeout_for_heartbeat(self._hb_interval_s),
            resume=resume if resume is not None and resume.from_run else None,
            firmware_for=_firmware_for,
        )
        if ok:
            # One ledger for the run: the experiment callbacks see the same
            # session numbers the log rows carry, not a second tally that could
            # drift from it.
            runner = self._exp.runner if self._exp is not None else None
            if runner is not None:
                runner.normalizer.set_pellet_ledger(self._pellets)
            self._pellet_run_nodes = list(nodes)
            return True, ""

        # Named CSV was opened above; without a running experiment, send
        # further traffic back to the daily activity log.
        self._resume_daily_log()
        return False, ""

    def stop_experiment(self) -> None:
        """Stop the running experiment (if any) and return to the daily log."""
        # Last live read before the runner goes away; frozen from here on.
        self._refresh_pellet_counters()
        self._pellet_run_nodes = []
        if self._exp.is_running:
            self._exp.stop()
        self._resume_daily_log()

    # ------------------------------------------------------------------
    # BNC
    # ------------------------------------------------------------------

    def _handle_bnc_edge(self, which: str, ts: float) -> None:
        """Handle a BNC IN1/IN2 edge event drained from the IOManager callback queue."""
        cfg = self._bnc_in1_cfg if which == "IN1" else self._bnc_in2_cfg

        # Classify the edge by reading the current (de-inverted) input level.
        high = True
        try:
            high = self._io.read_bnc_in1() if which == "IN1" else self._io.read_bnc_in2()
        except Exception:  # noqa: BLE001 — no GPIO hardware / read error
            pass
        edge = "rising" if high else "falling"
        action = cfg.rising_action if high else cfg.falling_action

        self._ui_bnc_edge(which, edge, ts)

        if self._log:
            self._log.add(LogEntry(
                timestamp=ts, direction="RX", node_id=0, frame_type="BNC",
                event_name=f"{which} {edge}", raw_id=0, raw_data=b"",
                details=f"label={cfg.label or '—'} action={action or '(none)'}",
            ))

        # Always forward to a running experiment so @exp.on_bnc_in works.
        if self._exp.is_running:
            self._exp.forward_bnc(which, ts=ts, high=high)

        if cfg.enabled and action:
            self._dispatch_bnc_action(action)

    def _maybe_fire_bnc_out(self, event_name: str) -> None:
        """
        Called for every incoming CAN EVENT frame. If BNC OUT is enabled and
        its (free-form) trigger matches this event, fire a pulse.
        """
        cfg = self._bnc_out_cfg
        if not cfg.enabled or not cfg.trigger:
            return
        trigger = normalize_event_match_key(cfg.trigger)
        event_key = normalize_event_match_key(event_name)
        matched = trigger in ("any", "anyevent", "all") or trigger == event_key
        if not matched:
            return

        self._io.pulse_bnc_out(cfg.pulse_width_us)
        self._ui_bnc_out_pulsed()

        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="TX", node_id=0, frame_type="BNC",
                event_name="BNC OUT pulse", raw_id=0, raw_data=b"",
                details=f"trigger={cfg.trigger} width={cfg.pulse_width_us // 1000}ms matched={event_name}",
            ))

    def manual_bnc_pulse(self) -> None:
        """Fire BNC OUT once for bench testing, regardless of Enabled."""
        width = self._bnc_out_cfg.pulse_width_us
        self._io.pulse_bnc_out(width)
        self._ui_bnc_out_pulsed()
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="TX", node_id=0, frame_type="BNC",
                event_name="BNC OUT manual pulse", raw_id=0, raw_data=b"",
                details=f"width={width // 1000}ms",
            ))

    # ------------------------------------------------------------------
    # Node identity
    # ------------------------------------------------------------------

    def assign_node_id(self, node_id: int, new_id: int) -> None:
        """
        Assign / reassign a node ID via discovery ASSIGN (0x081).

        Only works when the node's MAC is known from a previous ANNOUNCE or
        REJOIN — that MAC-addressed frame ensures only the targeted node
        accepts the assignment. Does NOT broadcast.
        """
        if not (1 <= new_id <= 254):
            return

        node = self._registry.get(node_id) if self._registry else None
        mac = node.mac if node else None

        if not mac or mac == bytes(6):
            if self._log:
                self._log.add(LogEntry(
                    timestamp=time.time(),
                    direction="TX",
                    node_id=node_id,
                    frame_type="ERROR",
                    event_name="AssignId",
                    raw_id=0,
                    raw_data=b"",
                    details="MAC unknown — run discovery first",
                ))
            return

        ok = self._can.send_assign(mac, new_id)
        # Persist the intentional remapping on the base station immediately
        self._mac_registry.set(mac, new_id)
        if self._registry and node is not None:
            # If the MAC is moving between tile slots, keep UI MAC consistent
            if new_id != node_id:
                node.mac = None
                node.discovery_state = "Pending"
                dest = self._registry.get(new_id)
                if dest is not None:
                    dest.mac = mac
                    dest.discovery_state = "Enabled"
                self._refresh_tile(node_id)
                self._refresh_tile(new_id)
            else:
                node.mac = mac
                node.discovery_state = "Enabled"
                self._refresh_tile(node_id)

        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(),
                direction="TX",
                node_id=new_id,
                frame_type="DISCOVERY",
                event_name="ASSIGN",
                raw_id=CAN_ID_ASSIGN,
                raw_data=mac + bytes([new_id]),
                details=f"MAC={format_mac(mac)} → id={new_id}"
                        + ("" if ok else " (send failed)"),
            ))

    # ------------------------------------------------------------------
    # Frame dispatch, commands, discovery, heartbeat policy, pellets,
    # schedulers. Moved here from app.py without changes.
    # ------------------------------------------------------------------

    @staticmethod
    def _is_can_interface_up(interface: str) -> bool:
        """Best-effort check via `ip link show <interface>` — never raises."""
        if not interface:
            return False
        try:
            result = subprocess.run(
                ["ip", "link", "show", interface],
                capture_output=True, text=True, timeout=1.0,
            )
            return result.returncode == 0 and "UP" in result.stdout
        except Exception:
            return False

    def _registry_node_ids(self) -> List[int]:
        num_nodes = self._registry.num_nodes() if self._registry else 0
        return list(range(1, num_nodes + 1))

    def _default_experiment_def(self) -> Optional[ExperimentDef]:
        """
        The template the GUI opens on. Looked up explicitly by name —
        load_experiment_defs() sorts by filename, so index 0 is whatever
        sorts first alphabetically, not necessarily the intended default.
        """
        if not self._exp_defs:
            return None
        for d in self._exp_defs:
            if d.name == DEFAULT_EXPERIMENT_NAME:
                return d
        return self._exp_defs[0]

    def _resume_daily_log(self) -> None:
        """Close the experiment CSV (if open) and append to today's daily file."""
        if self._log is None or not self._log.is_named_session:
            return
        self._log.resume_daily(self._exp_log_dir)

    def _fire_sync_marker(self) -> None:
        """
        Called once at true experiment-session activation (ExperimentControl
        .on_session_start): hold every node's status LED solid for
        SYNC_FLASH_MS and fire a coincident BNC OUT pulse, so field-camera
        footage can be aligned to session start. Nodes latched in Fault
        silently ignore the CAN command (firmware guard) and are called out
        by id in the log row.
        """
        ts = time.time()
        if self._can is not None:
            self._can.send_broadcast(CanCmd.SyncFlash, build_sync_flash(SYNC_FLASH_MS))
        if self._io is not None:
            self._io.pulse_bnc_out(SYNC_FLASH_MS * 1000)

        faulted = []
        if self._registry is not None:
            faulted = [
                n.node_id for n in self._registry.all_nodes()
                if n.dispense_state == DispenseState.Fault
            ]
        details = f"{SYNC_FLASH_MS}ms LED + BNC OUT"
        if self._log:
            details += f" · run {self._log.run_id}"
        if faulted:
            details += f" · skipped (fault): {faulted}"

        if self._log:
            self._log.add(LogEntry(
                timestamp=ts, direction="TX", node_id=0, frame_type="SYNC",
                event_name="SyncFlash (broadcast)", raw_id=0x100, raw_data=b"",
                details=details,
            ))

    def _experiment_pellet_tallies(self) -> Optional[Dict[int, NodeTally]]:
        """
        Per-node pellet totals for the current (or last) experiment run.

        Read live from the ledger while the run is going, and once more on
        the frame it ends so the final pellet is counted; frozen after that.
        A session resumed under the same name starts from the ledger's
        baseline, so the numbers carry on from the previous run. None
        before the first run.
        """
        if self._pellet_run_nodes:
            self._pellet_snapshot = {
                n: self._pellets.tally(n) for n in self._pellet_run_nodes
            }
            if not self._exp.is_running:
                self._pellet_run_nodes = []
        return self._pellet_snapshot

    def _dispatch_rx(self, msg) -> None:
        arb_id = msg.arbitration_id
        data   = bytes(msg.data)
        ftype  = classify_frame(arb_id)

        entry_name = ""
        details    = ""
        skip_log   = False
        log_fields: Dict[str, Any] = {}

        if ftype == "HEARTBEAT":
            node_id = node_id_from_hb_id(arb_id)
            if node_id and self._registry:
                hb = parse_heartbeat(data)
                if hb:
                    self._maybe_request_mac_via_ping(node_id)
                    self._registry.update_from_heartbeat(node_id, hb)
                    self._ensure_node_heartbeat_config(node_id)
                    self._refresh_tile(node_id)
                    details = (
                        f"state={hb.dispense_state.name} "
                        f"mouse_presence={int(hb.mouse_presence)} "
                        f"sensors=pellet:{int(hb.pellet)},"
                        f"load_position:{int(hb.load_position)},"
                        f"dome_open:{int(hb.dome_open)} "
                        f"fault={hb.fault_code.name}"
                    )
                    if hb.fault_code.value != 0:
                        details += f" ({fault_user_message(hb.fault_code)})"

                    # Heartbeats increment nothing, so any advance they reveal
                    # is a frame that never arrived. This is the recovery path
                    # for a node that was offline or on a noisy bus.
                    recs = self._pellets.witness_heartbeat(
                        node_id, hb.pellets_presented, hb.pellets_taken
                    )
                    tally = self._pellets.tally(node_id)
                    details += (
                        f" session_pellets={tally.presented}"
                        f" session_taken={tally.taken}"
                    )
                    log_fields.update({
                        "session_pellets": tally.presented,
                        "session_taken": tally.taken,
                        "node_pellets": hb.pellets_presented,
                        "node_taken": hb.pellets_taken,
                    })
                    for name, rec in recs.items():
                        if rec.had_gap or rec.restarted:
                            self._log_pellet_gap(node_id, f"Heartbeat/{name}", rec)

        elif ftype == "EVENT":
            node_id = node_id_from_event_id(arb_id)
            if node_id and self._registry:
                ev = parse_event(data)
                if ev:
                    if ev.event == CanEvent.Pong and len(ev.raw_extra) >= 6:
                        mac = bytes(ev.raw_extra[:6])
                        details = f"MAC={format_mac(mac)}"
                        self._handle_pong_mac(node_id, mac)
                    else:
                        self._maybe_request_mac_via_ping(node_id)
                    if ev.event == CanEvent.InputChanged:
                        changed = parse_input_changed(ev)
                        if changed:
                            # Read dispense state BEFORE applying the input so a
                            # dome rising edge while Loaded can defer to DomeOpened.
                            node = self._registry.get(node_id)
                            prior_state = node.dispense_state if node is not None else None
                            self._registry.update_from_input(
                                node_id, changed.input_id, changed.active
                            )
                            entry_name = behavioral_input_log_name(
                                changed.input_id, changed.active, prior_state
                            )
                            if entry_name is None:
                                skip_log = True
                                entry_name = ""
                            else:
                                details = (
                                    f"input={changed.input_id.name} "
                                    f"active={int(changed.active)}"
                                )
                        else:
                            entry_name = "Invalid Input Event"
                            details = "InputChanged payload must contain input ID and state"
                    else:
                        fault_code = parse_fault_code(ev) if ev.event == CanEvent.Fault else None
                        self._registry.update_from_event(node_id, ev.event, fault_code=fault_code)
                        entry_name = CAN_EVENT_DISPLAY_NAME.get(ev.event, ev.event.name)
                        if ev.event == CanEvent.Fault:
                            code = fault_code.name if fault_code is not None else "unknown"
                            details = f"fault={code} — {fault_user_message(fault_code)}"
                            entry_name = f"Fault: {code}"
                        elif ev.event == CanEvent.DomeOpenWarning:
                            details = "dome sensor open >30s"
                        elif ev.event == CanEvent.PelletReload:
                            info = parse_pellet_reload(ev)
                            if info is None:
                                details = "malformed PelletReload payload"
                            else:
                                details = (
                                    f"warning reason={info.reason_name} "
                                    f"attempt {info.attempt}/{PELLET_RELOAD_MAX}"
                                )
                                ctx = {"pellet_count": info.pellet_count}
                                rec = self._pellets.witness_event(
                                    node_id, ev.event, info.pellet_count
                                )
                                details += " " + self._pellet_details(
                                    node_id, ev.event, ctx, rec
                                )
                                log_fields.update(
                                    self._pellet_fields(node_id, ev.event, ctx, rec)
                                )
                        elif ev.event == CanEvent.FirmwareInfo:
                            version = parse_firmware_info(ev)
                            if version is None:
                                details = "malformed FirmwareInfo payload"
                            else:
                                details = f"version={version}"
                                node = self._registry.get(node_id)
                                if node is not None:
                                    node.firmware_version = version
                        elif ev.event == CanEvent.PresenceCalResult:
                            cal = parse_presence_cal(ev)
                            if cal is None:
                                details = "malformed PresenceCalResult payload"
                            else:
                                details = (
                                    f"ok={int(cal.ok)} threshold={cal.threshold} "
                                    f"samples={cal.samples}"
                                )
                                node = self._registry.get(node_id)
                                if not cal.ok:
                                    details += (
                                        " — FAILED (too few samples captured; "
                                        "retry with an empty cage)"
                                    )
                                elif (
                                    node is not None
                                    and node.presence_threshold is not None
                                    and node.presence_threshold == cal.threshold
                                ):
                                    details += " — note: threshold unchanged from previous calibration"
                                if node is not None:
                                    node.presence_threshold = cal.threshold
                                    node.presence_cal_ok = cal.ok
                        elif ev.event == CanEvent.ConfigApplied:
                            applied = parse_config_applied(ev)
                            if applied is None:
                                details = "malformed ConfigApplied payload"
                            else:
                                config_name = CONFIG_TYPE_NAME.get(
                                    applied.config_type, f"0x{applied.config_type:02X}"
                                )
                                if applied.config_type == CONFIG_PRESENCE_FACTOR:
                                    value_str = f"{config_applied_factor(applied):g}"
                                    node = self._registry.get(node_id)
                                    if node is not None and applied.ok:
                                        node.presence_factor = config_applied_factor(applied)
                                else:
                                    value_str = str(applied.raw_value)
                                details = f"config={config_name} ok={int(applied.ok)} value={value_str}"
                                if not applied.ok:
                                    details += " — REJECTED"
                        else:
                            ctx = parse_event_context(ev)
                            if ctx is not None:
                                # The node's number is folded into the session
                                # ledger and never shown as "the" pellet count —
                                # it runs from node power-on, which is what made
                                # the old logs unreadable mid-session.
                                rec = self._pellets.witness_event(
                                    node_id, ev.event, ctx["pellet_count"]
                                )
                                details = self._pellet_details(node_id, ev.event, ctx, rec)
                                log_fields.update(
                                    self._pellet_fields(node_id, ev.event, ctx, rec)
                                )
                                if "pellet_present" in ctx:
                                    details += f" pellet_present={int(ctx['pellet_present'])}"
                                    log_fields["pellet_present"] = bool(ctx["pellet_present"])
                                if "dome_open" in ctx:
                                    details += f" dome_open={int(ctx['dome_open'])}"
                                    log_fields["dome_open"] = bool(ctx["dome_open"])
                                if rec is not None and (rec.had_gap or rec.restarted):
                                    self._log_pellet_gap(node_id, entry_name, rec)
                        # Distinguish a real delivery from a no-feed (mimic)
                        # cycle for later analysis. Keep entry_name from
                        # CAN_EVENT_DISPLAY_NAME — BNC OUT matching normalizes
                        # spaces/underscores so "Dome Opened" and legacy
                        # "DomeOpened" both match.
                        if ev.event == CanEvent.NoFeedPresented:
                            details += " delivery=EMPTY — empty plate raised, no pellet delivered"
                        elif ev.event == CanEvent.Dwelling:
                            details += " delivery=EMPTY — holding at drop position, M1 idle"
                        elif ev.event == CanEvent.Loaded:
                            details += " delivery=PELLET"
                    self._refresh_tile(node_id)
                    if not skip_log:
                        self._maybe_fire_bnc_out(entry_name)
                    if ev.event in (CanEvent.Loaded, CanEvent.NoFeedPresented):
                        self._arm_chained_schedules(node_id)

        elif ftype == "DISCOVERY":
            node_id = 0
            info = parse_discovery(arb_id, data)
            if info:
                entry_name = {
                    0x080: "ANNOUNCE",
                    0x081: "ASSIGN",
                    0x082: "ACK",
                    0x083: "REJOIN",
                }.get(arb_id, "Unknown Discovery")
                if info["mac"]:
                    details = f"MAC={format_mac(info['mac'])}"
                if info["node_id"] is not None:
                    details += f" id={info['node_id']}"
                # Route to discovery manager
                if self._discovery:
                    handled = self._discovery.handle_frame(arb_id, data)
                    # Log the ASSIGN we just sent in response to ANNOUNCE
                    if handled and arb_id == CAN_ID_ANNOUNCE and self._log:
                        pending_mac = self._discovery.pending_mac
                        pending_id = self._discovery.pending_id
                        if pending_mac is not None and pending_id is not None:
                            self._log.add(LogEntry(
                                timestamp=time.time(),
                                direction="TX",
                                node_id=pending_id,
                                frame_type="DISCOVERY",
                                event_name="ASSIGN",
                                raw_id=CAN_ID_ASSIGN,
                                raw_data=pending_mac + bytes([pending_id]),
                                details=f"MAC={format_mac(pending_mac)} id={pending_id}",
                            ))
            node_id = 0

        else:
            node_id = 0

        if self._log and not skip_log:
            self._log.add(LogEntry(
                timestamp=time.time(),
                direction="RX",
                node_id=node_id or 0,
                frame_type=ftype,
                event_name=entry_name,
                raw_id=arb_id,
                raw_data=data,
                details=details,
                fields=log_fields,
            ))

    def _pellet_details(self, node_id: int, event, ctx: dict, rec) -> str:
        """
        Human-readable pellet context for an event row.

        The leading number is always the base station's session count. The
        node's own counter is written to fields_json for diagnostics but kept
        out of the details string — reading it as the pellet number is exactly
        the confusion this replaced.
        """
        tally = self._pellets.tally(node_id)
        if event == CanEvent.PelletTaken:
            return f"session_taken={tally.taken}"
        if rec is None:
            return f"node_count={ctx['pellet_count']}"
        return f"session_pellets={tally.presented}"

    def _pellet_fields(self, node_id: int, event, ctx: dict, rec) -> Dict[str, Any]:
        """Structured pellet accounting for the row's fields_json."""
        tally = self._pellets.tally(node_id)
        fields: Dict[str, Any] = {
            "session_pellets": tally.presented,
            "session_taken": tally.taken,
        }
        # Node counter, labelled for what it is: a power-on lifetime count kept
        # only so a dropped frame can be detected later.
        if event == CanEvent.PelletTaken:
            fields["node_taken"] = ctx["pellet_count"]
        else:
            fields["node_pellets"] = ctx["pellet_count"]
        if rec is not None and rec.missed:
            fields["missed_frames"] = rec.missed
        if rec is not None and rec.restarted:
            fields["node_counter_restarted"] = True
        return fields

    def _log_pellet_gap(self, node_id: int, context: str, rec) -> None:
        """
        Record a dropped-frame gap or a node counter restart as its own row.

        A gap has already been corrected in the session total by the time this
        runs — the point of the row is that the correction is visible during
        analysis instead of silently smoothing over lost data.
        """
        if not self._log:
            return
        if rec.restarted:
            name = "Pellet Counter Restart"
            details = (
                f"node counter went backwards during {context} — node rebooted; "
                f"re-baselined, session total unchanged at {rec.total}"
            )
        else:
            name = "Pellet Count Gap"
            details = (
                f"node counter jumped by {rec.delta} across {context} — "
                f"{rec.missed} event frame(s) never arrived; session total "
                f"corrected to {rec.total}"
            )
        self._log.add(LogEntry(
            timestamp=time.time(),
            direction="SYS",
            node_id=node_id,
            frame_type="PELLET_AUDIT",
            event_name=name,
            raw_id=0,
            raw_data=b"",
            details=details,
            source="SYS",
            fields={
                "node": node_id,
                "missed_frames": rec.missed,
                "delta": rec.delta,
                "session_total": rec.total,
                "node_counter_restarted": rec.restarted,
            },
        ))

    def _maybe_request_mac_via_ping(self, node_id: int) -> None:
        """
        If a node's MAC is still unknown in this session, send it a Ping.

        A node only sends ANNOUNCE/REJOIN (which carry its MAC) once at
        boot. If the base-station GUI is restarted while nodes are already
        running, those discovery frames are never seen again — only
        heartbeats/events arrive, which carry no MAC on their own. Rather
        than trusting stale data from the persistent MAC↔ID registry file,
        the firmware echoes the node's live MAC in its Pong reply to Ping
        (see _handle_pong_mac), so the tile only ever shows a MAC that was
        just confirmed by the actual node. Retries are throttled so this
        doesn't flood the bus while waiting for a reply.

        Suppressed only while a Re-discover pass is actively reassigning IDs:
        Re-discover clears identities and Ping/Pong must not rewrite the
        MAC↔ID map mid-handshake. Ordinary startup discovery (no Re-discover)
        must NOT block this — its Running phase can persist up to
        DISCOVERY_IDLE_TIMEOUT_S after the last frame, which would otherwise
        delay MAC resolution for every already-running node on every launch.
        """
        if not self._registry or not self._can:
            return
        if self._discovery and self._discovery.is_reassigning:
            return
        node = self._registry.get(node_id)
        if node is None or node.mac is not None:
            return
        now = time.time()
        if now - self._mac_ping_sent.get(node_id, 0.0) < MAC_PING_RETRY_S:
            return
        self._mac_ping_sent[node_id] = now
        self._send_cmd(node_id, CanCmd.Ping)

    def _handle_pong_mac(self, node_id: int, mac: bytes) -> None:
        """
        A Pong arrived carrying the node's live MAC (in reply to any Ping,
        not just a MAC-resolution one). Register it if new/changed, and
        refresh the persistent MAC↔ID registry so future discovery/REJOIN
        alignment stays accurate too. The RX log entry for this frame
        (added by the caller) already records the confirmed MAC.
        Mark node as online (primary indicator of connectivity).
        """
        if not self._registry:
            return
        # During an active Re-discover reassignment, ANNOUNCE/ACK/REJOIN own
        # identity — ignore in-flight Pongs that would restore wiped IDs.
        # (is_reassigning, not is_running — see _maybe_request_mac_via_ping.)
        if self._discovery and self._discovery.is_reassigning:
            return
        node = self._registry.get(node_id)
        if node is None:
            return
        self._mac_ping_sent.pop(node_id, None)
        node.online = True
        if node.mac == mac:
            return
        self._registry.register_node(node_id, mac, source="PING")
        if self._mac_registry is not None:
            self._mac_registry.set(mac, node_id)
        self._refresh_tile(node_id)
        self._maybe_apply_heartbeat_when_roster_complete()

    def _on_rediscover(self, sender=None, app_data=None, user_data=None) -> None:
        """
        Re-discover: wipe the persistent MAC↔ID dictionary, clear live node
        identity state, broadcast ClearId so all nodes wipe their saved NVS
        ID and re-enter WaitAEI, then pulse AEO to trigger fresh ANNOUNCE
        from every node (IDs reassigned from 1).
        """
        self._mac_ping_sent.clear()
        self._hb_configured_nodes.clear()
        self._hb_roster_configured = False
        if self._registry:
            self._registry.clear_identities()
            self._refresh_all_tiles()
        if self._discovery:
            self._discovery.reset()
            time.sleep(0.25)
        elif self._mac_registry:
            # No discovery manager (should not happen on main screen) —
            # still wipe the persistent map.
            self._mac_registry.clear()
        if self._log:
            path = self._mac_registry.path if self._mac_registry else "?"
            self._log.add(LogEntry(
                timestamp=time.time(),
                direction="SYS",
                node_id=0,
                frame_type="REGISTRY",
                event_name="Cleared",
                raw_id=0,
                raw_data=b"",
                details=f"MAC↔ID registry cleared + ClearId broadcast ({path})",
            ))

    def _on_node_discovered(self, node) -> None:
        if self._registry:
            self._registry.register_node(node.node_id, node.mac, source=node.source)
            if self._mac_registry:
                saved_label = self._mac_registry.get_label(node.mac)
                if saved_label:
                    self._registry.set_label(node.node_id, saved_label)
        self._refresh_tile(node.node_id)
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(),
                direction="RX",
                node_id=node.node_id,
                frame_type="DISCOVERY",
                event_name=node.source,
                raw_id=0,
                raw_data=node.mac,
                details=f"MAC={format_mac(node.mac)} id={node.node_id}",
            ))
        # As soon as every expected slot has a MAC, push the GUI heartbeat
        # interval — do not wait for the 30s discovery idle timeout.
        self._maybe_apply_heartbeat_when_roster_complete()

    def _on_discovery_complete(self) -> None:
        """Idle-timeout fallback: push interval even if roster stayed incomplete."""
        self._sync_heartbeat_policy(broadcast=True)

    def _expected_nodes_recognized(self) -> bool:
        """True when every setup slot (1..N) has a MAC from discovery/Ping."""
        if self._registry is None:
            return False
        expected = self._registry.num_nodes()
        if expected <= 0:
            return False
        for node_id in range(1, expected + 1):
            node = self._registry.get(node_id)
            if node is None or node.mac is None:
                return False
        return True

    def _maybe_apply_heartbeat_when_roster_complete(self) -> None:
        """Push SetConfig once when the full expected node roster is recognized."""
        if self._hb_roster_configured:
            return
        if not self._expected_nodes_recognized():
            return
        self._hb_roster_configured = True
        self._sync_heartbeat_policy(broadcast=True)

    def _sync_heartbeat_policy(
        self,
        seconds: Optional[int] = None,
        *,
        broadcast: bool = True,
    ) -> None:
        """
        Adopt ``seconds`` (or the GUI spinbox) as the live heartbeat interval,
        scale registry / experiment offline timeouts to 3× that value, and
        optionally broadcast SetConfig so every node matches.
        """
        if seconds is None:
            seconds = self._read_hb_interval_input_s()
        else:
            seconds = max(1, int(seconds))
        self._hb_interval_s = float(seconds)
        timeout_s = offline_timeout_for_heartbeat(self._hb_interval_s)
        if self._registry is not None:
            self._registry.set_offline_timeout(timeout_s)
        runner = self._exp.runner if self._exp is not None else None
        if runner is not None:
            runner.normalizer.set_online_timeout(timeout_s)
        if broadcast and self._can is not None:
            payload = build_setconfig_heartbeat(seconds * 1000)
            self._broadcast(CanCmd.SetConfig, payload)
            self._hb_last_push_ts = time.time()

    def _ensure_node_heartbeat_config(self, node_id: int) -> None:
        """
        Late-join / post-reboot nodes boot at the firmware 5s default.
        First heartbeat from an unconfigured node triggers a SetConfig push.
        Debounced so a burst of first-seen nodes shares one broadcast.
        """
        if node_id in self._hb_configured_nodes:
            return
        self._hb_configured_nodes.add(node_id)
        last = getattr(self, "_hb_last_push_ts", 0.0)
        if time.time() - last < 1.0:
            return
        self._sync_heartbeat_policy(broadcast=True)

    def _command_details(self, cmd: CanCmd, payload: bytes = b"", ok: bool = True) -> str:
        """Short human-readable purpose for COMMAND log rows."""
        purpose = COMMAND_PURPOSE.get(cmd, cmd.name)
        if cmd == CanCmd.SetConfig and len(payload) >= 3 and payload[0] == CONFIG_HEARTBEAT_INTERVAL:
            ms = payload[1] | (payload[2] << 8)
            purpose = f"heartbeat={ms / 1000:g}s"
        elif cmd == CanCmd.SetConfig and len(payload) >= 5 and payload[0] == CONFIG_PRESENCE_FACTOR:
            factor = struct.unpack("<f", payload[1:5])[0]
            purpose = f"presence_factor={factor:g}"
        if not ok:
            return f"{purpose} — send failed"
        return purpose

    def _on_recover_node(self, node_id: int) -> None:
        """
        Recover a faulted node: clears the fault and readies it for the next
        action. During an experiment this also re-arms the node's program
        (via the template's on_recover); otherwise it sends a plain Recover.
        """
        if self._exp.is_running and self._exp.recover_node(node_id):
            if self._registry:
                self._registry.clear_fault(node_id)
                self._refresh_tile(node_id)
            return
        self._send_cmd(node_id, CanCmd.Recover)

    def _send_cmd(self, node_id: int, cmd: CanCmd, payload: bytes = b"") -> None:
        if not self._can:
            return
        if cmd in _PELLET_GATED_CMDS and self._pellet_gate_blocks(node_id, cmd):
            return
        ok = self._can.send_command(node_id, cmd, payload)
        if cmd == CanCmd.Recover and self._registry:
            self._registry.clear_fault(node_id)
            self._refresh_tile(node_id)
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(),
                direction="TX",
                node_id=node_id,
                frame_type="COMMAND",
                event_name=cmd.name,
                raw_id=0x100 + node_id,
                raw_data=bytes([cmd.value]) + payload,
                details=self._command_details(cmd, payload, ok),
            ))

    def _pellet_gate_blocks(self, node_id: int, cmd: CanCmd) -> bool:
        """
        True (and logs a WARNING row) if ``cmd`` must be withheld from
        ``node_id`` because its plate already has a pellet on it — the
        manual-GUI counterpart to ExperimentControl.dispense()'s veto.
        Silently allows the command through when node/registry state isn't
        known (e.g. no session started) rather than blocking on missing data.
        """
        node = self._registry.get(node_id) if self._registry else None
        if node is None or not node.pellet:
            return False
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="LOCAL", node_id=node_id, frame_type="WARNING",
                event_name=f"{cmd.name} skipped (pellet present)", raw_id=0, raw_data=b"",
                details="Command withheld — pellet already on plate.",
            ))
        return True

    def _broadcast(self, cmd: CanCmd, payload: bytes = b"") -> None:
        if not self._can:
            return
        if cmd in _PELLET_GATED_CMDS and self._registry:
            occupied = [n.node_id for n in self._registry.all_nodes() if n.pellet]
            if occupied:
                clear_nodes = [n.node_id for n in self._registry.all_nodes() if not n.pellet]
                if self._log:
                    self._log.add(LogEntry(
                        timestamp=time.time(), direction="TX", node_id=0, frame_type="WARNING",
                        event_name=f"{cmd.name} (broadcast) partially skipped", raw_id=0, raw_data=b"",
                        details=(
                            f"pellet already on plate — withheld for {occupied}; "
                            f"sent individually to {clear_nodes}"
                        ),
                    ))
                for node_id in clear_nodes:
                    self._send_cmd(node_id, cmd, payload)
                return
        ok = self._can.send_broadcast(cmd, payload)
        if cmd == CanCmd.Recover and self._registry:
            for node in self._registry.all_nodes():
                self._registry.clear_fault(node.node_id)
            self._refresh_all_tiles()
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(),
                direction="TX",
                node_id=0,
                frame_type="COMMAND",
                event_name=f"{cmd.name} (broadcast)",
                raw_id=0x100,
                raw_data=bytes([cmd.value]) + payload,
                details=self._command_details(cmd, payload, ok),
            ))

    def _arm_chained_schedules(self, source_node_id: int) -> None:
        """
        Called when `source_node_id` fires a Loaded event. Any node
        whose schedule is chained to this node gets its one-shot dispense
        timer (re-)armed for `chained_delay_minutes` from now.
        """
        now = time.time()
        for node_id, cfg in self._schedules.items():
            if cfg.mode == "chained" and cfg.chained_node_id == source_node_id:
                cfg.armed_fire_time = now + cfg.chained_delay_minutes * 60.0
                self._refresh_schedule_text(node_id, now)

    def _tick_schedulers(self, now: float) -> None:
        """
        Fire Dispense commands for nodes whose schedule is due, and refresh
        the live countdown label on every frame so the tile shows a ticking
        "Xm YYs" clock until the next dispense.
        """
        for node_id, cfg in self._schedules.items():
            if cfg.mode == "interval" and cfg.next_fire_time is not None:
                if now >= cfg.next_fire_time:
                    self._send_cmd(node_id, CanCmd.Dispense)
                    cfg.next_fire_time = now + cfg.interval_minutes * 60.0
            elif cfg.mode == "chained" and cfg.armed_fire_time is not None:
                if now >= cfg.armed_fire_time:
                    self._send_cmd(node_id, CanCmd.Dispense)
                    cfg.armed_fire_time = None
            if cfg.mode != "off":
                self._refresh_schedule_text(node_id, now)

    def _dispatch_bnc_action(self, action: str) -> None:
        """Dispatch curated BNC IN actions (commands + experiment control)."""
        keyword = action.strip().lower().replace("-", "_")
        if keyword in ("", "(none)"):
            return
        if keyword == "dispense_all":
            self._broadcast(CanCmd.Dispense)
        elif keyword == "recover_all":
            self._broadcast(CanCmd.Recover)
        elif keyword == "ping_all":
            self._broadcast(CanCmd.Ping)
        elif keyword == "reqstatus_all":
            self._broadcast(CanCmd.ReqStatus)
        elif keyword == "start_experiment":
            self._on_experiment_start()
        elif keyword == "stop_experiment":
            self._on_experiment_stop()

    def _on_label_change(self, node_id: int, new_label: str) -> None:
        if self._registry:
            self._registry.set_label(node_id, new_label)
            node = self._registry.get(node_id)
            if node and node.mac and self._mac_registry:
                self._mac_registry.set_label(node.mac, new_label)

    def _shutdown(self) -> None:
        if self._exp.is_running:
            self._exp.stop()
        if self._can:
            self._can.stop()
        if self._log:
            self._log.close()
        if self._discovery:
            self._discovery.stop()
        self._io.shutdown()
