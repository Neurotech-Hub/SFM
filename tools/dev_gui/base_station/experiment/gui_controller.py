"""
gui_controller.py — Hosts ExperimentRunner inside the DearPyGui app.

The GUI owns CAN polling and BNC edge callbacks. This controller builds an
experiment from a JSON schema + params, steps it with already-drained CAN
messages, and mirrors experiment log rows into the GUI LogManager.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

from ..log_manager import LogEntry, LogManager
from ..protocol import CAN_CMD_PURPOSE, CanCmd
from .context import ExperimentLogEntry
from .runner import ExperimentRunner
from .schema import ExperimentDef, build_experiment

if TYPE_CHECKING:
    from ..can_manager import CanManager
    from ..io_manager import IOManager


class ExperimentController:
    """GUI-side host for one experiment session at a time."""

    def __init__(self) -> None:
        self._runner: Optional[ExperimentRunner] = None
        self._exp_def: Optional[ExperimentDef] = None
        self._gui_log: Optional[LogManager] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._runner is not None and self._runner.is_active

    @property
    def is_running(self) -> bool:
        """True while a session has been started and not yet finished."""
        r = self._runner
        return r is not None and r._started and not r.is_finished

    @property
    def runner(self) -> Optional[ExperimentRunner]:
        return self._runner

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        exp_def: ExperimentDef,
        params: Dict[str, Any],
        nodes: Sequence[int],
        can: Optional["CanManager"] = None,
        io: Optional["IOManager"] = None,
        log: Optional[LogManager] = None,
        log_dir: Optional[str] = None,
        on_session_start: Optional[Callable[[], None]] = None,
    ) -> bool:
        """
        Build and start an experiment. Returns False if one is already running.
        Does not open CAN — the GUI session already owns the bus.

        ``on_session_start``, if given, fires once at true session activation
        (immediately if the template has no start_when, later otherwise) —
        e.g. to fire the camera sync flash at the real start moment.
        """
        if self.is_running:
            return False

        exp = build_experiment(exp_def, params=params, nodes=nodes)
        runner = exp.make_runner(
            can=can,
            io=io,
            # The GUI path logs everything through the unified LogManager
            # (see _on_experiment_log below); passing log_dir=None here
            # keeps ExperimentControl's own CSV writer a no-op so nothing is
            # written twice.
            log_dir=None,
            wire_bnc=False,
        )
        self._gui_log = log
        runner.ctx.on_log = self._on_experiment_log
        runner.ctx.on_session_start = on_session_start
        self._runner = runner
        self._exp_def = exp_def
        runner.start()
        return True

    def stop(self) -> None:
        """End the session (fires on_end, closes experiment CSV)."""
        if self._runner is None:
            return
        self._runner.close()
        self._runner = None
        self._exp_def = None

    def step(
        self,
        messages: Optional[Sequence[Any]] = None,
        now: Optional[float] = None,
    ) -> None:
        """Forward one tick with already-drained CAN frames. No-op when idle."""
        if self._runner is None or self._runner.is_finished:
            if self._runner is not None and self._runner.is_finished:
                # Keep finished runner briefly so status_line can show reason,
                # but do not keep stepping.
                return
            return
        self._runner.step(now=now, messages=messages if messages is not None else [])

    def forward_bnc(
        self,
        which: str,
        ts: Optional[float] = None,
        high: bool = True,
    ) -> None:
        """Forward a GUI BNC IN edge into the running experiment."""
        if self._runner is None or self._runner.is_finished:
            return
        # Channel numbering is 0-based: IN1 → 0, IN2 → 1.
        channel = 0 if which.upper() in ("IN1", "BNC_IN1") else 1
        edge = "rising" if high else "falling"
        self._runner.feed_bnc_in(channel, edge, now=ts, high=high)

    def recover_node(self, node_id: int, now: Optional[float] = None) -> bool:
        """
        Recover a faulted node in the running experiment.

        Returns False when no experiment is active (the caller should fall back
        to sending a plain Recover). Clears the node's fault and re-arms it via
        the template's on_recover handler.
        """
        if self._runner is None or self._runner.is_finished:
            return False
        self._runner.recover_node(node_id, now=now)
        return True

    def status_line(self) -> str:
        """Short status for the experiment panel."""
        r = self._runner
        if r is None:
            return "Idle"
        if r.is_finished:
            reason = r.ctx.stop_reason or "ended"
            return (
                f"Finished · pellets={r.ctx.counter('pellets')} "
                f"elapsed={r.ctx.elapsed():.1f}s · {reason}"
            )
        if r.is_active:
            name = self._exp_def.label if self._exp_def else r.experiment.name
            return (
                f"Running: {name} · pellets={r.ctx.counter('pellets')} "
                f"elapsed={r.ctx.elapsed():.1f}s"
            )
        if r._started:
            return "Waiting for start condition…"
        return "Idle"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_experiment_log(self, entry: ExperimentLogEntry) -> None:
        if self._gui_log is None:
            return
        if entry.name == "command":
            self._gui_log.add(self._command_log_entry(entry))
            return
        field_str = " ".join(f"{k}={v}" for k, v in entry.fields.items())
        self._gui_log.add(
            LogEntry(
                timestamp=entry.timestamp,
                direction="SYS",
                node_id=entry.node_id,
                frame_type="EXPERIMENT",
                event_name=entry.name,
                raw_id=0,
                raw_data=b"",
                details=field_str,
                source="EXP",
                fields=dict(entry.fields),
            )
        )

    def _command_log_entry(self, entry: ExperimentLogEntry) -> LogEntry:
        """
        Render an experiment-issued CAN command (dispense/recover/...) as a
        COMMAND-type row identical in shape to a manually-issued command, so
        it appears under the log's COMMAND filter regardless of whether a
        human clicked a button or a running experiment triggered it.
        """
        cmd_name = str(entry.fields.get("cmd", ""))
        try:
            cmd = CanCmd[cmd_name]
        except KeyError:
            cmd = None
        payload_hex = entry.fields.get("payload_hex") or ""
        payload = bytes.fromhex(payload_hex) if payload_hex else b""
        node_id = entry.node_id
        broadcast = node_id == 0
        purpose = CAN_CMD_PURPOSE.get(cmd, cmd_name) if cmd is not None else cmd_name
        exp_label = self._exp_def.label if self._exp_def else "experiment"
        return LogEntry(
            timestamp=entry.timestamp,
            direction="TX",
            node_id=node_id,
            frame_type="COMMAND",
            event_name=f"{cmd_name} (broadcast)" if broadcast else cmd_name,
            raw_id=0x100 if broadcast else (0x100 + node_id),
            raw_data=(bytes([cmd.value]) if cmd is not None else b"") + payload,
            details=f"{purpose} — {exp_label}",
            source="EXP",
            fields=dict(entry.fields),
        )
