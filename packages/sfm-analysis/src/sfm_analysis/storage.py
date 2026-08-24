"""storage.py — find VFM session logs, cross-platform, read-only.

Resolution order:

  1. ``$SFM_LOG_DIR``            — explicit override, always wins
  2. an external/removable volume containing an existing ``sfm_logs`` dir
       Linux:   /media/<user>/*, /run/media/<user>/*, /mnt/*
       macOS:   /Volumes/*
       Windows: fixed/removable drive roots (D:\\ .. Z:\\)
  3. ``~/sfm_logs``               — fallback, returned whether or not it
                                     exists

This is deliberately a *reader's* resolver: unlike the VFM base station's
own writer-side resolver (``base_station.storage.default_log_dir`` in the
parent repo, which creates directories and writes a probe file to test
writability), this function never touches the filesystem. Reading
someone's logs must not modify their disk.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator


def _candidate_roots() -> Iterator[Path]:
    if sys.platform == "darwin":
        base = Path("/Volumes")
        if base.is_dir():
            yield from base.iterdir()
        return

    if sys.platform.startswith("win"):
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{letter}:\\")
            if root.is_dir():
                yield root
        return

    # Linux and everything else: the common udisks2/systemd-mount locations.
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    for base in (Path("/media"), Path("/run/media")):
        for candidate in (base / user, base):
            if candidate.is_dir():
                try:
                    yield from candidate.iterdir()
                except OSError:
                    pass
    mnt = Path("/mnt")
    if mnt.is_dir():
        try:
            yield from mnt.iterdir()
        except OSError:
            pass


def default_log_dir() -> Path:
    """Best-guess directory to look for session logs in.

    Never creates a directory and never writes anything to disk — see the
    module docstring. If nothing is found, returns ``~/sfm_logs`` whether
    or not it exists, so callers always get a concrete path to report or
    to hand to ``discover_sessions``.
    """
    override = os.environ.get("SFM_LOG_DIR")
    if override:
        return Path(override).expanduser()

    for root in _candidate_roots():
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        candidate = root / "sfm_logs"
        if candidate.is_dir():
            return candidate

    return Path("~/sfm_logs").expanduser()
