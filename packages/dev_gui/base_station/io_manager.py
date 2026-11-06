"""
io_manager.py — Base station GPIO management (BNC I/O, button, AEO).

Owns every base-station GPIO pin *except* CAN, which is handled entirely by
the kernel SocketCAN driver (see ``deploy/`` for the MCP2515 device tree
overlay and ``can_manager.py`` for the SocketCAN wrapper). Responsibilities:

  - BNC IN 1 / BNC IN 2 : edge-triggered digital inputs, transparently
    de-inverted in software. The Schmitt-trigger inverters on the HAT
    (SN74AHCT1G14) invert the true BNC TTL level before it reaches the Pi
    GPIO, so callers of this module always see true BNC-level logic
    (``True`` == BNC signal is HIGH).
  - BNC OUT             : digital output with a hardware-precision one-shot
    pulse generator, used to mirror CAN/system events out to external
    recording systems (e.g. electrophysiology DAQs) for synchronization.
  - User button (GPIO3) : simple debounced digital input.
  - AEO (GPIO27)        : Address Enable Out — drives the daisy-chain
    discovery signal to the first node. Used by DiscoveryManager instead of
    it touching GPIO directly.

All GPIO access degrades gracefully when no GPIO backend is available (e.g.
running the GUI on a dev machine against vcan0, or on a Pi without the
optional ``gpiod``/``RPi.GPIO`` packages installed) — every public method
becomes a harmless no-op / simulated value rather than raising.

Trigger/action configuration (``BNCInputConfig`` / ``BNCOutputConfig``) is
intentionally free-form: this module does not hard-code a fixed list of
"actions" or "triggers". The GUI layer owns interpreting those strings and
deciding what to actually do (send a CAN command, log something, etc.). This
keeps the base station flexible for use cases we haven't anticipated yet.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Pin definitions (BCM numbering) — fixed by the custom CAN HAT schematic.
# ---------------------------------------------------------------------------
PIN_BNC_IN1 = 12   # BNC Input 1  — inverted Schmitt trigger
PIN_BNC_IN2 = 13   # BNC Input 2  — inverted Schmitt trigger
PIN_BNC_OUT = 6    # BNC Output   — non-inverting buffer + LED
PIN_BTN     = 3    # User button
PIN_AEO     = 27   # Address Enable Out -> first node in the daisy chain
                    # (CAN_IO_27 on the pin table; NOT GPIO17, which is a
                    # spare IO routed onto the CAN bus cable for future use)

# Debounce window for the user button (seconds).
BUTTON_DEBOUNCE_S = 0.05

# BNC OUT pulse: sleep for the bulk of the duration, then spin the final
# stretch on a monotonic clock for tight timing accuracy.
_PULSE_SPIN_MARGIN_S = 0.0002

EdgeCallback = Callable[[], None]

# Longest BNC OUT pulse accepted. A pulse holds the single pulse worker, so an
# unbounded width would block every later pulse.
MAX_PULSE_US = 60_000_000


# ---------------------------------------------------------------------------
# Timed events reported by the IOManager
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BncEdgeEvent:
    """
    One edge on a BNC input, with the time it was captured.

    ``rising`` is the *logical* BNC level after the edge: True when the BNC
    signal went HIGH. The HAT's Schmitt inverters are already undone, so a raw
    rising edge on the Pi pin arrives here as ``rising=False``.

    ``t_mono`` is a ``time.monotonic()`` reading. With libgpiod it is the
    kernel's own capture time, taken when the edge happened, not when Python
    got round to reading it. ``t_wall`` is the same instant on the wall clock.
    ``source`` says which: "kernel" (libgpiod timestamp) or "software" (the
    time the callback ran, as RPi.GPIO gives no edge time).

    ``missed`` counts edges the kernel reported skipping on this line, from
    gaps in its per-line sequence numbers. It is 0 when nothing was lost or
    when the sequence is unknown.
    """
    channel: int      # 0 = BNC IN 0 (GPIO12), 1 = BNC IN 1 (GPIO13)
    which: str        # "IN1" / "IN2": the names used in the event log
    rising: bool
    t_mono: float
    t_wall: float
    seqno: int = 0
    missed: int = 0
    source: str = "kernel"


@dataclass(frozen=True)
class PulseRecord:
    """
    What the BNC OUT worker actually did for one pulse. All times are
    ``time.monotonic()`` readings taken as the pin write returned.
    """
    purpose: str
    requested_us: int
    t_request: float
    t_rise: float
    t_fall: float
    wall_rise: float

    @property
    def width_us(self) -> float:
        """Measured time between the line going high and low."""
        return (self.t_fall - self.t_rise) * 1_000_000

    @property
    def latency_us(self) -> float:
        """Time from the request to the line going high."""
        return (self.t_rise - self.t_request) * 1_000_000


BNC_IN_MODES = ("edge", "pulse", "level", "log_only")
BNC_OUT_MAPPINGS = (
    "legacy", "off", "session_start_sync", "session_start_stop", "trial", "node_event", "periodic",
)

EdgeListener = Callable[[BncEdgeEvent], None]
PulseListener = Callable[[PulseRecord], None]

_BNC_CHANNELS = {PIN_BNC_IN1: (0, "IN1"), PIN_BNC_IN2: (1, "IN2")}


# ---------------------------------------------------------------------------
# Configurable BNC mapping — deliberately free-form placeholders.
# ---------------------------------------------------------------------------

@dataclass
class BNCInputConfig:
    """
    Describes what should happen when a BNC input edge is detected.

    Each edge has its own action so the user can, e.g., start the experiment on
    the rising edge and stop it on the falling edge. An empty action for an edge
    means "do nothing on that edge" (edges are independently optional).

    ``rising_action`` / ``falling_action`` are free-form placeholders, not a
    fixed enum — the GUI offers/interprets whatever action string it supports
    (e.g. "dispense_all", "start_experiment") and dispatches it when the
    matching edge fires. Unrecognized values are simply logged.
    """
    label: str = ""
    enabled: bool = False
    rising_action: str = ""              # action on rising edge ("" = none)
    falling_action: str = ""             # action on falling edge ("" = none)
    action_params: dict = field(default_factory=dict)

    # How the input is read. The default, "edge", is the original behaviour:
    # each edge runs its own action as soon as it arrives. The others give the
    # input a meaning of its own:
    #   "pulse"    a signal that goes active and back again. If it stayed active
    #              for at least ``min_width_ms`` it counts as one pulse and runs
    #              ``action``; a shorter one is logged as a rejected glitch.
    #   "level"    the line itself is the signal. Going active runs ``action``;
    #              going inactive runs ``release_action``.
    #   "log_only" every edge is logged and passed to a running experiment, and
    #              nothing else happens. For wiring in an external sync signal.
    mode: str = "edge"
    action: str = ""                     # pulse / level mode: what an active signal does
    release_action: str = ""             # level mode: what the line going inactive does
    active_high: bool = True             # which BNC level counts as active
    min_width_ms: float = 1.0            # pulse mode: shortest signal that counts


@dataclass
class BNCOutputConfig:
    """
    Describes what should cause the BNC output to pulse.

    Like ``BNCInputConfig``, ``trigger`` / ``trigger_params`` are free-form
    placeholders — the GUI owns matching CAN/system events against the
    configured trigger and calling ``IOManager.pulse_bnc_out()``.
    pulse_width_us is stored internally as microseconds but displayed as milliseconds in the GUI.
    """
    label: str = ""
    enabled: bool = False
    pulse_width_us: int = 10000
    trigger: str = ""                    # placeholder — GUI-defined/free text
    trigger_params: dict = field(default_factory=dict)

    # What the output signals. One thing at a time, so the other equipment
    # never has to tell different kinds of pulse apart. The default,
    # "legacy", is the original behaviour: a 500 ms pulse at every session
    # start, plus a pulse for each event matching ``trigger`` while ``enabled``.
    #   "off"                 no pulses (a manual pulse still works)
    #   "session_start_sync"  one ``sync_width_us`` pulse as a session starts
    #   "session_start_stop"  a ``pulse_width_us`` pulse at the start and a pulse
    #                         twice as wide at the end
    #   "trial"               a ``pulse_width_us`` pulse at the start of each
    #                         trial, and a double-width one at the session end
    #                         (which also closes the last trial)
    #   "node_event"          a pulse whenever a node sends the event named by
    #                         ``trigger``, from any node
    #   "periodic"            a pulse every ``periodic_interval_s`` seconds while
    #                         a session runs, for correcting clock drift later
    mapping: str = "legacy"
    sync_width_us: int = 500_000
    periodic_interval_s: float = 60.0


# ---------------------------------------------------------------------------
# IOManager
# ---------------------------------------------------------------------------

class IOManager:
    """
    Owns every base-station GPIO pin except CAN.

    Usage::

        io = IOManager()
        io.begin()
        io.on_bnc_in1_edge(lambda: print("BNC IN1 edge!"))
        io.pulse_bnc_out(100)     # 100 microsecond pulse
        io.drive_aeo(True)
        ...
        io.shutdown()
    """

    def __init__(self) -> None:
        self._gpio_backend: Optional[str] = None   # "gpiod" | "RPi.GPIO" | None
        self._gpio = None                           # RPi.GPIO module reference
        self._request = None                        # gpiod LineRequest handle

        self._bnc_in1_cb: List[EdgeCallback] = []
        self._bnc_in2_cb: List[EdgeCallback] = []
        self._button_cb: List[EdgeCallback] = []
        self._edge_listeners: List[EdgeListener] = []
        self._pulse_listeners: List[PulseListener] = []
        self._last_seqno: Dict[int, int] = {}

        self._last_button_time = 0.0
        self._bnc_out_state = False
        self._edge_thread_stop = threading.Event()

        # Single owner of the BNC OUT line: one worker thread, one pulse at a time.
        self._pulse_cv = threading.Condition()
        self._pulse_job: Optional[tuple] = None   # (duration_us, purpose, t_request)
        self._pulse_busy = False
        self._pulse_thread: Optional[threading.Thread] = None
        self._pulse_stop = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def begin(self) -> None:
        """
        Configure all pins. Tries backends in order of precision:

          1. libgpiod (v2)     — preferred: native edge events + fast writes
          2. RPi.GPIO          — fallback: widely available on Raspberry Pi OS
          3. None (simulation) — no hardware present (dev machine / CI)
        """
        if self._try_setup_gpiod():
            self._gpio_backend = "gpiod"
        elif self._try_setup_rpi_gpio():
            self._gpio_backend = "RPi.GPIO"
        else:
            self._gpio_backend = None

        # Always leave BNC OUT low at startup so the onboard LED does not
        # light spuriously before anything has been explicitly configured.
        self.set_bnc_out(False)

    def shutdown(self) -> None:
        """Release GPIO resources cleanly."""
        self._stop_pulse_worker()
        self.set_bnc_out(False)
        self._edge_thread_stop.set()

        if self._gpio_backend == "RPi.GPIO" and self._gpio is not None:
            try:
                self._gpio.cleanup([PIN_BNC_IN1, PIN_BNC_IN2, PIN_BNC_OUT, PIN_BTN, PIN_AEO])
            except Exception:
                pass
        elif self._gpio_backend == "gpiod" and self._request is not None:
            try:
                self._request.release()
            except Exception:
                pass

        self._gpio_backend = None
        self._gpio = None
        self._request = None

    @property
    def backend(self) -> Optional[str]:
        """Active GPIO backend: "gpiod", "RPi.GPIO", or None (simulation)."""
        return self._gpio_backend

    @property
    def is_available(self) -> bool:
        return self._gpio_backend is not None

    # ------------------------------------------------------------------
    # BNC Inputs — transparent de-inversion + edge callbacks
    # ------------------------------------------------------------------

    def read_bnc_in1(self) -> bool:
        """True == BNC signal is logically HIGH (Schmitt inversion already undone)."""
        return self._read_input_inverted(PIN_BNC_IN1)

    def read_bnc_in2(self) -> bool:
        """True == BNC signal is logically HIGH (Schmitt inversion already undone)."""
        return self._read_input_inverted(PIN_BNC_IN2)

    def on_bnc_in1_edge(self, callback: EdgeCallback) -> None:
        """Register a callback fired (from a background thread) on every BNC IN1 edge."""
        self._bnc_in1_cb.append(callback)

    def on_bnc_in2_edge(self, callback: EdgeCallback) -> None:
        """Register a callback fired (from a background thread) on every BNC IN2 edge."""
        self._bnc_in2_cb.append(callback)

    def on_bnc_edge(self, listener: EdgeListener) -> None:
        """
        Register a listener for every edge on either BNC input, fired from a
        background thread with a ``BncEdgeEvent``. Unlike the zero-argument
        callbacks above, the event says which way the signal went and when.
        """
        self._edge_listeners.append(listener)

    def on_bnc_pulse(self, listener: PulseListener) -> None:
        """Register a listener told, from the pulse thread, how each BNC OUT pulse went."""
        self._pulse_listeners.append(listener)

    # ------------------------------------------------------------------
    # BNC Output — hardware-precision one-shot pulse
    # ------------------------------------------------------------------

    def set_bnc_out(self, high: bool) -> None:
        """Drive the BNC output (and its onboard LED indicator) level directly."""
        self._bnc_out_state = high
        self._write_output(PIN_BNC_OUT, high)

    def read_bnc_out(self) -> bool:
        """Last commanded level of the BNC output (not read back from hardware)."""
        return self._bnc_out_state

    def pulse_bnc_out(self, duration_us: int = 100, purpose: str = "") -> bool:
        """
        Fire a single HIGH pulse of ``duration_us`` microseconds on the BNC
        output, then return it LOW.

        One worker thread owns the line, so pulses never overlap and a later
        pulse can't cut an earlier one short. If a pulse is already running or
        waiting, this one is refused and False is returned; the caller decides
        whether to log that. Otherwise True is returned straight away and the
        pulse happens in the background.

        The worker sleeps for most of the width and spins on a monotonic clock
        for the last stretch, which keeps jitter far tighter than a plain
        ``time.sleep()``. How it went is reported to ``on_bnc_pulse`` listeners
        as a ``PulseRecord``. This is userspace timing: expect tens of
        microseconds of jitter, not nanoseconds.
        """
        duration_us = max(1, min(int(duration_us), MAX_PULSE_US))
        with self._pulse_cv:
            if self._pulse_busy or self._pulse_job is not None:
                return False
            self._ensure_pulse_worker()
            self._pulse_job = (duration_us, purpose, time.monotonic())
            self._pulse_cv.notify()
        return True

    @property
    def bnc_out_busy(self) -> bool:
        """True while a pulse is running or waiting to run."""
        with self._pulse_cv:
            return self._pulse_busy or self._pulse_job is not None

    def _ensure_pulse_worker(self) -> None:
        """Start the pulse worker if it isn't running. Call with the condition held."""
        if self._pulse_thread is not None and self._pulse_thread.is_alive():
            return
        self._pulse_stop = False
        self._pulse_thread = threading.Thread(
            target=self._pulse_loop, daemon=True, name="sfm-bnc-pulse"
        )
        self._pulse_thread.start()

    def _pulse_loop(self) -> None:
        while True:
            with self._pulse_cv:
                while self._pulse_job is None and not self._pulse_stop:
                    self._pulse_cv.wait()
                if self._pulse_stop:
                    return
                duration_us, purpose, t_request = self._pulse_job
                self._pulse_job = None
                self._pulse_busy = True
            try:
                self._write_output(PIN_BNC_OUT, True)
                t_rise = time.monotonic()
                wall_rise = time.time()
                self._bnc_out_state = True
                self._precise_wait_us(duration_us)
                self._write_output(PIN_BNC_OUT, False)
                t_fall = time.monotonic()
                self._bnc_out_state = False
                record = PulseRecord(
                    purpose=purpose, requested_us=duration_us, t_request=t_request,
                    t_rise=t_rise, t_fall=t_fall, wall_rise=wall_rise,
                )
                for listener in list(self._pulse_listeners):
                    try:
                        listener(record)
                    except Exception:
                        pass
            finally:
                with self._pulse_cv:
                    self._pulse_busy = False

    def _stop_pulse_worker(self) -> None:
        with self._pulse_cv:
            self._pulse_stop = True
            self._pulse_job = None
            self._pulse_cv.notify_all()
        thread = self._pulse_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._pulse_thread = None

    # ------------------------------------------------------------------
    # User button
    # ------------------------------------------------------------------

    def read_button(self) -> bool:
        return self._read_input_raw(PIN_BTN)

    def on_button_press(self, callback: EdgeCallback) -> None:
        """Register a debounced callback fired on every button edge."""
        self._button_cb.append(callback)

    # ------------------------------------------------------------------
    # AEO — Address Enable Out (daisy-chain discovery signal)
    # ------------------------------------------------------------------

    def drive_aeo(self, high: bool) -> None:
        """Drive AEO (GPIO27) HIGH/LOW to enable the next node in the daisy chain."""
        self._write_output(PIN_AEO, high)

    # ------------------------------------------------------------------
    # Backend setup
    # ------------------------------------------------------------------

    def _try_setup_gpiod(self) -> bool:
        """
        Attempt to configure pins via libgpiod v2. Provides native edge-event
        support (no polling) for BNC IN1/2 and the button.
        """
        try:
            import gpiod
            from gpiod.line import Bias, Direction, Edge, Value
        except Exception:
            return False

        chip_path = self._find_gpiochip()
        if chip_path is None:
            return False

        try:
            request = gpiod.request_lines(
                chip_path,
                consumer="sfm-io-manager",
                config={
                    PIN_BNC_IN1: gpiod.LineSettings(
                        direction=Direction.INPUT, edge_detection=Edge.BOTH, bias=Bias.PULL_DOWN
                    ),
                    PIN_BNC_IN2: gpiod.LineSettings(
                        direction=Direction.INPUT, edge_detection=Edge.BOTH, bias=Bias.PULL_DOWN
                    ),
                    PIN_BTN: gpiod.LineSettings(
                        direction=Direction.INPUT, edge_detection=Edge.BOTH, bias=Bias.PULL_UP
                    ),
                    PIN_BNC_OUT: gpiod.LineSettings(
                        direction=Direction.OUTPUT, output_value=Value.INACTIVE
                    ),
                    PIN_AEO: gpiod.LineSettings(
                        direction=Direction.OUTPUT, output_value=Value.INACTIVE
                    ),
                },
            )
        except Exception:
            return False

        self._request = request
        self._edge_thread_stop.clear()
        threading.Thread(
            target=self._gpiod_edge_loop, args=(request,), daemon=True, name="sfm-gpiod-edges"
        ).start()
        return True

    def _try_setup_rpi_gpio(self) -> bool:
        """Fallback backend using RPi.GPIO (polling-based edge detection via its own thread)."""
        try:
            import RPi.GPIO as GPIO  # type: ignore
        except Exception:
            return False

        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(PIN_BNC_IN1, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            GPIO.setup(PIN_BNC_IN2, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            GPIO.setup(PIN_BTN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.setup(PIN_BNC_OUT, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(PIN_AEO, GPIO.OUT, initial=GPIO.LOW)

            GPIO.add_event_detect(
                PIN_BNC_IN1, GPIO.BOTH, callback=lambda ch: self._on_rpi_bnc_edge(PIN_BNC_IN1)
            )
            GPIO.add_event_detect(
                PIN_BNC_IN2, GPIO.BOTH, callback=lambda ch: self._on_rpi_bnc_edge(PIN_BNC_IN2)
            )
            GPIO.add_event_detect(PIN_BTN, GPIO.BOTH, callback=self._on_button_raw_edge)
        except Exception:
            return False

        self._gpio = GPIO
        return True

    # Chip labels of the Pi's header GPIO controller: the Pi 5's RP1 and the
    # BCM2711/2712 SoC pin controllers on earlier boards.
    _HEADER_CHIP_LABELS = ("pinctrl-rp1", "pinctrl-bcm2712", "pinctrl-bcm2711", "pinctrl-bcm2835")

    @staticmethod
    def _find_gpiochip() -> Optional[str]:
        """
        Locate the Pi's header GPIO chip device node.

        The chip numbers move between Pi models and kernel versions (the Pi 5's
        header is gpiochip4 on older kernels and gpiochip0 on newer ones), so
        the chip is picked by its label. If the labels can't be read, falls back
        to the first of the usual device nodes that exists.
        """
        try:
            import gpiod
            names = sorted(n for n in os.listdir("/dev") if n.startswith("gpiochip"))
            for name in names:
                path = f"/dev/{name}"
                try:
                    with gpiod.Chip(path) as chip:
                        label = chip.get_info().label
                except Exception:
                    continue
                if label in IOManager._HEADER_CHIP_LABELS:
                    return path
        except Exception:
            pass
        for candidate in ("/dev/gpiochip0", "/dev/gpiochip4"):
            if os.path.exists(candidate):
                return candidate
        return None

    def _gpiod_edge_loop(self, request) -> None:
        """Background thread: block on libgpiod edge events and dispatch them."""
        while not self._edge_thread_stop.is_set():
            try:
                if request.wait_edge_events(timeout=0.2):
                    for event in request.read_edge_events():
                        offset = event.line_offset
                        if offset in _BNC_CHANNELS:
                            self._on_gpiod_bnc_event(offset, event)
                        elif offset == PIN_BTN:
                            self._on_button_raw_edge(None)
            except Exception:
                time.sleep(0.2)

    def _on_gpiod_bnc_event(self, pin: int, event) -> None:
        """
        Turn one libgpiod edge event into a ``BncEdgeEvent``.

        The pin sees the inverted signal, so a raw rising edge is a logical
        falling edge. The kernel timestamp (CLOCK_MONOTONIC, the same clock as
        ``time.monotonic()`` on Linux) is used as the capture time, which keeps
        it accurate even if Python reads the event late.
        """
        kind = getattr(event.event_type, "name", str(event.event_type)).upper()
        raw_rising = "RISING" in kind
        stamp_ns = getattr(event, "timestamp_ns", None)
        t_mono = stamp_ns / 1e9 if stamp_ns else time.monotonic()
        seqno = int(getattr(event, "line_seqno", 0) or 0)
        self._emit_bnc_edge(
            pin, rising=not raw_rising, t_mono=t_mono, seqno=seqno,
            source="kernel" if stamp_ns else "software",
        )

    def _on_rpi_bnc_edge(self, pin: int) -> None:
        """
        RPi.GPIO reports that an edge happened but not which way or when, so
        the level is read the moment the callback runs. That is as close as
        this backend gets, and the event says so with ``source="software"``.
        """
        t_mono = time.monotonic()
        self._emit_bnc_edge(
            pin, rising=self._read_input_inverted(pin), t_mono=t_mono, seqno=0,
            source="software",
        )

    def _emit_bnc_edge(
        self, pin: int, rising: bool, t_mono: float, seqno: int = 0, source: str = "kernel"
    ) -> None:
        """Hand one BNC edge to every listener and to the legacy callbacks."""
        channel, which = _BNC_CHANNELS[pin]
        missed = 0
        if seqno:
            last = self._last_seqno.get(pin)
            if last is not None and seqno > last + 1:
                missed = seqno - last - 1
            self._last_seqno[pin] = seqno
        # Same offset between the two clocks as right now; exact to microseconds,
        # and still right if this thread read the event a little late.
        t_wall = time.time() - (time.monotonic() - t_mono)
        event = BncEdgeEvent(
            channel=channel, which=which, rising=rising, t_mono=t_mono, t_wall=t_wall,
            seqno=seqno, missed=missed, source=source,
        )
        for listener in list(self._edge_listeners):
            try:
                listener(event)
            except Exception:
                pass
        self._dispatch(self._bnc_in1_cb if channel == 0 else self._bnc_in2_cb)

    # ------------------------------------------------------------------
    # IO primitives (backend dispatch)
    # ------------------------------------------------------------------

    def _read_input_raw(self, pin: int) -> bool:
        """
        Raw GPIO level, no inversion applied.

        In simulation mode (no GPIO backend available), BNC IN pins default
        to raw HIGH — mirroring the Schmitt inverter's resting output when no
        BNC signal is present — so ``read_bnc_in1()``/``read_bnc_in2()`` report
        the same idle (no-signal) state as real hardware.
        """
        if self._gpio_backend == "RPi.GPIO" and self._gpio is not None:
            try:
                return bool(self._gpio.input(pin))
            except Exception:
                return False
        if self._gpio_backend == "gpiod" and self._request is not None:
            try:
                from gpiod.line import Value
                return self._request.get_value(pin) == Value.ACTIVE
            except Exception:
                return False
        return pin in (PIN_BNC_IN1, PIN_BNC_IN2)

    def _read_input_inverted(self, pin: int) -> bool:
        """
        Read a BNC input pin and undo the hardware Schmitt-trigger inversion,
        so callers always see true BNC-level logic (True == signal present).
        """
        return not self._read_input_raw(pin)

    def _write_output(self, pin: int, high: bool) -> None:
        if self._gpio_backend == "RPi.GPIO" and self._gpio is not None:
            try:
                self._gpio.output(pin, self._gpio.HIGH if high else self._gpio.LOW)
            except Exception:
                pass
        elif self._gpio_backend == "gpiod" and self._request is not None:
            try:
                from gpiod.line import Value
                self._request.set_value(pin, Value.ACTIVE if high else Value.INACTIVE)
            except Exception:
                pass
        # else: simulation mode (no hardware) — no-op, state still tracked in memory.

    @staticmethod
    def _dispatch(callbacks: List[EdgeCallback]) -> None:
        for cb in callbacks:
            try:
                cb()
            except Exception:
                pass

    def _on_button_raw_edge(self, _channel) -> None:
        now = time.monotonic()
        if (now - self._last_button_time) < BUTTON_DEBOUNCE_S:
            return
        self._last_button_time = now
        self._dispatch(self._button_cb)

    # ------------------------------------------------------------------
    # BNC OUT pulse timing
    # ------------------------------------------------------------------

    @staticmethod
    def _precise_wait_us(duration_us: int) -> None:
        """
        Busy-wait for ``duration_us`` microseconds using a monotonic clock.
        Sleeps for the bulk of the duration (yielding the CPU) then spins for
        the final ~200 us for tight timing accuracy — this is the best
        precision achievable from pure Python/userspace without a dedicated
        real-time kernel timer or hardware PWM one-shot.
        """
        target = time.perf_counter() + (duration_us / 1_000_000)
        remaining = target - time.perf_counter()
        if remaining > _PULSE_SPIN_MARGIN_S:
            time.sleep(remaining - _PULSE_SPIN_MARGIN_S)
        while time.perf_counter() < target:
            pass
