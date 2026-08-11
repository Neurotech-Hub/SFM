"""
dev_settings.py — Persistent Developer Menu settings for the base station GUI.

Values a developer tunes at runtime (presence factor, no-feed dwell default)
that should survive a GUI restart instead of resetting to hardcoded defaults
every launch.

File format (JSON)::

    {
      "version": 1,
      "presence_factor": 3.0,
      "no_feed_dwell_s": 6.0
    }

Default path: ``~/.sfm/dev_settings.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SETTINGS_PATH = Path("~/.sfm/dev_settings.json")

# Mirrors firmware's kDefaultPresenceFactor (PresenceService.h) and
# kDefaultNoFeedDwellMs (ServiceTypes.h / ExperimentControl.default_no_feed_dwell_s).
DEFAULT_PRESENCE_FACTOR = 3.0
DEFAULT_NO_FEED_DWELL_S = 6.0


class DevSettings:
    """Small JSON-backed store for Developer Menu values."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path or DEFAULT_SETTINGS_PATH).expanduser().resolve()
        self.presence_factor: float = DEFAULT_PRESENCE_FACTOR
        self.no_feed_dwell_s: float = DEFAULT_NO_FEED_DWELL_S
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        """Load settings from disk. Missing / corrupt file -> defaults."""
        self.presence_factor = DEFAULT_PRESENCE_FACTOR
        self.no_feed_dwell_s = DEFAULT_NO_FEED_DWELL_S
        if not self._path.is_file():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            factor = data.get("presence_factor")
            if isinstance(factor, (int, float)):
                self.presence_factor = float(factor)
            dwell = data.get("no_feed_dwell_s")
            if isinstance(dwell, (int, float)):
                self.no_feed_dwell_s = float(dwell)
        except (OSError, json.JSONDecodeError, TypeError):
            self.presence_factor = DEFAULT_PRESENCE_FACTOR
            self.no_feed_dwell_s = DEFAULT_NO_FEED_DWELL_S

    def save(self) -> None:
        """Write the current settings to disk (atomic replace)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {
            "version": 1,
            "presence_factor": self.presence_factor,
            "no_feed_dwell_s": self.no_feed_dwell_s,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        tmp.replace(self._path)

    def set_presence_factor(self, factor: float) -> None:
        """Update + persist the presence factor immediately."""
        self.presence_factor = float(factor)
        self.save()

    def set_no_feed_dwell_s(self, dwell_s: float) -> None:
        """Update + persist the no-feed dwell default immediately."""
        self.no_feed_dwell_s = float(dwell_s)
        self.save()
