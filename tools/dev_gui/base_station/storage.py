"""storage.py — Where session logs live.

Resolves the log directory the base station writes to: an external drive
mounted under ``/media/<user>/*`` if one is available, else ``~/sfm_logs``.

This lives outside ``app.py`` so headless tools (``run_report.py``) can find
the logs without importing dearpygui. ``app.py`` re-exports both functions
under their historical private names, so GUI code is unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple


def default_log_dir() -> str:
    """
    Get default log directory: external storage if available, else fallback to ~/sfm_logs.

    Scans /media/<user>/* for any mounted external drive (USB/SD card readers
    mount with a dynamic volume-label path, e.g. /media/base-station-00/9016-4EF8),
    plus a few common fixed mount points. First writable candidate wins.
    """
    candidates: List[str] = []

    # Scan /media/<user>/* — udisks2-style auto-mounts use the volume label
    # as the directory name, which varies per drive/format, so we glob it.
    media_root = Path("/media") / os.environ.get("USER", "base-station-00")
    if media_root.is_dir():
        try:
            for entry in sorted(media_root.iterdir()):
                if entry.is_dir():
                    candidates.append(str(entry / "sfm_logs"))
        except OSError:
            pass

    # Common fixed mount points as additional fallbacks.
    candidates += [
        "/mnt/external/sfm_logs",
        "/mnt/usb/sfm_logs",
        "/media/sfm_logs",
    ]

    for path_str in candidates:
        path = Path(path_str)
        parent = path.parent
        if not parent.exists() or not parent.is_dir():
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".write_test"
            test_file.touch()
            test_file.unlink()
            return path_str
        except (OSError, PermissionError):
            continue

    return str(Path("~/sfm_logs").expanduser())


def validate_log_dir(log_dir_str: str) -> Tuple[bool, str]:
    """
    Validate that a log directory is available, accessible, and writable.

    Returns: (is_valid, error_message)
    """
    if not log_dir_str or not log_dir_str.strip():
        return False, "Log directory path cannot be empty."

    log_dir = Path(log_dir_str).expanduser()

    # Check if path exists
    if not log_dir.exists():
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as e:
            return False, f"Cannot create log directory: {e}"

    # Check if it's a directory
    if not log_dir.is_dir():
        return False, f"Log path exists but is not a directory: {log_dir}"

    # Check write permissions
    try:
        test_file = log_dir / ".write_test"
        test_file.touch()
        test_file.unlink()
    except (OSError, PermissionError) as e:
        return False, f"Log directory is not writable: {e}"

    return True, ""
