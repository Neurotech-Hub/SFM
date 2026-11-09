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
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


from . import storage
from .bnc_settings import BncSettings, apply_settings
from .can_manager import CanManager
from .discovery_manager import DiscoveryManager
from .io_manager import (
    BNC_IN_MODES,
    BNC_OUT_MAPPINGS,
    MAX_PULSE_US,
    BNCInputConfig,
    BNCOutputConfig,
    BncEdgeEvent,
    IOManager,
    PulseRecord,
)
from .log_manager import LogEntry, LogManager, sanitize_session_name
from .mac_id_registry import DEFAULT_REGISTRY_PATH, MacIdRegistry
from .dev_settings import DevSettings
from .gui_settings import GuiSettings
from .node_layout import NodeLayout
from .node_registry import (
    HEARTBEAT_MAX_S,
    HEARTBEAT_MIN_S,
    NodeRegistry,
    offline_timeout_for_heartbeat,
)
from .experiment import ExperimentController, load_experiment_defs
from .experiment.schema import DEFAULT_EXPERIMENTS_DIR, ExperimentDef, build_experiment
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
ERROR_LOG_INTERVAL_S = 10.0     # same engine error is logged at most this often
ARRIVAL_STAMP_MAX_SKEW_S = 6 * 3600  # a kernel stamp further from now than this is not trusted

# What the timestamps in a session's log mean. "capture-v1": CAN rows carry the
# kernel's arrival time and BNC rows carry the edge or pulse capture time, so
# they are the times things happened, not the times the engine got to them.
# Logs from before this have no timebase field, and their rows carry the time
# each frame was processed.
TIMEBASE = "capture-v1"
CLOCK_STEP_THRESHOLD_S = 0.02   # wall-clock jump that counts as a step
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

