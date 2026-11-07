"""
node_layout.py — where the operator dragged each node on the Rig Map.

Positions are saved per node **MAC address**, not node ID. Re-discover hands
out IDs from 1 again, so an ID can land on a different board; the MAC follows
the board. Each position is a pair of fractions of the map (0 to 1, left to
right and top to bottom), so it is right at any screen size.

File format (JSON), default ``~/.sfm/node_layout.json``::

    {"version": 1, "positions": {"AA:BB:CC:DD:EE:01": {"x": 0.25, "y": 0.4}}}

A file that can't be read is set aside as ``node_layout.json.bad`` and the
layout starts empty; the engine never fails to start over it.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .mac_id_registry import parse_mac
from .protocol import format_mac

DEFAULT_LAYOUT_PATH = Path("~/.sfm/node_layout.json")


def normalize_mac(mac: Union[bytes, str]) -> str:
    """The one spelling used as a key: upper-case, colon separated. Raises ValueError for junk."""
    if isinstance(mac, (bytes, bytearray)):
        if len(mac) != 6:
            raise ValueError("a MAC address has 6 bytes")
        return format_mac(bytes(mac))
    return format_mac(parse_mac(str(mac)))


def _fraction(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{what} must be between 0 and 1")
    return value


class NodeLayout:
    """Remembered node positions. Not thread-safe: use from the engine thread."""

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self._path = Path(path or DEFAULT_LAYOUT_PATH).expanduser().resolve()
        self._positions: Dict[str, Dict[str, float]] = {}
        self.load_problem: Optional[str] = None
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        self._positions = {}
        self.load_problem = None
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("positions", {}), dict):
                raise ValueError("unexpected layout")
        except (OSError, ValueError) as exc:
            self._set_aside(str(exc))
            return
        # One bad entry costs only that entry.
        for key, pos in data.get("positions", {}).items():
            try:
                self._positions[normalize_mac(key)] = {
                    "x": _fraction(pos["x"], "x"), "y": _fraction(pos["y"], "y"),
                }
            except (ValueError, KeyError, TypeError):
                continue

    def _set_aside(self, reason: str) -> None:
        self.load_problem = f"The saved node layout could not be read ({reason}); starting with an empty layout."
        try:
            self._path.replace(self._path.with_suffix(self._path.suffix + ".bad"))
        except OSError:
            pass

    def get(self, mac: Union[bytes, str, None]) -> Optional[Dict[str, float]]:
        if not mac:
            return None
        try:
            pos = self._positions.get(normalize_mac(mac))
        except ValueError:
            return None
        return dict(pos) if pos else None

    def all(self) -> Dict[str, Dict[str, float]]:
        return {mac: dict(pos) for mac, pos in self._positions.items()}

    def set(self, mac: Union[bytes, str], x: Any, y: Any) -> None:
        """Remember a position and write it to disk. Raises ValueError for a bad MAC or position."""
        key = normalize_mac(mac)
        pos = {"x": _fraction(x, "x"), "y": _fraction(y, "y")}
        previous = self._positions.get(key)
        self._positions[key] = pos
        try:
            self._save()
        except OSError:
            if previous is None:
                del self._positions[key]
            else:
                self._positions[key] = previous
            raise

    def clear(self) -> None:
        """Forget every position (the next view auto-arranges)."""
        previous, self._positions = self._positions, {}
        try:
            self._save()
        except OSError:
            self._positions = previous
            raise

    def _save(self) -> None:
        """Write to a temporary file, then rename it over the real one, so a kill can't leave half a file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "positions": dict(sorted(self._positions.items()))}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self._path)
