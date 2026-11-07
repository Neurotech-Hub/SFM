"""
gui_settings.py — what the operator picked on the Connect step, remembered for next time.

Saved to ``~/.sfm/gui_settings.json``: the CAN interface, the number of nodes,
the log directory, the auto-save choice, and the last experiment used. Only
these values are kept, each checked on the way in and on the way out, so a
hand-edited or damaged file can't put something unusable on screen. A file
that can't be read is set aside as ``.bad`` and the defaults apply.

These are the form's starting values. Nothing here is ever applied to a
running session.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

DEFAULT_SETTINGS_PATH = Path("~/.sfm/gui_settings.json")

MAX_NODES = 12
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


def _interface(v: Any) -> str:
    if not isinstance(v, str) or not _NAME.match(v.strip()):
        raise ValueError("The interface name can use letters, digits, '_', '.' and '-' (32 at most).")
    return v.strip()


def _num_nodes(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_NODES:
        raise ValueError(f"The number of nodes must be a whole number from 1 to {MAX_NODES}.")
    return v


def _log_dir(v: Any) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > 512 or "\0" in v:
        raise ValueError("The log directory must be a path of up to 512 characters.")
    return v.strip()


def _auto_save(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError("Auto-save must be on or off.")
    return v


def _experiment(v: Any) -> str:
    if not isinstance(v, str) or not _NAME.match(v):
        raise ValueError("That is not an experiment name.")
    return v


FIELDS = {
    "interface": _interface,
    "num_nodes": _num_nodes,
    "log_dir": _log_dir,
    "auto_save": _auto_save,
    "last_experiment": _experiment,
}


class GuiSettings:
    """The remembered Connect-step values. Not thread-safe: use from the engine thread."""

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self._path = Path(path or DEFAULT_SETTINGS_PATH).expanduser().resolve()
        self._values: Dict[str, Any] = {}
        self.load_problem: Optional[str] = None
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        self._values = {}
        self.load_problem = None
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("unexpected contents")
        except (OSError, ValueError) as exc:
            self.load_problem = f"The saved Connect settings could not be read ({exc}); using the defaults."
            try:
                self._path.replace(self._path.with_suffix(self._path.suffix + ".bad"))
            except OSError:
                pass
            return
        for key, check in FIELDS.items():
            if key in data:
                try:
                    self._values[key] = check(data[key])
                except ValueError:
                    continue  # one bad value costs only itself

    def all(self) -> Dict[str, Any]:
        return dict(self._values)

    def update(self, values: Dict[str, Any]) -> None:
        """Check every value, then save. Nothing changes if any is invalid (ValueError) or the write fails (OSError)."""
        unknown = sorted(set(values) - set(FIELDS))
        if unknown:
            raise ValueError(f"Unknown setting: {', '.join(unknown)}.")
        checked = {key: FIELDS[key](value) for key, value in values.items()}
        previous = self._values
        self._values = {**previous, **checked}
        try:
            self._save()
        except OSError:
            self._values = previous
            raise

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, **self._values}, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self._path)
