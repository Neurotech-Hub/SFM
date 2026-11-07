"""The kiosk supervisor: it keeps the browser up and never touches anything else."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from base_station import kiosk
from base_station.kiosk import Config, Supervisor, chromium_command, tree_pss_mb

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uses /proc and process groups")

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
QUICK_EXIT = [sys.executable, "-c", "raise SystemExit(3)"]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(kiosk, "BACKOFF_START_S", 0.02)
    monkeypatch.setattr(kiosk, "BACKOFF_MAX_S", 0.08)
    monkeypatch.setattr(kiosk, "PROBE_EVERY_S", 0.02)
    monkeypatch.setattr(kiosk, "TERMINATE_GRACE_S", 2)


def start(sup):
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    return t


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def sup_for(argv, **kw):
    cfg = Config(check_s=0.05, wait_s=0.3, limit_mb=1024)
    return Supervisor(cfg, command=lambda c: argv, probe=lambda u: True, **kw)


# ---------------------------------------------------------------------------

def test_the_command_is_a_kiosk_on_its_own_profile():
    cmd = chromium_command(Config(url="http://127.0.0.1:8080/", profile="~/x", ozone="wayland", extra=["--foo"]))
    assert cmd[0] == "chromium" and "--kiosk" in cmd and "--noerrdialogs" in cmd
    assert any(a.startswith("--user-data-dir=") and not a.endswith("~/x") for a in cmd), "the profile path is expanded"
    assert "--ozone-platform=wayland" in cmd and "--foo" in cmd
    assert cmd[-1] == "http://127.0.0.1:8080/"
    assert not any(a.startswith("--ozone") for a in chromium_command(Config())), "no ozone flag unless asked"


def test_a_browser_that_exits_is_started_again():
    sup = sup_for(QUICK_EXIT)
    t = start(sup)
    assert wait_for(lambda: sup.launches >= 3), "it keeps relaunching"
    sup.stop.set(); t.join(5)
    assert not t.is_alive()


def test_a_browser_over_the_memory_limit_is_replaced():
    readings = iter([5000.0] + [1.0] * 100)
    sup = sup_for(SLEEPER, measure=lambda pid: next(readings))
    t = start(sup)
    assert wait_for(lambda: sup.restarts_for_memory >= 1 and sup.launches >= 2)
    first_pid = None
    sup.stop.set(); t.join(10)
    assert not t.is_alive()
    assert sup.restarts_for_memory == 1, "only the one over-limit reading caused a restart"


def test_the_old_browser_is_really_gone_after_a_memory_restart():
    pids = []
    real_popen = subprocess.Popen

    def spy(*a, **k):
        p = real_popen(*a, **k)
        pids.append(p.pid)
        return p
    readings = iter([5000.0] + [1.0] * 100)
    sup = Supervisor(Config(check_s=0.05, wait_s=0.3), command=lambda c: SLEEPER, popen=spy,
                     probe=lambda u: True, measure=lambda pid: next(readings))
    t = start(sup)
    assert wait_for(lambda: len(pids) >= 2)
    time.sleep(0.2)
    first = pids[0]
    gone = not os.path.exists(f"/proc/{first}") or open(f"/proc/{first}/stat").read().rsplit(")", 1)[1].split()[0] == "Z"
    assert gone, "the first browser must not be left running"
    sup.stop.set(); t.join(10)


def test_stopping_the_supervisor_stops_the_browser():
    pids = []
    real_popen = subprocess.Popen

    def spy(*a, **k):
        p = real_popen(*a, **k)
        pids.append(p.pid)
        return p
    sup = Supervisor(Config(check_s=0.05, wait_s=0.3), command=lambda c: SLEEPER, popen=spy, probe=lambda u: True)
    t = start(sup)
    assert wait_for(lambda: pids)
    sup.stop.set(); t.join(10)
    assert not t.is_alive()
    time.sleep(0.1)
    state = open(f"/proc/{pids[0]}/stat").read().rsplit(")", 1)[1].split()[0] if os.path.exists(f"/proc/{pids[0]}") else "gone"
    assert state in ("gone", "Z")


def test_it_waits_for_the_gateway_before_starting_the_browser():
    answers = iter([False, False, False, True])
    seen = []
    sup = Supervisor(Config(check_s=0.05, wait_s=5), command=lambda c: SLEEPER,
                     probe=lambda u: (seen.append(u), next(answers, True))[1])
    t = start(sup)
    assert wait_for(lambda: sup.launches == 1)
    assert len(seen) >= 4, "it asked until the gateway answered"
    sup.stop.set(); t.join(10)


def test_it_starts_the_browser_anyway_if_the_gateway_never_answers():
    sup = Supervisor(Config(check_s=0.05, wait_s=0.2), command=lambda c: SLEEPER, probe=lambda u: False)
    t = start(sup)
    assert wait_for(lambda: sup.launches == 1), "the page can wait for the gateway itself"
    sup.stop.set(); t.join(10)


class FakeProc:
    pid = 4242
    def __init__(self, code=1): self.code = code
    def wait(self, timeout=None): return self.code
    def poll(self): return self.code


def test_a_crash_loop_backs_off_and_a_long_run_resets_it():
    now = [0.0]
    waits = []

    class Ev(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return False

    sup = Supervisor(Config(check_s=0.05, wait_s=0.0), command=lambda c: ["x"], popen=lambda *a, **k: FakeProc(),
                     probe=lambda u: True, clock=lambda: now[0])
    sup.stop = Ev()
    runs = {"n": 0}
    real_run_once = sup._run_once

    def run_once():
        runs["n"] += 1
        if runs["n"] == 6:
            now[0] += 100                    # this one stayed up for a long while
        if runs["n"] >= 8:
            sup.stop.set()
        return real_run_once()
    sup._run_once = run_once
    sup.run()
    delays = [w for w in waits if w in (0.02, 0.04, 0.08)]
    # Crash loop: the start delay, then doubling up to the maximum.
    assert delays[:5] == [0.02, 0.04, 0.08, 0.08, 0.08]
    # Run 6 stayed up, so the next delay starts again from the beginning.
    assert delays[5:7] == [0.02, 0.04]


def test_memory_is_counted_for_the_whole_browser_session_and_nothing_else():
    code = ("import subprocess, sys, time;"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
            "print('up', flush=True); time.sleep(30)")
    p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "up"
        pids = list(kiosk._session_pids(p.pid))
        assert p.pid in pids and len(pids) >= 2, "the parent and its child are both in the session"
        assert tree_pss_mb(p.pid) > 5
        assert os.getpid() not in pids
        assert tree_pss_mb(2 ** 22 + 5) == 0.0, "an unrelated session counts for nothing"
    finally:
        os.killpg(p.pid, 9)
        p.wait()


def test_helpers_left_behind_by_a_crashed_browser_are_cleaned_up():
    # A "browser" that starts a helper and then dies, leaving the helper running.
    code = ("import subprocess, sys;"
            "h = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            "print(h.pid, flush=True)")
    helper_pids = []
    real_popen = subprocess.Popen

    def spy(*a, **k):
        p = real_popen(*a, stdout=subprocess.PIPE, text=True, **k)
        helper_pids.append(int(p.stdout.readline()))
        return p
    sup = Supervisor(Config(check_s=0.05, wait_s=0.3), command=lambda c: [sys.executable, "-c", code], popen=spy, probe=lambda u: True)
    t = start(sup)
    assert wait_for(lambda: len(helper_pids) >= 1)
    first = helper_pids[0]
    assert wait_for(lambda: not os.path.exists(f"/proc/{first}") or open(f"/proc/{first}/stat").read().rsplit(")", 1)[1].split()[0] == "Z"), \
        "the helper must not outlive the browser"
    sup.stop.set(); t.join(10)
