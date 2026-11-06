"""
app.py — SFM Developer GUI (DearPyGui front-end).

The engine that talks to the nodes lives in engine.py; this module is the
window on top of it.

Two screens:
  1. Setup  — configure CAN interface, node count, mode, logging.
  2. Main   — node grid + broadcast bar + event log panel.

The DearPyGui render callback fires every frame (~60 fps) and calls the
engine's ``tick()`` (see engine.py for what a tick does). The widgets are
repainted through the engine's ``_refresh_*`` hooks.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import dearpygui.dearpygui as dpg

from . import storage
from .io_manager import BNCInputConfig
from .log_manager import LogEntry, sanitize_session_name
from .experiment.schema import ExperimentDef, ExperimentParam, param_visible
from .protocol import (
    CanCmd,
    CanEvent,
    fault_user_message,
    build_setconfig_presence_factor,
    CAN_EVENT_DISPLAY_NAME,
    PRESENCE_FACTOR_MIN,
    PRESENCE_FACTOR_MAX,
)
from .engine import (
    BNC_IN_ACTIONS,
    COMMAND_PURPOSE,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    BaseStationEngine,
    ScheduleConfig,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_W = 1500
WINDOW_H = 1200        
TILE_W   = 320
TILE_H   = 340  # room for multi-line fault instructions + pellet counts
FAULT_TEXT_WRAP = TILE_W - 24  # stay inside child_window padding/border
LOG_VISIBLE_ROWS = 30  # fixed event-strip rows; created once, updated in place
LOG_TABLE_HEIGHT = 220


# _get_default_log_dir / _validate_log_dir moved to storage.py so headless
# tools (e.g. run_report.py) can resolve the log directory without importing
# dearpygui. Re-exported here under their historical names — no behavior change.
_get_default_log_dir = storage.default_log_dir
_validate_log_dir = storage.validate_log_dir


def _bnc_out_trigger_items() -> list:
    display_names_set = set(CAN_EVENT_DISPLAY_NAME.values())
    # Behavioral InputChanged names that appear in the log (pellet/load
    # edges are suppressed; dome close has no CanEvent milestone).
    display_names_set.update({
        "Dome closed",
        "MousePresence Detected",
        "MousePresence Cleared",
    })
    # Start with display names
    names = sorted(display_names_set)
    # Add raw event names not already in display map (exclude Pong, InputChanged)
    for ev in CanEvent:
        if ev.name not in ("Pong", "InputChanged") and ev.name.lower() not in {n.lower() for n in names}:
            names.append(ev.name)
    names = sorted(set(names))
    return ["any_event"] + names

# Status dot color tags (registered once at startup)
_COLOR_GREEN  = (60,  200, 80,  255)
_COLOR_BLUE   = (60,  130, 220, 255)
_COLOR_CYAN   = (50,  200, 220, 255)
_COLOR_YELLOW = (220, 200, 50,  255)
_COLOR_RED    = (220, 50,  50,  255)
_COLOR_GREY   = (120, 120, 120, 255)
_COLOR_AMBER  = (230, 140, 30,  255)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class SFMApp(BaseStationEngine):
    """DearPyGui front-end for the base-station engine."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(args)
        self._bnc_tiles: Dict[str, dict] = {}

        # GUI state
        self._screen = "setup"
        self._node_tiles: Dict[int, dict] = {}   # node_id → {tag dict}
        self._log_filter_node = 0                # 0 = all
        self._log_filter_type = "All"

        # Schema-driven experiment param widgets. Rebuilt on template change.
        self._exp_param_tags: Dict[str, int] = {}          # scalar param.key → widget tag
        self._exp_node_param_tags: Dict[str, Dict[int, int]] = {}  # node param.key → {node_id: tag}
        self._exp_param_groups: List[tuple] = []           # [(ExperimentParam, group_tag)]
        self._exp_input_tags: List[Any] = []               # every input widget (for run-lock)
        self._exp_inputs_locked: bool = False

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        dpg.create_context()
        self._setup_theme()
        self._setup_fonts()
        self._build_setup_screen()
        dpg.create_viewport(
            title="SFM Developer GUI",
            width=WINDOW_W,
            height=WINDOW_H,
            resizable=True,
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        if hasattr(dpg, "set_render_callback"):
            dpg.set_render_callback(self._on_render)
        else:
            dpg.set_frame_callback(1, self._make_render_callback())
        dpg.start_dearpygui()
        self._shutdown()
        dpg.destroy_context()

    def _make_render_callback(self):
        """Return a frame callback that reschedules itself for the next frame."""
        def _frame_callback() -> None:
            self._on_render()
            if hasattr(dpg, "get_frame_count"):
                dpg.set_frame_callback(dpg.get_frame_count() + 1, _frame_callback)

        return _frame_callback

    # ------------------------------------------------------------------
    # Theme + Fonts
    # ------------------------------------------------------------------

    def _setup_theme(self) -> None:
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg,       (18,  20,  24,  255))
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg,        (26,  28,  35,  255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg,        (38,  42,  52,  255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (50,  56,  70,  255))
                dpg.add_theme_color(dpg.mvThemeCol_Button,         (45,  105, 195, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,  (60,  130, 220, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,   (30,  80,  170, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Header,         (45,  105, 195, 100))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered,  (60,  130, 220, 150))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive,  (30,  60,  120, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,           (220, 225, 235, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Border,         (60,  65,  80,  255))
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding,  6)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding,   6)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,   4)
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding,    4)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,     8, 6)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,   12, 12)
        dpg.bind_theme(global_theme)

    def _setup_fonts(self) -> None:
        """Register a Unicode-capable font for the GUI.

        The default DearPyGui font can fall back to replacement glyphs for
        symbols like ●, ○, and other punctuation, which shows up as '?' in the
        UI. Loading a system font with broader Unicode coverage fixes that.
        """
        candidates = [
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        ]
        self._font = None
        self._mono_font = None

        for font_path in candidates:
            if not font_path.exists():
                continue
            with dpg.font_registry():
                self._font = dpg.add_font(str(font_path), 16)
            dpg.bind_font(self._font)
            break

        if self._font is None:
            self._mono_font = None

    # ------------------------------------------------------------------
    # Setup Screen
    # ------------------------------------------------------------------

    def _build_setup_screen(self) -> None:
        vp_w, vp_h = WINDOW_W, WINDOW_H
        win_w, win_h = 480, 540

        with dpg.window(
            tag="setup_window",
            label="SFM Developer GUI",
            width=win_w,
            height=win_h,
            pos=((vp_w - win_w) // 2, (vp_h - win_h) // 2),
            no_close=True,
            no_collapse=True,
            no_move=True,
            no_resize=True,
        ):
            dpg.add_spacer(height=8)
            dpg.add_text("SFM Developer GUI", color=(100, 180, 255, 255))
            dpg.add_text(
                "Spatial Foraging Module — Base Station Control & Monitoring",
                color=(160, 165, 175, 255),
            )
            dpg.add_separator()
            dpg.add_spacer(height=6)

            # -- CAN Interface --
            with dpg.collapsing_header(label="CAN Interface", default_open=True):
                dpg.add_input_text(
                    tag="setup_interface",
                    label="Interface",
                    default_value=self._args.interface,
                    width=160,
                    callback=self._on_check_can_status,
                    on_enter=True,
                )
                dpg.add_input_int(
                    tag="setup_bitrate",
                    label="Bitrate (bps)",
                    default_value=self._args.bitrate,
                    width=160,
                    readonly=True,
                    step=0,
                )
                with dpg.group(horizontal=True):
                    dpg.add_text("Driver status:", color=(160, 165, 175, 255))
                    dpg.add_text("", tag="setup_can_status", color=_COLOR_GREY)
                    dpg.add_button(label="Check", width=60, callback=self._on_check_can_status)

            dpg.add_spacer(height=6)

            # -- Node Configuration --
            with dpg.collapsing_header(label="Node Configuration", default_open=True):
                dpg.add_input_int(
                    tag="setup_num_nodes",
                    label="Number of nodes",
                    default_value=self._args.nodes,
                    width=120,
                    min_value=1,
                    max_value=254,
                    min_clamped=True,
                    max_clamped=True,
                )

            dpg.add_spacer(height=6)

            # -- Logging --
            with dpg.collapsing_header(label="Logging", default_open=True):
                dpg.add_input_text(
                    tag="setup_log_dir",
                    label="Log directory",
                    default_value=_get_default_log_dir(),
                    width=260,
                )
                dpg.add_checkbox(tag="setup_auto_save", label="Auto-save to CSV", default_value=True)

            dpg.add_spacer(height=12)
            dpg.add_separator()
            dpg.add_spacer(height=8)

            # Error text (hidden until needed)
            dpg.add_text("", tag="setup_error", color=(220, 80, 80, 255))

            dpg.add_button(
                tag="setup_start_btn",
                label="   Start Session   ",
                width=200,
                callback=self._on_start_session,
            )

        self._on_check_can_status()

    def _on_check_can_status(self, *_args) -> None:
        """Check whether the configured SocketCAN interface exists and is UP."""
        interface = dpg.get_value("setup_interface").strip() if dpg.does_item_exist("setup_interface") else self._args.interface
        up = self._is_can_interface_up(interface)
        if up:
            dpg.configure_item("setup_can_status", default_value=f"● {interface} online", color=_COLOR_GREEN)
        else:
            dpg.configure_item("setup_can_status", default_value=f"● {interface} not found", color=_COLOR_RED)


    def _on_start_session(self) -> None:
        interface  = dpg.get_value("setup_interface").strip()
        bitrate    = dpg.get_value("setup_bitrate")
        num_nodes  = dpg.get_value("setup_num_nodes")
        log_dir    = dpg.get_value("setup_log_dir").strip()
        auto_save  = dpg.get_value("setup_auto_save")

        error = self.connect(interface, bitrate, num_nodes, log_dir, auto_save)
        if error:
            dpg.set_value("setup_error", error)
            return

        # Transition to main screen
        dpg.delete_item("setup_window")
        self._build_main_screen(num_nodes)
        self._screen = "main"

        # Probe the bus immediately: every already-running node answers with a
        # Pong carrying its live MAC (_handle_pong_mac), so tiles show MAC and
        # online status now instead of waiting for heartbeats / the per-node
        # MAC-resolution retry timer.
        self._broadcast(CanCmd.Ping)

    # ------------------------------------------------------------------
    # Main Screen
    # ------------------------------------------------------------------

    def _build_main_screen(self, num_nodes: int) -> None:
        with dpg.window(
            tag="main_window",
            label="SFM Developer GUI — Main",
            width=WINDOW_W,
            height=WINDOW_H,
            pos=(0, 0),
            no_close=True,
            no_collapse=True,
            no_move=True,
            no_resize=True,
            no_title_bar=True,
        ):
            # -- Broadcast bar --
            self._build_broadcast_bar()
            dpg.add_separator()
            dpg.add_spacer(height=4)

            # -- Node grid --
            self._build_node_grid(num_nodes)
            dpg.add_spacer(height=6)
            dpg.add_separator()

            # -- Experiment panel --
            self._build_experiment_panel()
            dpg.add_spacer(height=6)
            dpg.add_separator()

            # -- BNC / Sync I/O --
            self._build_bnc_panel()
            dpg.add_spacer(height=6)
            dpg.add_separator()

            # -- Event log --
            self._build_log_panel()

    def _build_broadcast_bar(self) -> None:
        with dpg.group(horizontal=True):
            dpg.add_button(
                tag="discovery_btn",
                label="Re-discover",
                width=110,
                callback=self._on_rediscover,
            )
            dpg.add_spacer(width=12)
            dpg.add_text("Broadcast:", color=(160, 165, 175, 255))
            dpg.add_button(label="Dispense All",  width=110,
                           callback=lambda: self._broadcast(CanCmd.Dispense))
            dpg.add_button(label="Recover All",   width=90,
                           callback=lambda: self._broadcast(CanCmd.Recover))
            dpg.add_button(label="Ping All",      width=80,
                           callback=lambda: self._broadcast(CanCmd.Ping))
            dpg.add_spacer(width=12)
            dpg.add_button(
                tag="presence_cal_btn",
                label="Calibrate Presence",
                width=150,
                callback=self._on_open_presence_cal_dialog,
            )
            dpg.add_spacer(width=12)
            dpg.add_button(
                tag="dev_menu_btn",
                label="Developer Menu",
                width=140,
                callback=self._on_open_dev_menu,
            )
            dpg.add_spacer(width=12)
            dpg.add_text("Heartbeat (s):", color=(160, 165, 175, 255))
            dpg.add_input_int(
                tag="hb_interval_input",
                default_value=DEFAULT_HEARTBEAT_INTERVAL_S,
                width=100,
                step=1,
            )
            dpg.add_button(
                label="Apply HB",
                width=80,
                callback=self._on_apply_heartbeat_interval,
            )

    def _build_node_grid(self, num_nodes: int) -> None:
        cols = min(num_nodes, 3)
        with dpg.collapsing_header(label="Nodes", default_open=True):
            with dpg.table(
                tag="node_grid_table",
                header_row=False,
                borders_innerH=False,
                borders_innerV=False,
                borders_outerH=False,
                borders_outerV=False,
            ):
                for _ in range(cols):
                    dpg.add_table_column(width_fixed=True, init_width_or_weight=TILE_W + 12)

                row_tag = None
                for i, node_id in enumerate(range(1, num_nodes + 1)):
                    if i % cols == 0:
                        row_tag = dpg.add_table_row(parent="node_grid_table")
                    with dpg.table_cell(parent=row_tag):
                        self._build_node_tile(node_id)

    def _build_node_tile(self, node_id: int) -> None:
        tags: dict = {}
        with dpg.child_window(width=TILE_W, height=TILE_H, border=True):

            # -- Header row: label + status dot --
            # DearPyGui always passes (sender, app_data, user_data); defaults on
            # lambda params are overridden by None unless user_data= is set.
            with dpg.group(horizontal=True):
                tags["label_input"] = dpg.add_input_text(
                    default_value=f"Node {node_id}",
                    width=120,
                    on_enter=True,
                    user_data=node_id,
                    callback=lambda s, a, u: self._on_label_change(u, a),
                )
                tags["status_dot"] = dpg.add_text("●", color=_COLOR_GREY)
                tags["status_text"] = dpg.add_text("OFFLINE", color=_COLOR_GREY)
                with dpg.tooltip(tags["status_text"]):
                    dpg.add_text(
                        "EMPTY = plate raised with NO pellet (a no-feed / mimic\n"
                        "dispense). Not a fault — see Status below for hopper\n"
                        "or actuator problems.",
                    )

            # -- Identity --
            dpg.add_separator()
            with dpg.group(horizontal=True):
                tags["can_id_text"]  = dpg.add_text(f"ID: {node_id}")
                dpg.add_spacer(width=10)
                tags["mac_text"]     = dpg.add_text("MAC: —")

            dpg.add_separator()

            # -- Sensor state --
            with dpg.group(horizontal=True):
                dpg.add_text("HB:", color=(160,165,175,255))
                tags["hb_text"] = dpg.add_text("—")
                dpg.add_spacer(width=12)
                dpg.add_text("Presence:", color=(160,165,175,255))
                tags["presence_text"] = dpg.add_text("○", color=_COLOR_GREY)

            with dpg.group(horizontal=True):
                dpg.add_text("Sensors:", color=(160,165,175,255))
                tags["pellet_text"] = dpg.add_text("Pellet: ○")
                dpg.add_spacer(width=6)
                tags["load_position_text"] = dpg.add_text("Load: ○")
                dpg.add_spacer(width=6)
                tags["dome_text"] = dpg.add_text("Dome: ○")

            with dpg.group(horizontal=True):
                dpg.add_text("Pellets:", color=(160,165,175,255))
                tags["pellet_count_text"] = dpg.add_text("—", color=_COLOR_GREY)

            # Single wrapped line: "Status: Offline" until online, then
            # "Status: Healthy" or "Status: <fault instruction>" (wrapped)
            tags["fault_text"] = dpg.add_text(
                "Status: Offline", wrap=FAULT_TEXT_WRAP, color=_COLOR_GREY,
            )
            tags["warn_text"] = dpg.add_text("", color=_COLOR_AMBER, wrap=FAULT_TEXT_WRAP)

            dpg.add_separator()

            # -- Command buttons --
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Dispense", width=85, user_data=node_id,
                    callback=lambda s, a, u: self._send_cmd(u, CanCmd.Dispense),
                )
                tags["recover_btn"] = dpg.add_button(
                    label="Recover", width=70, user_data=node_id,
                    callback=lambda s, a, u: self._on_recover_node(u),
                )
                dpg.add_button(
                    label="Ping", width=55, user_data=node_id,
                    callback=lambda s, a, u: self._send_cmd(u, CanCmd.Ping),
                )

            # -- AssignId override --
            with dpg.group(horizontal=True):
                tags["assign_input"] = dpg.add_input_int(
                    default_value=node_id, width=80,
                    min_value=1, max_value=254,
                    min_clamped=True, max_clamped=True,
                )
                dpg.add_button(
                    label="AssignId",
                    user_data=node_id,
                    callback=lambda s, a, u: self._on_assign_id(u),
                )

        self._node_tiles[node_id] = tags

    # ------------------------------------------------------------------
    # BNC / Sync I/O panel
    # ------------------------------------------------------------------

    def _build_bnc_panel(self) -> None:
        with dpg.collapsing_header(label="BNC / Sync I/O", default_open=False):
            dpg.add_text(
                "BNC IN: pick an action per edge (rising / falling) — broadcast command "
                "or start/stop the experiment; leave an edge as (none) to ignore it. "
                "BNC OUT Trigger: pulse on matching CAN event name (or any_event).",
                color=(140, 145, 155, 255), wrap=WINDOW_W - 40,
            )
            with dpg.group(horizontal=True):
                self._build_bnc_input_box(0, self._bnc_in1_cfg)
                dpg.add_spacer(width=16)
                self._build_bnc_input_box(1, self._bnc_in2_cfg)
                dpg.add_spacer(width=16)
                self._build_bnc_output_box()

    def _build_bnc_input_box(self, idx: int, cfg: BNCInputConfig) -> None:
        key = f"bnc_in{idx}"
        tags: dict = {"last_edge_ts": 0.0}
        with dpg.child_window(width=335, height=195, border=True):
            with dpg.group(horizontal=True):
                tags["dot"] = dpg.add_text("●", color=_COLOR_GREY)
                dpg.add_text(f"BNC IN {idx}")
            dpg.add_separator()
            dpg.add_input_text(
                label="Label", default_value=cfg.label, width=150,
                user_data=cfg,
                callback=lambda s, a, u: setattr(u, "label", a),
            )
            rising_default = cfg.rising_action if cfg.rising_action in BNC_IN_ACTIONS else "(none)"
            dpg.add_combo(
                label="Rising action", items=BNC_IN_ACTIONS,
                default_value=rising_default, width=180,
                user_data=cfg,
                callback=lambda s, a, u: setattr(
                    u, "rising_action", "" if a == "(none)" else a
                ),
            )
            falling_default = cfg.falling_action if cfg.falling_action in BNC_IN_ACTIONS else "(none)"
            dpg.add_combo(
                label="Falling action", items=BNC_IN_ACTIONS,
                default_value=falling_default, width=180,
                user_data=cfg,
                callback=lambda s, a, u: setattr(
                    u, "falling_action", "" if a == "(none)" else a
                ),
            )
            dpg.add_checkbox(
                label="Enabled", default_value=cfg.enabled,
                user_data=cfg,
                callback=lambda s, a, u: setattr(u, "enabled", a),
            )
            tags["last_text"] = dpg.add_text("Last: —", color=(160, 165, 175, 255))
        self._bnc_tiles[key] = tags

    def _build_bnc_output_box(self) -> None:
        cfg = self._bnc_out_cfg
        tags: dict = {"last_pulse_ts": 0.0}
        triggers = _bnc_out_trigger_items()
        default_trigger = cfg.trigger if cfg.trigger in triggers else "Loaded"
        with dpg.child_window(width=300, height=195, border=True):
            with dpg.group(horizontal=True):
                tags["dot"] = dpg.add_text("●", color=_COLOR_GREY)
                dpg.add_text("BNC OUT")
            dpg.add_separator()
            dpg.add_input_text(
                label="Label", default_value=cfg.label, width=150,
                user_data=cfg,
                callback=lambda s, a, u: setattr(u, "label", a),
            )
            dpg.add_input_int(
                label="Pulse (ms)", default_value=max(1, cfg.pulse_width_us // 1000), width=100,
                min_value=1, max_value=10000, min_clamped=True, max_clamped=True,
                user_data=cfg,
                callback=lambda s, a, u: setattr(u, "pulse_width_us", a * 1000),
            )
            dpg.add_combo(
                label="Trigger", items=triggers,
                default_value=default_trigger, width=180,
                user_data=cfg,
                callback=lambda s, a, u: setattr(u, "trigger", a),
            )
            if not cfg.trigger:
                cfg.trigger = default_trigger
            dpg.add_checkbox(
                label="Enabled", default_value=cfg.enabled,
                user_data=cfg,
                callback=lambda s, a, u: setattr(u, "enabled", a),
            )
            dpg.add_button(label="Manual Pulse", callback=lambda s, a, u: self._on_bnc_manual_pulse())
        self._bnc_tiles["bnc_out"] = tags

    def _build_experiment_panel(self) -> None:
        labels = [d.label for d in self._exp_defs] or ["(no experiments)"]
        default_def = self._default_experiment_def()
        with dpg.collapsing_header(label="Experiment", default_open=True):
            if self._exp_defs:
                dpg.add_text(
                    default_def.description if default_def else self._exp_defs[0].description,
                    tag="exp_description",
                    color=(140, 145, 155, 255),
                    wrap=WINDOW_W - 40,
                )
            with dpg.group(horizontal=True):
                dpg.add_text("Session name:", color=(160, 165, 175, 255))
                dpg.add_input_text(
                    tag="exp_session_name",
                    width=260,
                    callback=self._on_session_name_changed,
                )
            dpg.add_text(
                "Enter a session name to begin.",
                tag="exp_session_hint",
                color=(140, 145, 155, 255),
                wrap=WINDOW_W - 40,
            )
            with dpg.group(horizontal=True):
                dpg.add_text("Template:", color=(160, 165, 175, 255))
                dpg.add_combo(
                    tag="exp_template_combo",
                    items=labels,
                    default_value=default_def.label if default_def else labels[0],
                    width=220,
                    callback=self._on_experiment_template_changed,
                )
                dpg.add_button(
                    tag="exp_start_btn",
                    label="Start",
                    width=80,
                    callback=self._on_experiment_start,
                )
                dpg.add_button(
                    tag="exp_stop_btn",
                    label="Stop",
                    width=80,
                    callback=self._on_experiment_stop,
                )
            dpg.add_text("Idle", tag="exp_status_text", color=(160, 165, 175, 255))
            with dpg.group(horizontal=True):
                dpg.add_text("Pellets:", color=(160, 165, 175, 255))
                dpg.add_text("—", tag="exp_pellet_text", color=_COLOR_GREY)
            dpg.add_group(tag="exp_params_group")
            if self._exp_defs:
                self._rebuild_experiment_params(default_def or self._exp_defs[0])


    def _selected_experiment_def(self) -> Optional[ExperimentDef]:
        if not self._exp_defs:
            return None
        label = dpg.get_value("exp_template_combo") if dpg.does_item_exist("exp_template_combo") else None
        for d in self._exp_defs:
            if d.label == label:
                return d
        return self._default_experiment_def()

    def _on_experiment_template_changed(self, sender=None, app_data=None, user_data=None) -> None:
        exp_def = self._selected_experiment_def()
        if exp_def is None:
            return
        if dpg.does_item_exist("exp_description"):
            dpg.set_value("exp_description", exp_def.description)
        self._rebuild_experiment_params(exp_def)

    def _on_session_name_changed(self, sender=None, app_data=None, user_data=None) -> None:
        self._refresh_session_hint()
        self._refresh_experiment_status()

    def _refresh_session_hint(self) -> None:
        """Preview what Start will do with the current session-name text."""
        if not dpg.does_item_exist("exp_session_hint"):
            return
        raw = dpg.get_value("exp_session_name") if dpg.does_item_exist("exp_session_name") else ""
        raw = (raw or "").strip()
        if not sanitize_session_name(raw):
            dpg.set_value("exp_session_hint", "Enter a session name to begin.")
            return
        info = self._log.preview_session_path(raw, self._exp_log_dir) if self._log else None
        if info is None or info["path"] is None:
            dpg.set_value("exp_session_hint", "Enter a session name to begin.")
            return
        if info["will_create"]:
            dpg.set_value("exp_session_hint", f"→ will create {info['path']}")
        else:
            resume = info.get("resume") or {}
            extra = ""
            if resume.get("from_run"):
                nxt = int(resume.get("trials") or 0) + 1
                loaded = int(resume.get("pellets") or 0)
                taken = sum(int(v) for v in (resume.get("node_taken") or {}).values())
                extra = f", resuming at trial {nxt}, pellets {loaded} → {taken}"
            dpg.set_value(
                "exp_session_hint",
                f"→ appending to {info['path']} (run {info['next_run_id']}, "
                f"{info['row_count']:,} existing row(s){extra})",
            )

    # ------------------------------------------------------------------
    # Schema-driven parameter form
    # ------------------------------------------------------------------

    def _rebuild_experiment_params(self, exp_def: ExperimentDef) -> None:
        """Render every param from the JSON schema — no per-template code."""
        if not dpg.does_item_exist("exp_params_group"):
            return
        for child in dpg.get_item_children("exp_params_group", slot=1) or []:
            dpg.delete_item(child)
        self._exp_param_tags = {}
        self._exp_node_param_tags = {}
        self._exp_param_groups = []
        self._exp_input_tags = []
        if dpg.does_item_exist("exp_session_name"):
            self._exp_input_tags.append("exp_session_name")
        if dpg.does_item_exist("exp_template_combo"):
            self._exp_input_tags.append("exp_template_combo")
        self._refresh_session_hint()

        # Keys that gate another param's visibility need a change callback.
        controllers = set()
        for p in exp_def.parameters:
            if p.visible_when:
                controllers.update(p.visible_when.keys())

        for param in exp_def.parameters:
            group_tag = dpg.add_group(parent="exp_params_group")
            self._exp_param_groups.append((param, group_tag))
            if param.is_node_param:
                self._render_node_param(param, group_tag)
            else:
                self._render_scalar_param(param, group_tag, notify=param.key in controllers)

        self._apply_param_visibility(exp_def)

    def _render_scalar_param(self, param: ExperimentParam, parent, notify: bool) -> None:
        tag = f"exp_param_{param.key}"
        cb = self._on_param_controller_changed if notify else None
        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text(f"{param.label}:", color=(160, 165, 175, 255))
            if param.type == "bool":
                widget = dpg.add_checkbox(
                    tag=tag, default_value=bool(param.default), callback=cb,
                )
            elif param.type == "int":
                kwargs = {"tag": tag, "default_value": int(param.default or 0), "width": 120}
                if param.min is not None:
                    kwargs["min_value"] = int(param.min); kwargs["min_clamped"] = True
                if param.max is not None:
                    kwargs["max_value"] = int(param.max); kwargs["max_clamped"] = True
                widget = dpg.add_input_int(callback=cb, **kwargs)
            elif param.type == "float":
                kwargs = {"tag": tag, "default_value": float(param.default or 0.0),
                          "width": 120, "format": "%.2f"}
                if param.min is not None:
                    kwargs["min_value"] = float(param.min); kwargs["min_clamped"] = True
                if param.max is not None:
                    kwargs["max_value"] = float(param.max); kwargs["max_clamped"] = True
                widget = dpg.add_input_float(callback=cb, **kwargs)
            elif param.type == "choice":
                opts = param.options or [str(param.default)]
                widget = dpg.add_combo(
                    tag=tag, items=opts, width=160, callback=cb,
                    default_value=str(param.default if param.default in opts else opts[0]),
                )
            else:  # str
                widget = dpg.add_input_text(
                    tag=tag, width=160, callback=cb,
                    default_value="" if param.default is None else str(param.default),
                )
            self._exp_param_tags[param.key] = widget
            self._exp_input_tags.append(widget)
            if param.help:
                dpg.add_text(param.help, color=(120, 125, 135, 255))

    def _render_node_param(self, param: ExperimentParam, parent) -> None:
        """Render one widget per registry node (checkbox / number / dropdown)."""
        node_ids = self._registry_node_ids()
        self._exp_node_param_tags[param.key] = {}
        default_pct = max(0, round(100 / len(node_ids))) if node_ids else 0
        with dpg.group(parent=parent):
            with dpg.group(horizontal=True):
                dpg.add_text(f"{param.label}:", color=(160, 165, 175, 255))
                for node_id in node_ids:
                    if param.type == "nodes":
                        w = dpg.add_checkbox(label=str(node_id), default_value=True)
                    elif param.type == "node_number":
                        lo = int(param.min) if param.min is not None else 0
                        hi = int(param.max) if param.max is not None else 100
                        w = dpg.add_input_int(
                            label=str(node_id), width=55, step=0,
                            default_value=min(max(default_pct, lo), hi),
                            min_value=lo, max_value=hi,
                            min_clamped=True, max_clamped=True,
                        )
                    else:  # node_choice
                        opts = param.options or [str(param.default)]
                        default = str(param.default if param.default in opts else opts[0])
                        w = dpg.add_combo(
                            label=str(node_id), items=opts, default_value=default, width=90,
                        )
                    self._exp_node_param_tags[param.key][node_id] = w
                    self._exp_input_tags.append(w)
                if param.type == "nodes":
                    b_all = dpg.add_button(
                        label="All", width=45,
                        callback=lambda s, a, u, k=param.key: self._set_all_nodes(k, True),
                    )
                    b_none = dpg.add_button(
                        label="None", width=50,
                        callback=lambda s, a, u, k=param.key: self._set_all_nodes(k, False),
                    )
                    self._exp_input_tags += [b_all, b_none]
            if param.help:
                dpg.add_text(param.help, color=(120, 125, 135, 255), wrap=WINDOW_W - 60)

    def _set_all_nodes(self, key: str, value: bool) -> None:
        for tag in self._exp_node_param_tags.get(key, {}).values():
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, value)

    def _on_param_controller_changed(self, sender=None, app_data=None, user_data=None) -> None:
        exp_def = self._selected_experiment_def()
        if exp_def is not None:
            self._apply_param_visibility(exp_def)

    def _controller_values(self, exp_def: ExperimentDef) -> dict:
        """Current values of scalar params, for evaluating visible_when."""
        vals = {}
        for param in exp_def.parameters:
            if param.is_node_param:
                continue
            tag = self._exp_param_tags.get(param.key)
            vals[param.key] = (
                dpg.get_value(tag) if tag is not None and dpg.does_item_exist(tag)
                else param.default
            )
        return vals

    def _apply_param_visibility(self, exp_def: ExperimentDef) -> None:
        vals = self._controller_values(exp_def)
        for param, group_tag in self._exp_param_groups:
            if dpg.does_item_exist(group_tag):
                dpg.configure_item(group_tag, show=param_visible(param, vals))

    def _collect_experiment_params(self, exp_def: ExperimentDef) -> dict:
        """Read every param back. node_number/node_choice → {node_id: value};
        the 'nodes' param is skipped (it supplies the nodes= argument)."""
        vals = self._controller_values(exp_def)
        values: dict = {}
        for param in exp_def.parameters:
            if param.type == "nodes":
                continue
            if not param_visible(param, vals):
                values[param.key] = param.default
                continue
            if param.is_node_param:
                values[param.key] = self._collect_node_param(param)
            else:
                tag = self._exp_param_tags.get(param.key)
                values[param.key] = (
                    dpg.get_value(tag) if tag is not None and dpg.does_item_exist(tag)
                    else param.default
                )
        return values

    def _collect_node_param(self, param: ExperimentParam) -> dict:
        result = {}
        for node_id, tag in self._exp_node_param_tags.get(param.key, {}).items():
            if dpg.does_item_exist(tag):
                result[node_id] = dpg.get_value(tag)
        return result

    def _effective_nodes(self, exp_def: ExperimentDef) -> List[int]:
        """The active node set (also the nodes= argument to build_experiment)."""
        for param in exp_def.parameters:
            if param.type == "nodes":
                tags = self._exp_node_param_tags.get(param.key, {})
                return [n for n, t in tags.items() if dpg.does_item_exist(t) and dpg.get_value(t)]
        for param in exp_def.parameters:
            if param.type == "node_number":
                tags = self._exp_node_param_tags.get(param.key, {})
                return [n for n, t in tags.items()
                        if dpg.does_item_exist(t) and (dpg.get_value(t) or 0) > 0]
        for param in exp_def.parameters:
            if param.type == "node_choice":
                inactive = param.options[0] if param.options else "off"
                tags = self._exp_node_param_tags.get(param.key, {})
                return [n for n, t in tags.items()
                        if dpg.does_item_exist(t) and dpg.get_value(t) != inactive]
        return self._registry_node_ids()

    # ------------------------------------------------------------------
    # Start / stop (locks the whole config while running)
    # ------------------------------------------------------------------

    def _set_experiment_inputs_enabled(self, enabled: bool) -> None:
        self._exp_inputs_locked = not enabled
        for tag in self._exp_input_tags:
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=enabled)

    def _on_experiment_start(self, sender=None, app_data=None, user_data=None) -> None:
        exp_def = self._selected_experiment_def()
        if exp_def is None or self._can is None or self._registry is None:
            return
        if self._exp.is_running:
            return

        session_name = dpg.get_value("exp_session_name").strip() if dpg.does_item_exist("exp_session_name") else ""
        started, message = self.start_experiment(
            exp_def,
            session_name,
            lambda: (self._collect_experiment_params(exp_def), self._effective_nodes(exp_def)),
        )
        if message:
            dpg.set_value("exp_status_text", message)
        if started:
            self._set_experiment_inputs_enabled(False)  # lock config while running
            dpg.configure_item("exp_start_btn", enabled=False)
            self._refresh_experiment_status()
            self._refresh_session_hint()

    def _on_experiment_stop(self, sender=None, app_data=None, user_data=None) -> None:
        self.stop_experiment()
        self._set_experiment_inputs_enabled(True)
        if dpg.does_item_exist("exp_start_btn"):
            dpg.configure_item("exp_start_btn", enabled=True)
        self._refresh_experiment_status()


    def _refresh_experiment_status(self) -> None:
        if not dpg.does_item_exist("exp_status_text"):
            return
        dpg.set_value("exp_status_text", self._exp.status_line())
        running = self._exp.is_running
        session_name = dpg.get_value("exp_session_name").strip() if dpg.does_item_exist("exp_session_name") else ""
        has_session_name = bool(sanitize_session_name(session_name))
        if dpg.does_item_exist("exp_start_btn"):
            dpg.configure_item("exp_start_btn", enabled=not running and has_session_name)
        # Calibrating pauses presence sampling on every node for ~5s and
        # rewrites NVS thresholds — never allow it mid-run, since a running
        # experiment may depend on live presence readings.
        if dpg.does_item_exist("presence_cal_btn"):
            dpg.configure_item("presence_cal_btn", enabled=not running)
        # Re-enable the form when a run ends on its own (pellet cap / duration)
        # and send further traffic to the daily activity log.
        if not running:
            self._resume_daily_log()
            if self._exp_inputs_locked:
                self._set_experiment_inputs_enabled(True)


    def _refresh_pellet_counters(self) -> None:
        """Experiment-panel total and per-node tile counts."""
        tallies = self._experiment_pellet_tallies()
        if dpg.does_item_exist("exp_pellet_text"):
            if tallies is None:
                dpg.configure_item("exp_pellet_text", default_value="—", color=_COLOR_GREY)
            else:
                presented = sum(t.presented for t in tallies.values())
                taken = sum(t.taken for t in tallies.values())
                dpg.configure_item(
                    "exp_pellet_text",
                    default_value=f"{presented} presented · {taken} taken",
                    color=(200, 210, 220, 255),
                )
        for node_id, tags in self._node_tiles.items():
            tally = tallies.get(node_id) if tallies is not None else None
            if tally is None:
                dpg.configure_item(tags["pellet_count_text"], default_value="—", color=_COLOR_GREY)
            else:
                dpg.configure_item(
                    tags["pellet_count_text"],
                    default_value=f"{tally.presented} presented / {tally.taken} taken",
                    color=(200, 210, 220, 255),
                )

    def _build_log_panel(self) -> None:
        dpg.add_text("Event Log", color=(100, 180, 255, 255))
        with dpg.group(horizontal=True):
            dpg.add_text("Node:", color=(160,165,175,255))
            dpg.add_combo(
                tag="log_filter_node",
                items=["All"] + [str(i) for i in range(1, (self._registry.num_nodes() if self._registry else 10) + 1)],
                default_value="All",
                width=80,
                callback=self._refresh_log_table,
            )
            dpg.add_text("Type:", color=(160,165,175,255))
            dpg.add_combo(
                tag="log_filter_type",
                items=["All", "EVENT", "COMMAND", "DISCOVERY", "BNC", "SYNC", "WARNING", "EXPERIMENT"],
                default_value="All",
                width=120,
                callback=self._refresh_log_table,
            )
            dpg.add_button(label="Clear", callback=self._on_log_clear)
            # TODO(report-gui-hook): a "Report" button belongs here, calling
            # base_station.report.build_session_report(self._log.csv_path, ...)
            # on a background threading.Thread (this render loop is single-
            # threaded — see _make_render_callback/_on_render — so report
            # generation on a multi-hour session must not run inline).
            dpg.add_text("", tag="log_count_text", color=(160,165,175,255))

        with dpg.table(
            tag="log_table",
            header_row=True,
            borders_innerH=True,
            borders_innerV=True,
            borders_outerH=True,
            borders_outerV=True,
            scrollY=True,
            freeze_rows=1,
            height=LOG_TABLE_HEIGHT,
            policy=dpg.mvTable_SizingFixedFit,
        ):
            dpg.add_table_column(label="Time",      width_fixed=True, init_width_or_weight=95)
            dpg.add_table_column(label="Node",      width_fixed=True, init_width_or_weight=60)
            dpg.add_table_column(label="Dir",       width_fixed=True, init_width_or_weight=35)
            dpg.add_table_column(label="Type",      width_fixed=True, init_width_or_weight=90)
            dpg.add_table_column(label="Event",     width_fixed=True, init_width_or_weight=150)
            dpg.add_table_column(label="ID",        width_fixed=True, init_width_or_weight=55)
            dpg.add_table_column(label="Data",      width_stretch=True, init_width_or_weight=0.4)
            dpg.add_table_column(label="Details",   width_stretch=True, init_width_or_weight=1.6)
            for i in range(LOG_VISIBLE_ROWS):
                with dpg.table_row(tag=f"log_row_{i}", show=False):
                    dpg.add_text("", tag=f"log_time_{i}")
                    dpg.add_text("", tag=f"log_node_{i}")
                    dpg.add_text("", tag=f"log_dir_{i}")
                    dpg.add_text("", tag=f"log_type_{i}")
                    dpg.add_text("", tag=f"log_event_{i}")
                    dpg.add_text("", tag=f"log_id_{i}")
                    dpg.add_text("", tag=f"log_data_{i}")
                    dpg.add_text("", tag=f"log_details_{i}")

    # ------------------------------------------------------------------
    # Render callback (called every frame)
    # ------------------------------------------------------------------

    def _on_render(self) -> None:
        if self._screen != "main":
            return
        self.tick()

    # ------------------------------------------------------------------
    # Tile refresh
    # ------------------------------------------------------------------

    def _refresh_tile(self, node_id: int) -> None:
        if not self._registry:
            return
        node = self._registry.get(node_id)
        if node is None:
            return
        tags = self._node_tiles.get(node_id)
        if tags is None:
            return

        color = node.status_color

        dpg.configure_item(tags["status_dot"],  color=color)
        dpg.configure_item(tags["status_text"], default_value=node.status_label, color=color)
        dpg.set_value(tags["label_input"],      node.label)
        dpg.configure_item(tags["mac_text"],    default_value=f"MAC : {node.mac_str}")

        # Heartbeat age — thresholds scale with the configured heartbeat
        # interval so a healthy beat at any interval reads as normal.
        age = node.heartbeat_age_s
        expected = max(self._hb_interval_s, 0.1)
        if age is None:
            hb_str = "—"
            hb_color = _COLOR_GREY
        elif age > expected * 3:
            hb_str = f"{age:.1f}s ago"
            hb_color = _COLOR_RED
        elif age > expected * 1.5:
            hb_str = f"{age:.1f}s ago"
            hb_color = _COLOR_AMBER
        else:
            hb_str = f"{age:.1f}s ago"
            hb_color = (200, 210, 220, 255)
        dpg.configure_item(tags["hb_text"], default_value=hb_str, color=hb_color)

        # Presence
        dpg.configure_item(
            tags["presence_text"],
            default_value="●" if node.presence else "○",
            color=(100, 220, 120, 255) if node.presence else (160, 165, 175, 255),
        )

        # Sensors
        for sensor_tag, val, label in [
            (tags["pellet_text"], node.pellet, "Pellet"),
            (tags["load_position_text"], node.load_position, "Load"),
            (tags["dome_text"], node.dome_open, "Dome"),
        ]:
            sym = "●" if val else "○"
            col = (100, 220, 120, 255) if val else (160, 165, 175, 255)
            dpg.configure_item(sensor_tag, default_value=f"{label}: {sym}", color=col)

        # Status: Offline (grey) takes priority, then fault (red), then
        # Healthy (green) — "Healthy" must never imply a node we have no
        # live connection to.
        if not node.online:
            status_str = "Status: Offline"
            status_col = _COLOR_GREY
        elif node.fault_code.value != 0:
            status_str = f"Status: {node.fault_code.name} — {fault_user_message(node.fault_code)}"
            status_col = _COLOR_RED
        else:
            status_str = "Status: Healthy"
            status_col = (100, 220, 120, 255)
        dpg.configure_item(tags["fault_text"], default_value=status_str, color=status_col)

        # Dome open warning (amber, non-sticky)
        warn = "Dome open >30s" if node.dome_open_warning else ""
        dpg.configure_item(tags["warn_text"], default_value=warn, color=_COLOR_AMBER)

    def _refresh_all_tiles(self) -> None:
        if not self._registry:
            return
        for node in self._registry.all_nodes():
            self._refresh_tile(node.node_id)

    # ------------------------------------------------------------------
    # Log table refresh
    # ------------------------------------------------------------------

    def _refresh_log_table(self, *_) -> None:
        if not self._log or not dpg.does_item_exist("log_row_0"):
            return

        node_filter = dpg.get_value("log_filter_node")
        type_filter = dpg.get_value("log_filter_type")

        node_id = None if node_filter == "All" else int(node_filter)
        ftype   = None if type_filter == "All" else type_filter

        matched = self._log.get_filtered(node_id=node_id, frame_type=ftype)
        shown = matched[:LOG_VISIBLE_ROWS]

        for i in range(LOG_VISIBLE_ROWS):
            if i >= len(shown):
                dpg.configure_item(f"log_row_{i}", show=False)
                continue
            entry = shown[i]
            dir_color = (100, 220, 120, 255) if entry.direction == "RX" else (120, 160, 255, 255)
            dpg.configure_item(f"log_row_{i}", show=True)
            dpg.configure_item(f"log_time_{i}", default_value=entry.timestamp_str)
            dpg.configure_item(
                f"log_node_{i}",
                default_value=str(entry.node_id) if entry.node_id else "—",
            )
            dpg.configure_item(f"log_dir_{i}", default_value=entry.direction, color=dir_color)
            dpg.configure_item(f"log_type_{i}", default_value=entry.frame_type)
            dpg.configure_item(f"log_event_{i}", default_value=entry.event_name)
            dpg.configure_item(f"log_id_{i}", default_value=entry.raw_id_hex)
            dpg.configure_item(f"log_data_{i}", default_value=entry.raw_data_hex)
            dpg.configure_item(f"log_details_{i}", default_value=entry.details)

        path = self._log.csv_path
        where = f" · {path.name}" if path is not None else ""
        dpg.set_value(
            "log_count_text",
            f"  {len(shown)}/{len(matched)} recent · {self._log.rows_written} on disk{where}",
        )

    # ------------------------------------------------------------------
    # Heartbeat interval policy
    # ------------------------------------------------------------------


    def _read_hb_interval_input_s(self) -> int:
        if dpg.does_item_exist("hb_interval_input"):
            seconds = int(dpg.get_value("hb_interval_input"))
        else:
            seconds = int(self._hb_interval_s)
        return max(1, seconds)


    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------


    def _on_apply_heartbeat_interval(self, sender=None, app_data=None, user_data=None) -> None:
        """Broadcast a SetConfig frame so every node adopts the new heartbeat interval."""
        self._sync_heartbeat_policy(broadcast=True)

    def _on_open_presence_cal_dialog(self, sender=None, app_data=None, user_data=None) -> None:
        """
        Confirm before broadcasting a presence recalibration — required,
        because a mis-click writes a new threshold into NVS on every node,
        and calibrating with an animal present sets a threshold ABOVE real
        presence readings (that node then stops detecting the animal until
        recalibrated). The button is also disabled while an experiment runs.
        """
        if self._exp.is_running:
            return
        if dpg.does_item_exist("presence_cal_modal"):
            dpg.delete_item("presence_cal_modal")
        with dpg.window(
            tag="presence_cal_modal",
            label="Recalibrate Presence Pads",
            modal=True,
            no_resize=True,
            width=420,
            height=250,
            pos=((WINDOW_W - 420) // 2, (WINDOW_H - 250) // 2),
        ):
            dpg.add_text("Broadcast presence recalibration to ALL nodes.",
                         color=(100, 180, 255, 255))
            dpg.add_separator()
            dpg.add_text(
                "Every cage must be EMPTY and undisturbed for ~5 seconds.\n"
                "Each node samples its idle pad and stores the new threshold\n"
                "in NVS. Calibrating with an animal present sets a threshold\n"
                "ABOVE real presence readings — that node will stop detecting\n"
                "the animal until it is recalibrated again.",
                color=(230, 140, 30, 255), wrap=390,
            )
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Cage is empty — Calibrate", width=230,
                               callback=self._on_confirm_presence_cal)
                dpg.add_button(label="Cancel", width=90,
                               callback=lambda: dpg.delete_item("presence_cal_modal"))

    def _on_confirm_presence_cal(self, sender=None, app_data=None, user_data=None) -> None:
        if dpg.does_item_exist("presence_cal_modal"):
            dpg.delete_item("presence_cal_modal")
        self._broadcast(CanCmd.CalibratePresence)

    def _on_open_dev_menu(self, sender=None, app_data=None, user_data=None) -> None:
        """
        Runtime tuning for values that otherwise require a reflash or hand-
        editing an experiment's JSON. Built to make adding more rows later
        (tuning motion/timing constants) a copy of this pattern, not a
        redesign — see the plan for the tiering rationale.
        """
        if dpg.does_item_exist("dev_menu_modal"):
            dpg.delete_item("dev_menu_modal")
        with dpg.window(
            tag="dev_menu_modal",
            label="Developer Menu",
            modal=True,
            no_resize=True,
            width=420,
            height=280,
            pos=((WINDOW_W - 420) // 2, (WINDOW_H - 280) // 2),
        ):
            dpg.add_text("Presence Detection", color=(100, 180, 255, 255))
            dpg.add_text(
                f"threshold = mean + factor × std_dev  (range {PRESENCE_FACTOR_MIN:g}-{PRESENCE_FACTOR_MAX:g})",
                color=(160, 165, 175, 255), wrap=390,
            )
            with dpg.group(horizontal=True):
                dpg.add_input_float(
                    tag="dev_presence_factor_input",
                    default_value=self._dev_settings.presence_factor,
                    width=120,
                    step=0.5,
                )
                dpg.add_button(
                    label="Apply to All Nodes", width=180,
                    callback=self._on_apply_presence_factor,
                )
            dpg.add_text(
                "Note: a lower factor makes detection more sensitive.",
                color=(160, 165, 175, 255), wrap=390,
            )

    def _on_apply_presence_factor(self, sender=None, app_data=None, user_data=None) -> None:
        factor = dpg.get_value("dev_presence_factor_input") if dpg.does_item_exist("dev_presence_factor_input") else self._dev_settings.presence_factor
        factor = float(factor)
        payload = build_setconfig_presence_factor(factor)
        self._broadcast(CanCmd.SetConfig, payload)
        self._dev_settings.set_presence_factor(factor)

    def _on_assign_id(self, node_id: int) -> None:
        tags = self._node_tiles.get(node_id)
        if tags is None:
            return
        self.assign_node_id(node_id, int(dpg.get_value(tags["assign_input"])))

    # ------------------------------------------------------------------
    # Dispense scheduler (SetConfig button)
    # ------------------------------------------------------------------

    def _on_open_schedule_dialog(self, node_id: int) -> None:
        """Open a modal letting the user set a flexible dispense schedule for one node."""
        if dpg.does_item_exist("schedule_modal"):
            dpg.delete_item("schedule_modal")

        cfg = self._schedules.get(node_id, ScheduleConfig())
        mode_labels = {
            "off": "Off",
            "interval": "Every X minutes",
            "chained": "X minutes after node Y dispenses",
        }
        num_nodes = self._registry.num_nodes() if self._registry else 9
        other_nodes = [str(i) for i in range(1, num_nodes + 1) if i != node_id]
        default_y = str(cfg.chained_node_id) if str(cfg.chained_node_id) in other_nodes else (other_nodes[0] if other_nodes else "")

        with dpg.window(
            tag="schedule_modal",
            label=f"SetConfig — Node {node_id} Dispense Schedule",
            modal=True,
            no_resize=True,
            width=360,
            height=260,
            pos=((WINDOW_W - 360) // 2, (WINDOW_H - 260) // 2),
        ):
            dpg.add_text(f"Dispense schedule for Node {node_id}", color=(100, 180, 255, 255))
            dpg.add_separator()
            dpg.add_radio_button(
                tag="schedule_mode_radio",
                items=list(mode_labels.values()),
                default_value=mode_labels[cfg.mode],
            )
            dpg.add_spacer(height=6)
            dpg.add_input_float(
                tag="schedule_minutes_input",
                label="Minutes",
                default_value=(cfg.interval_minutes if cfg.mode == "interval" else cfg.chained_delay_minutes),
                width=120,
                min_value=0.1,
                max_value=1440.0,
                min_clamped=True,
                max_clamped=True,
            )
            dpg.add_combo(
                tag="schedule_nodeY_combo",
                label="After node",
                items=other_nodes,
                default_value=default_y,
                width=120,
            )
            dpg.add_spacer(height=10)
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Apply", width=90, user_data=node_id,
                    callback=lambda s, a, u: self._on_apply_schedule(u),
                )
                dpg.add_button(
                    label="Cancel", width=90,
                    callback=lambda: dpg.delete_item("schedule_modal"),
                )

    def _on_apply_schedule(self, node_id: int) -> None:
        if not dpg.does_item_exist("schedule_mode_radio"):
            return
        mode_label = dpg.get_value("schedule_mode_radio")
        minutes = float(dpg.get_value("schedule_minutes_input"))
        node_y_str = dpg.get_value("schedule_nodeY_combo")

        cfg = self._schedules.get(node_id, ScheduleConfig())
        if mode_label == "Every X minutes":
            cfg.mode = "interval"
            cfg.interval_minutes = max(0.1, minutes)
            cfg.next_fire_time = time.time() + cfg.interval_minutes * 60.0
            cfg.armed_fire_time = None
        elif mode_label == "X minutes after node Y dispenses":
            cfg.mode = "chained"
            cfg.chained_delay_minutes = max(0.1, minutes)
            cfg.chained_node_id = int(node_y_str) if node_y_str else cfg.chained_node_id
            cfg.next_fire_time = None
            cfg.armed_fire_time = None
        else:
            cfg.mode = "off"
            cfg.next_fire_time = None
            cfg.armed_fire_time = None

        self._schedules[node_id] = cfg
        self._refresh_schedule_text(node_id)

        if self._log:
            self._log.add(LogEntry(
                timestamp=time.time(),
                direction="TX",
                node_id=node_id,
                frame_type="COMMAND",
                event_name="SetConfig (schedule)",
                raw_id=0,
                raw_data=b"",
                details=cfg.summary,
            ))

        dpg.delete_item("schedule_modal")

    def _refresh_schedule_text(self, node_id: int, now: Optional[float] = None) -> None:
        tags = self._node_tiles.get(node_id)
        cfg = self._schedules.get(node_id)
        if tags is None or "schedule_text" not in tags:
            return
        if cfg is None or cfg.mode == "off":
            dpg.configure_item(
                tags["schedule_text"],
                default_value="Schedule: Off",
                color=(160, 165, 175, 255),
            )
            return

        due = cfg.due_time
        remaining = (due - (now if now is not None else time.time())) if due is not None else None
        # Amber in the final half-minute so the ticking clock is hard to miss.
        if remaining is not None and 0 <= remaining <= 30.0:
            color = _COLOR_AMBER
        else:
            color = (100, 200, 100, 255)

        dpg.configure_item(
            tags["schedule_text"],
            default_value=cfg.display_line(now),
            color=color,
        )


    # ------------------------------------------------------------------
    # BNC / Sync I/O
    # ------------------------------------------------------------------

    def _ui_bnc_edge(self, which: str, edge: str, ts: float) -> None:
        tile = self._bnc_tiles.get(f"bnc_{which.lower()}")
        if tile is not None:
            tile["last_edge_ts"] = ts
            dpg.configure_item(tile["dot"], color=_COLOR_GREEN)
            dpg.set_value(
                tile["last_text"],
                f"Last: {edge} @ {time.strftime('%H:%M:%S', time.localtime(ts))}",
            )

    def _ui_bnc_out_pulsed(self) -> None:
        tile = self._bnc_tiles.get("bnc_out")
        if tile is not None:
            tile["last_pulse_ts"] = time.time()
            dpg.configure_item(tile["dot"], color=_COLOR_GREEN)


    def _on_bnc_manual_pulse(self) -> None:
        """'Manual Pulse' button — fires BNC OUT once for bench testing, regardless of Enabled."""
        self.manual_bnc_pulse()

    def _refresh_bnc_dots(self) -> None:
        """Update BNC IN/OUT indicator dots to reflect actual signal state (green=HIGH, grey=LOW)."""
        if not self._io:
            return
        # BNC IN 0
        tile = self._bnc_tiles.get("bnc_in0")
        if tile is not None:
            color = _COLOR_GREEN if self._io.read_bnc_in1() else _COLOR_GREY
            dpg.configure_item(tile["dot"], color=color)
        # BNC IN 1
        tile = self._bnc_tiles.get("bnc_in1")
        if tile is not None:
            color = _COLOR_GREEN if self._io.read_bnc_in2() else _COLOR_GREY
            dpg.configure_item(tile["dot"], color=color)
        # BNC OUT
        tile = self._bnc_tiles.get("bnc_out")
        if tile is not None:
            color = _COLOR_GREEN if self._io.read_bnc_out() else _COLOR_GREY
            dpg.configure_item(tile["dot"], color=color)

    # ------------------------------------------------------------------
    # Log actions
    # ------------------------------------------------------------------

    def _on_log_clear(self) -> None:
        if self._log:
            self._log.clear()
        self._last_display_generation = self._log.display_generation if self._log else -1
        self._refresh_log_table()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(args: argparse.Namespace) -> None:
    app = SFMApp(args)
    app.run()
