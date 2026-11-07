"""
process_lock.py — only one process may own the CAN bus and the BNC lines.

The DearPyGui app and the engine service both open SocketCAN and the GPIO
chip, and two of them at once would fight over both. Each takes this lock
before it opens anything. The operating system drops the lock when the process
ends, however it ends, so a crash never leaves it stuck.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

try:
    import fcntl
except ImportError:  # Windows: no flock. Development there never opens the hardware.
    fcntl = None  # type: ignore[assignment]

DEFAULT_LOCK_PATH = Path("~/.sfm/base_station.lock")

# Locks that are held. The operating system drops a lock when its file is
# closed, and a file is closed when its last reference goes. Keeping the
# lock object here means forgetting to store the result of ``acquire()``
# can't release the lock by accident.
_HELD: set = set()


class AlreadyRunning(RuntimeError):
    """Another base-station process holds the lock. ``holder`` is its description, if known."""

    def __init__(self, holder: str = "") -> None:
        self.holder = holder
        detail = f" ({holder})" if holder else ""
        super().__init__(
            f"Another SFM base-station process is already running{detail}. "
            "Only one can use the CAN bus and BNC lines at a time. Stop it first."
        )


class ProcessLock:
    """
    Hold with ``acquire()``, let go with ``release()``, or use ``with``.

    The file holds a line naming the holder, so the refusal can say who has it.
    """

    def __init__(self, path: Optional[Union[str, Path]] = None, name: str = "sfm") -> None:
        self._path = Path(path or DEFAULT_LOCK_PATH).expanduser()
        self._name = name
        self._file = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> "ProcessLock":
        if self._file is not None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" so a refused attempt never truncates the holder's line.
        f = open(self._path, "a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.seek(0)
                holder = f.read().strip().splitlines()[-1:] or [""]
                f.close()
                raise AlreadyRunning(holder[0]) from None
        f.seek(0)
        f.truncate()
        f.write(f"{self._name} pid={os.getpid()}\n")
        f.flush()
        self._file = f
        _HELD.add(self)
        return self

    def release(self) -> None:
        f, self._file = self._file, None
        _HELD.discard(self)
        if f is None:
            return
        try:
            f.seek(0)
            f.truncate()
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
