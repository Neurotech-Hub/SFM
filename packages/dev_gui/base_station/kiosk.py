"""
kiosk.py — keep the Rig Map open on the base station's monitor.

    python -m base_station.kiosk                  # Chromium, full screen, on http://127.0.0.1:8080/

It starts Chromium in kiosk mode, waits for the gateway first, starts Chromium
again if it exits, and restarts it if the whole browser (every process of it)
grows past a memory limit. It only ever touches the browser. The engine and
the gateway keep running, so a session goes on while the browser is restarted;
the page picks the session up again when it reloads.

Memory is measured as the proportional set size (PSS) of every process in the
browser's own session, which counts shared pages once. The plain resident size
would count them once per process and overstate a multi-process browser.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional

log = logging.getLogger("sfm.kiosk")

DEFAULT_URL = "http://127.0.0.1:8080/"
DEFAULT_LIMIT_MB = 1024
STABLE_AFTER_S = 60          # a browser that stayed up this long is not crash-looping
BACKOFF_START_S = 2
BACKOFF_MAX_S = 30
TERMINATE_GRACE_S = 5
PROBE_EVERY_S = 1.0           # how often to ask whether the gateway is up


@dataclass
class Config:
    url: str = DEFAULT_URL
    chromium: str = "chromium"
    profile: str = "~/.config/sfm-kiosk"
    limit_mb: float = DEFAULT_LIMIT_MB
    check_s: float = 30.0
    wait_s: float = 120.0            # how long to wait for the gateway before trying anyway
    ozone: Optional[str] = None      # "wayland" on the Pi's labwc session; None lets Chromium choose
    extra: List[str] = field(default_factory=list)


def chromium_command(cfg: Config) -> List[str]:
    """The command line for a kiosk browser."""
    cmd = [
        cfg.chromium,
        "--kiosk",
        "--noerrdialogs",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crash-bubble",      # no "restore pages?" bar after a crash
        "--disable-pinch",
        "--overscroll-history-navigation=0",
        "--password-store=basic",
        f"--user-data-dir={Path(cfg.profile).expanduser()}",
    ]
    if cfg.ozone:
        cmd.append(f"--ozone-platform={cfg.ozone}")
    cmd += cfg.extra
    cmd.append(cfg.url)
    return cmd


def url_is_up(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _session_pids(session_id: int, proc_root: str = "/proc") -> Iterable[int]:
    for name in os.listdir(proc_root):
        if not name.isdigit():
            continue
        try:
            stat = Path(proc_root, name, "stat").read_text()
            # Fields after the ")" that ends the command name: state ppid pgrp session ...
            fields = stat.rsplit(")", 1)[1].split()
            if int(fields[3]) == session_id:
                yield int(name)
        except (OSError, ValueError, IndexError):
            continue


def _pss_kb(pid: int, proc_root: str = "/proc") -> float:
    try:
        for line in Path(proc_root, str(pid), "smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:                                           # no smaps_rollup: fall back to resident pages
        pages = int(Path(proc_root, str(pid), "statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1024
    except (OSError, ValueError, IndexError):
        return 0.0


def tree_pss_mb(session_id: int, proc_root: str = "/proc") -> float:
    """Memory of every process in the browser's session, in MB."""
    return sum(_pss_kb(pid, proc_root) for pid in _session_pids(session_id, proc_root)) / 1024


class Supervisor:
    """Runs and restarts the browser. Every outside dependency is a parameter, so it can be tested."""

    def __init__(
        self,
        cfg: Config,
        command: Optional[Callable[[Config], List[str]]] = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        measure: Callable[[int], float] = tree_pss_mb,
        probe: Callable[[str], bool] = url_is_up,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._command = command or chromium_command
        self._popen, self._measure, self._probe, self._clock = popen, measure, probe, clock
        self.stop = threading.Event()
        self.launches = 0
        self.restarts_for_memory = 0
        self.proc: Optional[subprocess.Popen] = None

    def run(self) -> None:
        backoff = BACKOFF_START_S
        while not self.stop.is_set():
            self._wait_for_gateway()
            if self.stop.is_set():
                break
            started = self._clock()
            why = self._run_once()
            if self.stop.is_set():
                break
            if self._clock() - started >= STABLE_AFTER_S:
                backoff = BACKOFF_START_S           # it ran for a while: this is not a crash loop
            delay, backoff = backoff, min(backoff * 2, BACKOFF_MAX_S)
            log.warning("browser stopped (%s); starting it again in %ss", why, delay)
            self.stop.wait(delay)
        self._terminate()

    def _wait_for_gateway(self) -> None:
        deadline = self._clock() + self.cfg.wait_s
        while not self.stop.is_set() and self._clock() < deadline:
            if self._probe(self.cfg.url):
                return
            self.stop.wait(PROBE_EVERY_S)
        if not self.stop.is_set():
            log.warning("the gateway did not answer at %s; starting the browser anyway", self.cfg.url)

    def _run_once(self) -> str:
        cmd = self._command(self.cfg)
        log.info("starting the browser: %s", " ".join(cmd))
        self.launches += 1
        self.proc = self._popen(cmd, start_new_session=True)
        pid = self.proc.pid
        while not self.stop.is_set():
            try:
                code = self.proc.wait(timeout=self.cfg.check_s)
                self._reap(pid)
                return f"exited with code {code}"
            except subprocess.TimeoutExpired:
                pass
            mb = self._measure(pid)
            log.debug("browser memory %.0f MB (limit %.0f)", mb, self.cfg.limit_mb)
            if mb > self.cfg.limit_mb:
                self.restarts_for_memory += 1
                self._terminate()
                return f"memory {mb:.0f} MB is over the {self.cfg.limit_mb:.0f} MB limit"
        return "asked to stop"

    @staticmethod
    def _reap(pgid: int) -> None:
        """The browser's main process is gone; kill whatever it left behind in its process group.

        Chromium's helper processes can outlive a crashed main process, still holding memory and the
        profile. Nothing else is in this group, because the browser was started in its own session."""
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _terminate(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        for sig, wait in ((signal.SIGTERM, TERMINATE_GRACE_S), (signal.SIGKILL, 5)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sfm-kiosk", description="Keep the Rig Map open on this monitor")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--chromium", default=shutil.which("chromium") or shutil.which("chromium-browser") or "chromium")
    p.add_argument("--profile", default="~/.config/sfm-kiosk")
    p.add_argument("--limit-mb", type=float, default=DEFAULT_LIMIT_MB, help="restart the browser above this memory (PSS, all its processes)")
    p.add_argument("--check-s", type=float, default=30.0)
    p.add_argument("--ozone", default=os.environ.get("SFM_OZONE") or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else None))
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("extra", nargs="*", help="extra Chromium flags, after --")
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    a = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sup = Supervisor(Config(url=a.url, chromium=a.chromium, profile=a.profile, limit_mb=a.limit_mb,
                            check_s=a.check_s, ozone=a.ozone, extra=a.extra))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sup.stop.set())
    sup.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