@dataclass
class _ArmedStart:
    """A session that a BNC input is allowed to start, checked when it was armed."""
    exp_def: ExperimentDef
    session_name: str
    collect: Callable[[], Tuple[Dict[str, Any], List[int]]]


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
        # Edges arrive here from the GPIO thread as BncEdgeEvents (older callers
        # may still queue (which, ts) tuples). The edge thread never blocks on
        # this queue: when it is full the newest edge is dropped and counted.
        self._bnc_edge_queue: "queue.Queue[Any]" = queue.Queue(maxsize=256)
        self._bnc_edge_dropped = 0
        self._bnc_drops_reported = 0
        # How each BNC OUT pulse went, reported by the pulse thread and logged
        # on the engine thread.
        self._pulse_records: "queue.Queue[PulseRecord]" = queue.Queue()
        self._io.on_bnc_edge(self._on_bnc_edge_event)
        self._io.on_bnc_pulse(self._pulse_records.put)
        # When each BNC input last went active, for pulse-width checks.
        self._bnc_active_since: Dict[str, float] = {}
        # A session a BNC input may start. Never armed on its own: not at
        # launch, not after a run, not after a restart. Armed by hand.
        self._armed: Optional[_ArmedStart] = None
        # The latest message meant for the operator: (level, text).
        self.last_notice: Optional[Tuple[str, str]] = None
        # Counts notices, so a front-end can tell a repeat of the same text from no news.
        self.notice_seq = 0
        # What the running (or last) session was set up with; see commands._remember_setup.
        self.session_setup: Optional[Dict[str, Any]] = None

        # Wall minus monotonic clock. Both move together while the clock is
        # slewed; a jump means the wall clock was stepped (NTP, manual set).
        self._clock_offset = time.time() - time.monotonic()
        self._clock_check_enabled = True
        # Rate limit for error rows from the tick: (phase, error type) -> last logged.
        self._error_last: Dict[tuple, float] = {}
        self._error_counts: Dict[tuple, int] = {}

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
        self._exp.on_event = self._on_experiment_event
        self._exp_log_dir = str(Path("~/sfm_logs").expanduser())

        # BNC OUT on a node event is pulsed from the CAN receive thread, so the
        # pulse doesn't wait for the next tick. ``_fast_out`` is the settings it
        # reads there, replaced whole and never edited in place:
        # (normalized trigger, pulse width in us), or None when that path is off.
        self._fast_out: Optional[Tuple[str, int]] = None
        self._rx_tap_installed = False
        self._io_started = False     # the BNC/GPIO lines are claimed once and kept across a disconnect
        # Where the BNC configuration is saved. None until a front-end asks for
        # persistence with ``enable_bnc_persistence``.
        self._bnc_settings: Optional[BncSettings] = None
        # Where the operator put each node on the map, by MAC. None until a
        # front-end asks for it with ``enable_node_layout``.
        self._layout: Optional[NodeLayout] = None
        # The operator's last Connect-step choices. Loaded the first time it is asked for.
        self._gui_settings: Optional[GuiSettings] = None
        # Pulses the receive thread asked for and the pulse thread refused.
        self._pulse_refusals: "queue.Queue[Tuple[str, int, float]]" = queue.Queue()
        self._next_periodic: Optional[float] = None

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

    def _ui_notice(self, level: str, text: str) -> None:
        """Something the operator should see ("info" or "warning"). Also kept in ``last_notice``."""

    def _on_experiment_start(self, *_args) -> None:
        """
        A BNC input in the original "edge" mode asked to start an experiment.
        A front-end with its own form overrides this. Without one, the session
        armed with ``arm_start`` is started.
        """
        self._bnc_start("BNC")

    def _on_experiment_stop(self, *_args) -> None:
        """A BNC input asked to stop the experiment. A front-end overrides this to also update its display."""
        self.stop_experiment()

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

        # Let the receive thread pulse BNC OUT for a node event without
        # waiting for the next tick.
        set_tap = getattr(self._can, "set_rx_tap", None)
        if set_tap is not None:
            set_tap(self._rx_tap)
            self._rx_tap_installed = True
            self._refresh_fast_out()

        # Bring up base-station GPIO (BNC I/O, button, AEO). Degrades to a
        # no-op automatically when no GPIO hardware is present.
        if not self._io_started:
            self._io.begin()
            self._io_started = True

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

    def disconnect(self) -> str:
        """
        Close the CAN bus, the log and discovery so Connect can be done again
        with another interface, node count or log folder. The BNC lines stay
        claimed. Returns "" on success, otherwise a message for the operator.
        """
        if self._can is None:
            return ""
        if self._exp.is_running:
            return "Stop the session before changing the connection."
        if self._discovery:
            self._discovery.stop()
        self._can.stop()
        if self._log:
            self._log.close()
        self._can = self._registry = self._log = self._discovery = None
        self._rx_tap_installed = False
        self._hb_configured_nodes.clear()
        self._hb_roster_configured = False
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

        # Every step below is guarded on its own. A fault in one frame, one
        # display hook or one experiment step is logged and the tick carries
        # on: the CAN queue is emptied destructively, so an exception that
        # escaped here would also throw away the rest of the batch.

        # 1. Drain RX queue
        messages = self._can.poll_rx()
        for msg in messages:
            self._guard("rx frame", self._dispatch_rx, msg)

        # 1b. Drain BNC edge queue (populated by IOManager's GPIO thread)
        while True:
            try:
                item = self._bnc_edge_queue.get_nowait()
            except queue.Empty:
                break
            self._guard("bnc edge", self._handle_queued_bnc_edge, item)
        self._guard("bnc drops", self._report_bnc_drops)
        self._guard("bnc pulses", self._log_finished_pulses)
        self._guard("bnc display", self._refresh_bnc_dots)

        # 1c. Host experiment runner with the same drained CAN frames
        now = time.time()
        if self._exp.is_running:
            self._guard("experiment step", self._step_experiment, messages, now)
        # Switch back to the daily CSV on the same frame a run ends.
        # Widget repaints stay on UI_PAINT_INTERVAL.
        if not self._exp.is_running:
            self._guard("daily log", self._resume_daily_log)
        if now - self._last_panel_paint >= UI_PAINT_INTERVAL:
            self._last_panel_paint = now
            self._guard("status display", self._refresh_experiment_status)
            self._guard("pellet display", self._refresh_pellet_counters)

        # 2. Tick discovery timeout
        if self._discovery:
            self._guard("discovery", self._discovery.tick)

        # 3. Staleness check (once per second)
        if now - self._last_stale_check >= STALE_CHECK_INTERVAL:
            self._last_stale_check = now
            if self._registry:
                self._guard("staleness", self._registry.check_staleness)
            self._guard("tile display", self._refresh_all_tiles)

        # 3b. Base-station dispense scheduler (interval + chained modes)
        self._guard("schedulers", self._tick_schedulers, now)
        self._guard("periodic out", self._tick_periodic, time.monotonic())

        # 3c. Has the wall clock been stepped?
        self._guard("clock check", self._check_clock)

        # 4. Repaint the event strip only when a non-heartbeat row arrived
        #    or the operator changed a filter (that path calls refresh directly).
        if self._log is not None and self._log.display_generation != self._last_display_generation:
            self._last_display_generation = self._log.display_generation
            self._guard("log display", self._refresh_log_table)

    def _step_experiment(self, messages, now: float) -> None:
        self._exp.step(messages, now=now)
        if self._log and self._exp.runner is not None:
            self._log.set_context(trial=self._exp.runner.ctx.trial)

    def _guard(self, phase: str, fn: Callable[..., Any], *args: Any) -> Any:
        """Run one step of the tick; a failure is logged, not raised."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 — the engine must keep running
            self._report_error(phase, exc)
            return None

    def _report_error(self, phase: str, exc: BaseException) -> None:
        """
        Log a fault from the tick as an ``EngineError`` row. The same fault in
        the same step is logged at most once every ``ERROR_LOG_INTERVAL_S``
        seconds, with a count, so a fault repeating every tick can't flood the
        log.
        """
        key = (phase, type(exc).__name__)
        self._error_counts[key] = self._error_counts.get(key, 0) + 1
        now = time.monotonic()
        if now - self._error_last.get(key, -1e9) < ERROR_LOG_INTERVAL_S:
            return
        self._error_last[key] = now
        try:
            if self._log:
                self._log.add(LogEntry(
                    timestamp=time.time(), direction="SYS", node_id=0, frame_type="ERROR",
                    event_name="EngineError", raw_id=0, raw_data=b"",
                    details=f"{phase}: {type(exc).__name__}: {exc}",
                    source="SYS",
                    fields={
                        "phase": phase,
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "occurrences": self._error_counts[key],
                        "traceback": traceback.format_exc()[-1500:],
                    },
                ))
        except Exception:  # noqa: BLE001 — never raise from the error path
            pass

    def _check_clock(self) -> None:
        """
        Log a ``ClockStep`` row when the wall clock jumps. Slewing moves the
        wall and monotonic clocks together, so only a step changes their
        difference. A step in the middle of a session would shift every later
        timestamp, and the analysis needs to know where.
        """
        if not self._clock_check_enabled:
            return
        offset = time.time() - time.monotonic()
        step = offset - self._clock_offset
        if abs(step) < CLOCK_STEP_THRESHOLD_S:
            return
        self._clock_offset = offset
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="SYS", node_id=0, frame_type="WARNING",
                event_name="ClockStep", raw_id=0, raw_data=b"",
                details=f"wall clock stepped by {step:+.3f}s",
                source="SYS", fields={"step_s": round(step, 6), "clock_offset_s": offset},
            ))

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
            session_fields=self._timebase_fields(),
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

    def _timebase_fields(self) -> Dict[str, Any]:
        """
        Extra ``session_start`` fields saying how the timestamps in this
        session's log were taken, so the analysis doesn't have to guess.
        """
        backend = getattr(self._io, "backend", None)
        return {
            "timebase": TIMEBASE,
            # Add this to a monotonic reading to get wall time (seconds).
            "clock_offset_s": round(time.time() - time.monotonic(), 6),
            "bnc_backend": backend or "none",
            "bnc_edge_capture": "kernel" if backend == "gpiod" else ("software" if backend else "none"),
        }

    def stop_experiment(self) -> None:
        """Stop the running experiment (if any) and return to the daily log."""
        # Last live read before the runner goes away; frozen from here on.
        self._refresh_pellet_counters()
        self._pellet_run_nodes = []
        self._armed = None
        if self._exp.is_running:
            self._exp.stop()
        self._resume_daily_log()

    # ------------------------------------------------------------------
    # BNC
    # ------------------------------------------------------------------

    def _on_bnc_edge_event(self, event: BncEdgeEvent) -> None:
        """GPIO thread: queue an edge for the engine. Never blocks."""
        try:
            self._bnc_edge_queue.put_nowait(event)
        except queue.Full:
            self._bnc_edge_dropped += 1

    def _report_bnc_drops(self) -> None:
        """Say so when edges were dropped because the queue was full."""
        dropped = self._bnc_edge_dropped
        if dropped == self._bnc_drops_reported:
            return
        self._bnc_drops_reported = dropped
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="SYS", node_id=0, frame_type="WARNING",
                event_name="BNC Edges Dropped", raw_id=0, raw_data=b"",
                details=f"{dropped} BNC edge(s) were lost because the engine fell behind",
                source="BNC", fields={"dropped_total": dropped},
            ))

    def _handle_queued_bnc_edge(self, item: Any) -> None:
        if isinstance(item, BncEdgeEvent):
            self._handle_bnc_event(item)
        else:
            which, ts = item   # older callers queue (which, time)
            self._handle_bnc_edge(which, ts)

    def _handle_bnc_event(self, event: BncEdgeEvent) -> None:
        """
        Handle an edge that carries its own direction and capture time.

        The direction and time come from the GPIO layer, taken when the edge
        happened. Nothing is read back from the pin here, so two edges of a
        short pulse that arrive together are still told apart.
        """
        fields: Dict[str, Any] = {
            "channel": event.channel,
            "level": int(event.rising),
            "capture": event.source,
            "t_mono": round(event.t_mono, 6),
        }
        extra = ""
        if event.seqno:
            fields["seqno"] = event.seqno
        if event.missed:
            fields["missed_edges"] = event.missed
            extra = f" missed_edges={event.missed}"
        self._process_bnc_edge(event.which, event.rising, event.t_wall, fields, extra)

    def _handle_bnc_edge(self, which: str, ts: float, high: Optional[bool] = None) -> None:
        """
        Handle an edge reported only as "IN1/IN2 at time ts".

        Without ``high`` the direction is guessed by reading the pin now, which
        is wrong for a pulse shorter than the delay in reading it. Kept for
        callers that don't have the edge's direction; the GPIO layer does and
        uses ``_handle_bnc_event``.
        """
        if high is None:
            high = True
            try:
                high = self._io.read_bnc_in1() if which == "IN1" else self._io.read_bnc_in2()
            except Exception:  # noqa: BLE001 — no GPIO hardware / read error
                pass
        self._process_bnc_edge(which, high, ts, {})

    def _process_bnc_edge(
        self, which: str, high: bool, ts: float, fields: Dict[str, Any], extra: str = ""
    ) -> None:
        cfg = self._bnc_in1_cfg if which == "IN1" else self._bnc_in2_cfg
        edge = "rising" if high else "falling"
        mode = cfg.mode if cfg.mode in BNC_IN_MODES else "edge"
        action = cfg.rising_action if high else cfg.falling_action
        what = f"action={action or '(none)'}" if mode == "edge" else f"mode={mode}"

        self._ui_bnc_edge(which, edge, ts)

        if self._log:
            self._log.add(LogEntry(
                timestamp=ts, direction="RX", node_id=0, frame_type="BNC",
                event_name=f"{which} {edge}", raw_id=0, raw_data=b"",
                details=f"label={cfg.label or '—'} {what}{extra}",
                source="BNC", fields=fields,
            ))

        # Always forward to a running experiment so @exp.on_bnc_in works. This
        # is before any start below, so the pulse that starts a session never
        # counts as the first trial pulse.
        if self._exp.is_running:
            self._exp.forward_bnc(which, ts=ts, high=high)

        if not cfg.enabled or mode == "log_only":
            return
        if mode == "edge":
            if action:
                self._dispatch_bnc_action(action)
        elif mode == "pulse":
            self._bnc_pulse_input(which, cfg, high, ts)
        else:
            self._bnc_level_input(which, cfg, high)

    # ------------------------------------------------------------------
    # BNC input modes: pulse and level
    # ------------------------------------------------------------------

    def _bnc_pulse_input(self, which: str, cfg: BNCInputConfig, high: bool, ts: float) -> None:
        """
        Pulse mode: the signal going active starts the clock and going inactive
        ends it. A pulse at least ``min_width_ms`` wide runs ``cfg.action``; a
        narrower one is logged as a rejected glitch. Widths come from the edge
        capture times, so a 10 ms pulse is measured as 10 ms however late the
        engine reads it. The action runs when the pulse ends.
        """
        if high == cfg.active_high:
            self._bnc_active_since[which] = ts
            return
        started = self._bnc_active_since.pop(which, None)
        if started is None:
            return      # went inactive without our seeing it go active (e.g. at power-up)
        width_ms = (ts - started) * 1000.0
        accepted = width_ms >= float(cfg.min_width_ms)
        if self._log:
            self._log.add(LogEntry(
                timestamp=started, direction="RX", node_id=0, frame_type="BNC",
                event_name=f"{which} pulse" if accepted else f"{which} pulse rejected",
                raw_id=0, raw_data=b"",
                details=f"width={width_ms:.3f}ms min={float(cfg.min_width_ms):g}ms",
                source="BNC",
                fields={
                    "width_ms": round(width_ms, 3),
                    "min_width_ms": float(cfg.min_width_ms),
                    "accepted": accepted,
                    "active_high": cfg.active_high,
                },
            ))
        if accepted:
            self._run_bnc_action(cfg.action, which)

    def _bnc_level_input(self, which: str, cfg: BNCInputConfig, high: bool) -> None:
        """Level mode: going active runs ``cfg.action``, going inactive runs ``cfg.release_action``."""
        if high == cfg.active_high:
            self._run_bnc_action(cfg.action, which)
        else:
            self._run_bnc_action(cfg.release_action, which)

    def _run_bnc_action(self, action: str, which: str) -> None:
        """Run the action of a pulse or level input. Starting a session needs it to be armed."""
        keyword = (action or "").strip().lower().replace("-", "_")
        if keyword in ("", "(none)"):
            return
        if keyword == "start_experiment":
            self._bnc_start(which)
        elif keyword == "stop_experiment":
            if self._exp.is_running:
                self._on_experiment_stop()
        else:
            self._dispatch_bnc_action(action)

    # ------------------------------------------------------------------
    # Arming: a BNC input may only start a session that was armed by hand
    # ------------------------------------------------------------------

    @property
    def start_armed(self) -> bool:
        """True while a session is armed for a BNC input to start."""
        return self._armed is not None

    def arm_start(
        self,
        exp_def: ExperimentDef,
        session_name: str,
        collect: Callable[[], Tuple[Dict[str, Any], List[int]]],
    ) -> str:
        """
        Allow a BNC input to start this session, once.

        Everything that can be checked in advance is checked now, so a pulse
        later never starts something that was already known to be wrong:
        the bus is up, a session name is set, nodes are chosen, the experiment
        builds with them (for example the arm count), and at least one enabled
        BNC input is set to start an experiment. A level input that is already
        active is refused, since arming would start the session at once.

        Returns "" when armed, otherwise the reason it was not. The arming is
        used up by the start, and is gone after a restart or when the session
        ends. It is never re-armed automatically.
        """
        if exp_def is None or self._can is None or self._registry is None:
            return "Connect to the bus first."
        if self._exp.is_running:
            return "A session is already running."
        if not sanitize_session_name(session_name):
            return "Enter a session name before arming."
        try:
            params, nodes = collect()
        except Exception as exc:  # noqa: BLE001
            return f"Could not read the experiment setup: {exc}"
        if not nodes:
            return "Enable at least one node to run."
        try:
            build_experiment(exp_def, params=params, nodes=nodes)
        except Exception as exc:  # noqa: BLE001
            return str(exc) or "The experiment could not be built with these settings."

        starters = self._bnc_start_inputs()
        if not starters:
            return "No enabled BNC input is set to start an experiment."
        for which, cfg in starters:
            if cfg.mode == "level" and self._bnc_is_active(which, cfg):
                return f"{cfg.label or which} is already active. Release it, then arm."

        self._armed = _ArmedStart(exp_def, session_name, collect)
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="LOCAL", node_id=0, frame_type="BNC",
                event_name="BNC Armed", raw_id=0, raw_data=b"",
                details=f"session={sanitize_session_name(session_name)} nodes={list(nodes)} "
                        f"inputs={[w for w, _ in starters]}",
                source="BNC", fields={"inputs": [w for w, _ in starters], "nodes": list(nodes)},
            ))
        return ""

    def disarm_start(self) -> None:
        """Withdraw the arming without starting anything."""
        if self._armed is not None and self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="LOCAL", node_id=0, frame_type="BNC",
                event_name="BNC Disarmed", raw_id=0, raw_data=b"",
                details="start disarmed", source="BNC",
            ))
        self._armed = None

    def _bnc_start_inputs(self) -> List[Tuple[str, BNCInputConfig]]:
        """Enabled inputs that are set to start an experiment, in any mode."""
        out = []
        for which, cfg in (("IN1", self._bnc_in1_cfg), ("IN2", self._bnc_in2_cfg)):
            if not cfg.enabled or cfg.mode == "log_only":
                continue
            wanted = (cfg.rising_action, cfg.falling_action) if cfg.mode == "edge" else (cfg.action,)
            if any((a or "").strip().lower().replace("-", "_") == "start_experiment" for a in wanted):
                out.append((which, cfg))
        return out

    def _bnc_is_active(self, which: str, cfg: BNCInputConfig) -> bool:
        try:
            high = self._io.read_bnc_in1() if which == "IN1" else self._io.read_bnc_in2()
        except Exception:  # noqa: BLE001 — no GPIO hardware: nothing is held active
            return False
        return bool(high) == cfg.active_high

    def _bnc_start(self, which: str) -> None:
        """A BNC input asked to start a session. Check, then start the armed one."""
        if self._exp.is_running:
            self._bnc_reject(which, "a session is already running", level="info")
            return
        armed = self._armed
        if armed is None:
            self._bnc_reject(which, "start is not armed")
            return
        problem = self._start_blockers(armed)
        if problem:
            self._bnc_reject(which, problem)   # stays armed, so the next pulse can try again
            return
        started, message = self.start_experiment(armed.exp_def, armed.session_name, armed.collect)
        if not started:
            self._bnc_reject(which, message or "the experiment did not start")
            return
        self._armed = None
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="LOCAL", node_id=0, frame_type="BNC",
                event_name="BNC Start", raw_id=0, raw_data=b"",
                details=f"session started by {which}", source="BNC", fields={"input": which},
            ))
        self._notice("info", f"Session started by {which}.")

    def _start_blockers(self, armed: _ArmedStart) -> str:
        """Why a session can't start right now, or "" if it can."""
        problems = []
        if self._discovery is not None and self._discovery.is_reassigning:
            problems.append("Re-discover is changing node IDs")
        try:
            _params, nodes = armed.collect()
        except Exception as exc:  # noqa: BLE001
            return f"the experiment setup could not be read ({exc})"
        offline, faulted = [], []
        for node_id in nodes:
            node = self._registry.get(node_id) if self._registry is not None else None
            if node is None or not node.online:
                offline.append(node_id)
            elif node.dispense_state == DispenseState.Fault:
                faulted.append(node_id)
        if offline:
            problems.append(f"node(s) {offline} offline")
        if faulted:
            problems.append(f"node(s) {faulted} in fault")
        return "; ".join(problems)

    def _bnc_reject(self, which: str, reason: str, level: str = "warning") -> None:
        """A start was asked for and refused: say so in the log and to the operator."""
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="LOCAL", node_id=0, frame_type="WARNING",
                event_name="BNC Start Rejected", raw_id=0, raw_data=b"",
                details=f"{which}: {reason}", source="BNC",
                fields={"input": which, "reason": reason},
            ))
        self._notice(level, f"BNC start rejected ({which}): {reason}.")

    def _notice(self, level: str, text: str) -> None:
        self.notice_seq += 1
        self.last_notice = (level, text)
        self._ui_notice(level, text)

    def _maybe_fire_bnc_out(self, event_name: str) -> None:
        """
        Called for every incoming CAN EVENT frame. If BNC OUT is enabled and
        its (free-form) trigger matches this event, fire a pulse.
        """
        cfg = self._bnc_out_cfg
        if cfg.mapping == "legacy":
            if not cfg.enabled or not cfg.trigger:
                return
        elif cfg.mapping == "node_event":
            if self._fast_out is not None:
                return      # the receive thread already pulsed for this frame
            if not cfg.trigger:
                return
        else:
            return
        if not self._trigger_matches(normalize_event_match_key(cfg.trigger), event_name):
            return

        self._pulse_out(cfg.pulse_width_us, f"event:{event_name}")

    @staticmethod
    def _trigger_matches(trigger_key: str, event_name: str) -> bool:
        """Does a normalized trigger (see ``normalize_event_match_key``) match this event name?"""
        return trigger_key in ("any", "anyevent", "all") or trigger_key == normalize_event_match_key(event_name)

    # ------------------------------------------------------------------
    # BNC OUT: the receive-thread fast path
    # ------------------------------------------------------------------

    def _event_match_name(self, node_id: int, ev) -> Optional[str]:
        """
        The name a node event is logged under, which is what a BNC OUT trigger
        is matched against; None for an event that is not logged (the raw
        pellet and load edges). The same rules as ``_dispatch_rx``, which a test
        holds them to.
        """
        if ev.event == CanEvent.InputChanged:
            changed = parse_input_changed(ev)
            if not changed:
                return "Invalid Input Event"
            node = self._registry.get(node_id) if self._registry else None
            prior_state = node.dispense_state if node is not None else None
            return behavioral_input_log_name(changed.input_id, changed.active, prior_state)
        if ev.event == CanEvent.Fault:
            code = parse_fault_code(ev)
            return f"Fault: {code.name if code is not None else 'unknown'}"
        return CAN_EVENT_DISPLAY_NAME.get(ev.event, ev.event.name)

    def _rx_tap(self, msg) -> None:
        """
        CAN receive thread, before the frame is queued: pulse BNC OUT if this
        is a node event the output is set to follow.

        This runs beside the engine thread, so it only reads the settings in
        ``_fast_out`` and asks the pulse thread for a pulse. Anything it can't
        do safely from here (logging) is queued for the engine thread.
        """
        fast = self._fast_out
        if fast is None:
            return
        arb_id = msg.arbitration_id
        if classify_frame(arb_id) != "EVENT":
            return
        node_id = node_id_from_event_id(arb_id)
        if not node_id:
            return
        ev = parse_event(bytes(msg.data))
        if ev is None:
            return
        name = self._event_match_name(node_id, ev)
        if name is None or not self._trigger_matches(fast[0], name):
            return
        purpose = f"event:{name}"
        if self._io.pulse_bnc_out(fast[1], purpose=purpose) is False:
            self._pulse_refusals.put((purpose, fast[1], time.time()))

    def _refresh_fast_out(self) -> None:
        """Recompute what the receive thread reads from the current BNC OUT settings."""
        cfg = self._bnc_out_cfg
        if self._rx_tap_installed and cfg.mapping == "node_event" and cfg.trigger:
            self._fast_out = (normalize_event_match_key(cfg.trigger), int(cfg.pulse_width_us))
        else:
            self._fast_out = None

    def bnc_out_trigger_names(self) -> List[str]:
        """Event names BNC OUT can follow: "any_event", then every name a node event is logged under."""
        names = set(CAN_EVENT_DISPLAY_NAME.values())
        names.update({"Dome closed", "MousePresence Detected", "MousePresence Cleared"})
        known = {n.lower() for n in names}
        for ev in CanEvent:
            if ev.name not in ("Pong", "InputChanged") and ev.name.lower() not in known:
                names.add(ev.name)
        return ["any_event"] + sorted(names)

    def set_bnc_out_mapping(
        self,
        mapping: str,
        *,
        trigger: Optional[str] = None,
        width_us: Optional[int] = None,
        sync_width_us: Optional[int] = None,
        interval_s: Optional[float] = None,
    ) -> str:
        """
        Choose what BNC OUT signals. Returns "" when applied, otherwise why not.
        Nothing is changed unless everything is valid. It is locked while a
        session runs, so a session's pulses all mean the same thing.
        """
        cfg = self._bnc_out_cfg
        if self._exp.is_running:
            return "BNC OUT is locked while a session runs."
        if mapping not in BNC_OUT_MAPPINGS:
            return f"Unknown BNC OUT mapping: {mapping}."
        new_trigger = cfg.trigger if trigger is None else trigger
        new_width = cfg.pulse_width_us if width_us is None else int(width_us)
        new_sync = cfg.sync_width_us if sync_width_us is None else int(sync_width_us)
        new_interval = cfg.periodic_interval_s if interval_s is None else float(interval_s)
        for label, value in (("Pulse width", new_width), ("Sync pulse width", new_sync)):
            if not 1 <= value <= MAX_PULSE_US:
                return f"{label} must be between 1 µs and {MAX_PULSE_US // 1_000_000} s."
        if mapping == "node_event":
            known = {normalize_event_match_key(n) for n in self.bnc_out_trigger_names()}
            if not new_trigger or normalize_event_match_key(new_trigger) not in known:
                return "Choose a node event for BNC OUT to follow."
        if mapping == "periodic":
            if new_interval < 0.5:
                return "The periodic interval must be at least 0.5 s."
            if new_width >= new_interval * 1_000_000:
                return "The pulse must be shorter than the interval between pulses."

        cfg.mapping = mapping
        cfg.trigger = new_trigger
        cfg.pulse_width_us = new_width
        cfg.sync_width_us = new_sync
        cfg.periodic_interval_s = new_interval
        cfg.enabled = mapping != "off"
        self._next_periodic = None
        self._refresh_fast_out()
        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="LOCAL", node_id=0, frame_type="BNC",
                event_name="BNC OUT Mapping", raw_id=0, raw_data=b"",
                details=f"mapping={mapping}", source="BNC",
                fields={"mapping": mapping, "trigger": new_trigger, "pulse_width_us": new_width,
                        "sync_width_us": new_sync, "periodic_interval_s": new_interval},
            ))
        self._save_bnc_settings()
        return ""

    def set_bnc_input(
        self,
        which: str,
        *,
        label: Optional[str] = None,
        enabled: Optional[bool] = None,
        mode: Optional[str] = None,
        action: Optional[str] = None,
        release_action: Optional[str] = None,
        rising_action: Optional[str] = None,
        falling_action: Optional[str] = None,
        active_high: Optional[bool] = None,
        min_width_ms: Optional[float] = None,
    ) -> str:
        """
        Change how a BNC input ("IN1" or "IN2") is read. Returns "" when
        applied, otherwise why not; nothing changes unless everything is valid.
        Locked while a session runs. Any change withdraws an arming, because
        what was checked when it was armed may no longer be true.
        """
        if which not in ("IN1", "IN2"):
            return f"Unknown BNC input: {which}."
        if self._exp.is_running:
            return "BNC inputs are locked while a session runs."
        cfg = self._bnc_in1_cfg if which == "IN1" else self._bnc_in2_cfg
        if mode is not None and mode not in BNC_IN_MODES:
            return f"Unknown input mode: {mode}."
        for name, value in (("action", action), ("release_action", release_action),
                            ("rising_action", rising_action), ("falling_action", falling_action)):
            if value is not None and value not in ("",) + tuple(BNC_IN_ACTIONS):
                return f"Unknown action for {name}: {value}."
        if min_width_ms is not None and not 0 < float(min_width_ms) <= 60_000:
            return "The minimum pulse width must be between 0 and 60000 ms."

        changes = {
            "label": label, "enabled": enabled, "mode": mode, "action": action,
            "release_action": release_action, "rising_action": rising_action,
            "falling_action": falling_action, "active_high": active_high,
            "min_width_ms": None if min_width_ms is None else float(min_width_ms),
        }
        for name, value in changes.items():
            if value is not None:
                setattr(cfg, name, value)
        self._bnc_active_since.pop(which, None)
        if self._armed is not None:
            self.disarm_start()
        self._save_bnc_settings()
        return ""

    # ------------------------------------------------------------------
    # BNC settings that survive a restart
    # ------------------------------------------------------------------

    def enable_bnc_persistence(self, path: Optional[str] = None) -> List[str]:
        """
        Load the saved BNC configuration and keep saving it as it changes.
        Returns a description of each saved value that was skipped as unusable.

        Only the configuration comes back. Whether a session is armed is never
        saved, so a restart always starts unarmed.
        """
        self._bnc_settings = BncSettings(path)
        skipped = apply_settings(
            self._bnc_settings.load(), self._bnc_in1_cfg, self._bnc_in2_cfg, self._bnc_out_cfg
        )
        self._refresh_fast_out()
        if skipped and self._log:
            self._log.add(LogEntry(
                timestamp=time.time(), direction="SYS", node_id=0, frame_type="WARNING",
                event_name="BNC Settings Skipped", raw_id=0, raw_data=b"",
                details=f"{len(skipped)} saved BNC value(s) could not be used: " + "; ".join(skipped),
                source="BNC", fields={"skipped": skipped},
            ))
        return skipped

    def gui_settings(self) -> GuiSettings:
        """The remembered Connect-step values, loaded on first use."""
        if self._gui_settings is None:
            self._gui_settings = GuiSettings()
            if self._gui_settings.load_problem:
                self._notice("warning", self._gui_settings.load_problem)
        return self._gui_settings

    def enable_node_layout(self, path: Optional[str] = None) -> None:
        """Load the saved node positions and keep them for ``state``. See ``node_layout.py``."""
        self._layout = NodeLayout(path)
        if self._layout.load_problem:
            self._notice("warning", self._layout.load_problem)

    def _save_bnc_settings(self) -> None:
        if self._bnc_settings is None:
            return
        try:
            self._bnc_settings.save(self._bnc_in1_cfg, self._bnc_in2_cfg, self._bnc_out_cfg)
        except Exception as exc:  # noqa: BLE001 — a full disk must not stop the engine
            self._notice("warning", f"BNC settings could not be saved: {exc}")
            if self._log:
                self._log.add(LogEntry(
                    timestamp=time.time(), direction="SYS", node_id=0, frame_type="WARNING",
                    event_name="BNC Settings Not Saved", raw_id=0, raw_data=b"",
                    details=str(exc), source="BNC",
                ))

    def _on_experiment_event(self, name: str, fields: Dict[str, Any]) -> None:
        """An experiment logged something: pulse BNC OUT if the mapping follows it."""
        cfg = self._bnc_out_cfg
        if name == "trial" and cfg.mapping == "trial":
            self._pulse_out(cfg.pulse_width_us, "trial_start")
        elif name == "session_end" and cfg.mapping in ("session_start_stop", "trial"):
            # Twice as wide as a start, so the end can be told from a start.
            self._pulse_out(cfg.pulse_width_us * 2, "session_end")

    def _tick_periodic(self, now_mono: float) -> None:
        """Mapping "periodic": a pulse at the start of a session and then every interval."""
        cfg = self._bnc_out_cfg
        if cfg.mapping != "periodic" or not self._exp.is_running:
            self._next_periodic = None
            return
        interval = max(0.5, float(cfg.periodic_interval_s))
        if self._next_periodic is None:
            self._next_periodic = now_mono
        if now_mono >= self._next_periodic:
            self._pulse_out(cfg.pulse_width_us, "periodic")
            self._next_periodic += interval
            if self._next_periodic < now_mono:      # the engine stalled; don't fire a burst
                self._next_periodic = now_mono + interval

    def manual_bnc_pulse(self) -> None:
        """Fire BNC OUT once for bench testing, regardless of Enabled."""
        self._pulse_out(self._bnc_out_cfg.pulse_width_us, "manual")

    def _pulse_out(self, width_us: int, purpose: str) -> bool:
        """
        Ask for one BNC OUT pulse. The pulse thread owns the line, so a request
        made while a pulse is running is refused, and that is logged here. An
        accepted pulse is logged when it finishes (``_log_finished_pulses``),
        with the width and latency actually achieved.
        """
        accepted = self._io.pulse_bnc_out(width_us, purpose=purpose)
        if accepted is False:
            if self._log:
                self._log.add(LogEntry(
                    timestamp=time.time(), direction="TX", node_id=0, frame_type="BNC",
                    event_name="BNC OUT pulse refused", raw_id=0, raw_data=b"",
                    details=f"purpose={purpose} — a pulse was already running",
                    source="BNC", fields={"purpose": purpose, "requested_us": int(width_us)},
                ))
            return False
        self._ui_bnc_out_pulsed()
        return True

    def _log_finished_pulses(self) -> None:
        """Write one row for each BNC OUT pulse the pulse thread has finished, and each it refused."""
        while True:
            try:
                purpose, width_us, when = self._pulse_refusals.get_nowait()
            except queue.Empty:
                break
            if self._log:
                self._log.add(LogEntry(
                    timestamp=when, direction="TX", node_id=0, frame_type="BNC",
                    event_name="BNC OUT pulse refused", raw_id=0, raw_data=b"",
                    details=f"purpose={purpose} — a pulse was already running",
                    source="BNC", fields={"purpose": purpose, "requested_us": int(width_us)},
                ))
        while True:
            try:
                rec = self._pulse_records.get_nowait()
            except queue.Empty:
                return
            if not self._log:
                continue
            self._log.add(LogEntry(
                timestamp=rec.wall_rise, direction="TX", node_id=0, frame_type="BNC",
                event_name="BNC OUT pulse", raw_id=0, raw_data=b"",
                details=(
                    f"purpose={rec.purpose or '—'} requested={rec.requested_us / 1000:g}ms "
                    f"measured={rec.width_us / 1000:.3f}ms latency={rec.latency_us / 1000:.3f}ms"
                ),
                source="BNC",
                fields={
                    "purpose": rec.purpose,
                    "requested_us": rec.requested_us,
                    "width_us": round(rec.width_us, 1),
                    "latency_us": round(rec.latency_us, 1),
                    "t_mono_rise": round(rec.t_rise, 6),
                },
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
        pulse = self._session_start_pulse()
        if self._io is not None and pulse is not None:
            self._pulse_out(*pulse)

        faulted = []
        if self._registry is not None:
            faulted = [
                n.node_id for n in self._registry.all_nodes()
                if n.dispense_state == DispenseState.Fault
            ]
        details = f"{SYNC_FLASH_MS}ms LED" + (" + BNC OUT" if pulse is not None else "")
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

    def _session_start_pulse(self) -> Optional[Tuple[int, str]]:
        """The BNC OUT pulse for a session starting, as (width in us, purpose), or None for no pulse."""
        cfg = self._bnc_out_cfg
        if cfg.mapping == "legacy":
            return SYNC_FLASH_MS * 1000, "session_start_sync"
        if cfg.mapping == "session_start_sync":
            return cfg.sync_width_us, "session_start_sync"
        if cfg.mapping == "session_start_stop":
            return cfg.pulse_width_us, "session_start"
        return None

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

    @staticmethod
    def _arrival_time(msg) -> float:
        """
        When a CAN frame reached the base station.

        SocketCAN stamps each frame in the kernel as it arrives, and python-can
        carries that in ``msg.timestamp``. Using it keeps the row's time correct
        when the engine reads the frame late, however late. A missing,
        zero or implausible stamp (more than ``ARRIVAL_STAMP_MAX_SKEW_S`` from
        now, or in the future) falls back to the time of processing.
        """
        now = time.time()
        try:
            stamp = float(getattr(msg, "timestamp", 0.0) or 0.0)
        except (TypeError, ValueError):
            return now
        if stamp > 0.0 and stamp <= now + 1.0 and now - stamp < ARRIVAL_STAMP_MAX_SKEW_S:
            return stamp
        return now

    def _dispatch_rx(self, msg) -> None:
        arb_id = msg.arbitration_id
        data   = bytes(msg.data)
        ftype  = classify_frame(arb_id)
        rx_ts  = self._arrival_time(msg)

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
                timestamp=rx_ts,
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
        seconds: Optional[float] = None,
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
        # What nodes can be told is 100 ms to 65.535 s. Keep the interval the
        # offline timers use equal to what is sent, so a larger request can't
        # leave the timers waiting for beats the nodes will never skip.
        interval_ms = int(round(min(max(float(seconds), HEARTBEAT_MIN_S), HEARTBEAT_MAX_S) * 1000))
        self._hb_interval_s = interval_ms / 1000.0
        timeout_s = offline_timeout_for_heartbeat(self._hb_interval_s)
        if self._registry is not None:
            self._registry.set_offline_timeout(timeout_s)
        runner = self._exp.runner if self._exp is not None else None
        if runner is not None:
            runner.normalizer.set_online_timeout(timeout_s)
        if broadcast and self._can is not None:
            payload = build_setconfig_heartbeat(interval_ms)
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
