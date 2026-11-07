"""One process at a time may own the CAN bus and the BNC lines."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from base_station.process_lock import AlreadyRunning, ProcessLock

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX only")


def test_second_holder_is_refused_and_told_who_has_it(tmp_path) -> None:
    path = tmp_path / "bs.lock"
    first = ProcessLock(path, name="engine-a").acquire()
    try:
        with pytest.raises(AlreadyRunning) as info:
            ProcessLock(path, name="gui-b").acquire()
        assert "engine-a" in str(info.value)
        assert "pid=" in info.value.holder
        # The refused attempt must not have erased the holder's note.
        assert "engine-a" in path.read_text()
    finally:
        first.release()


def test_lock_can_be_taken_again_after_release(tmp_path) -> None:
    path = tmp_path / "bs.lock"
    with ProcessLock(path):
        pass
    with ProcessLock(path) as lock:
        assert lock.held


def test_acquire_is_idempotent_for_the_holder(tmp_path) -> None:
    lock = ProcessLock(tmp_path / "bs.lock")
    assert lock.acquire() is lock.acquire()
    lock.release()
    lock.release()  # releasing twice is harmless


def test_a_killed_holder_does_not_leave_the_lock_stuck(tmp_path) -> None:
    path = tmp_path / "bs.lock"
    code = (
        "import sys, time\n"
        "from base_station.process_lock import ProcessLock\n"
        f"ProcessLock({str(path)!r}, name='child').acquire()\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(AlreadyRunning, match="child"):
            ProcessLock(path).acquire()
        child.kill()  # no chance to clean up
        child.wait(5)
        deadline = time.monotonic() + 2
        while True:
            try:
                ProcessLock(path).acquire().release()
                break
            except AlreadyRunning:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
    finally:
        if child.poll() is None:
            child.kill()


def test_dropping_the_lock_object_does_not_release_the_lock(tmp_path) -> None:
    path = tmp_path / "bs.lock"
    ProcessLock(path, name="forgotten").acquire()   # result not kept, on purpose
    import gc
    gc.collect()
    with pytest.raises(AlreadyRunning, match="forgotten"):
        ProcessLock(path).acquire()
    from base_station import process_lock
    for held in list(process_lock._HELD):
        held.release()


def test_the_test_suite_runs_with_a_throwaway_home(isolated_home) -> None:
    """Guards the conftest: settings written during tests must never reach the real ~/.sfm."""
    from pathlib import Path
    from base_station.mac_id_registry import MacIdRegistry
    assert Path.home() == isolated_home
    registry = MacIdRegistry()
    assert str(isolated_home) in str(registry.path)
