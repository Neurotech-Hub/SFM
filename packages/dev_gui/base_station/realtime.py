"""
realtime.py — optional real-time priority for the threads that time the BNC lines.

Competing programs can hold the base station's timing threads off the CPU for a few
milliseconds, and ordinary Linux scheduling can't promise otherwise. A thread with a
real-time priority runs ahead of everything ordinary, so a pulse is not held up by a browser
or by the desktop. Measured on the Pi: with it the slowest of 1000 pulses under heavy load
was about 130 us late, without it several milliseconds.

It is off unless asked for, with ``SFM_RT_PRIORITY=<1-99>`` (the engine's ``--realtime``
option sets it). The timing threads (CAN receive, BNC pulse, BNC edges) get that priority.
The engine's other Python threads get ``OTHERS_BELOW`` less, set once on the main thread at
start-up, which every thread created later inherits. That matters: all of them share one
interpreter lock, and a timing thread waiting for the lock behind an ordinary-priority thread
that a busy program keeps pre-empting is held up just the same (priority inversion).
Measured: without it, one second of heavy load delayed pulses by 50 ms.

It needs no root. The user's real-time limit must allow it (``LimitRTPRIO=`` in a system
service, or an rtprio line in /etc/security/limits.d). If the system refuses, the engine runs
at normal priority and ``status()`` says so; nothing fails.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict

log = logging.getLogger("sfm.realtime")

ENV = "SFM_RT_PRIORITY"

_lock = threading.Lock()
_threads: Dict[str, str] = {}      # thread label -> what happened ("FIFO 50" or the reason it was refused)


def requested_priority() -> int:
    """The priority asked for (1-99), or 0 for "off"."""
    try:
        value = int(os.environ.get(ENV, "0") or 0)
    except ValueError:
        return 0
    return value if 1 <= value <= 99 else 0


OTHERS_BELOW = 10


def elevate_current_thread(label: str, below: int = 0) -> bool:
    """
    Give the calling thread the requested real-time priority, less ``below``. Returns True if it
    now has it. Does nothing, and returns False, when no priority was asked for.
    """
    priority = requested_priority()
    if not priority:
        return False
    priority = max(1, priority - below)
    try:
        # The thread's own id, not 0 and not the process: only this thread is raised.
        os.sched_setscheduler(threading.get_native_id(), os.SCHED_FIFO, os.sched_param(priority))
    except (PermissionError, OSError, AttributeError) as exc:
        reason = ("refused: the real-time limit is too low (see LimitRTPRIO)" if isinstance(exc, PermissionError)
                  else f"refused: {exc}")
        with _lock:
            _threads[label] = reason
        log.warning("real-time priority for %s: %s", label, reason)
        return False
    with _lock:
        _threads[label] = f"FIFO {priority}"
    log.info("real-time priority for %s: FIFO %d", label, priority)
    return True


def status() -> Dict[str, object]:
    """What was asked for and what each timing thread got, for the state shown to the operator."""
    with _lock:
        return {"requested": requested_priority(), "threads": dict(_threads)}


def reset_for_tests() -> None:
    with _lock:
        _threads.clear()
