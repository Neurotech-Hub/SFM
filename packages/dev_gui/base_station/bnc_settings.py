"""
bnc_settings.py — BNC input and output settings that survive a restart.

How the two BNC inputs are read and what BNC OUT signals is part of how a rig
is wired to other equipment, so it should not reset every time the base station
is restarted. This stores those settings. It does not store whether a session
is armed to start: arming is never remembered, on purpose.

File format (JSON)::

    {
      "version": 1,
      "inputs": {"IN1": {"enabled": true, "mode": "pulse", ...}, "IN2": {...}},
      "output": {"mapping": "node_event", "trigger": "Dome Opened", ...}
    }

Unknown keys are ignored and a value that doesn't fit is skipped (and reported),
so a file from another version still loads whatever it can.

Default path: ``~/.sfm/bnc_settings.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .io_manager import BNC_IN_MODES, BNC_OUT_MAPPINGS, MAX_PULSE_US, BNCInputConfig, BNCOutputConfig

DEFAULT_BNC_SETTINGS_PATH = Path("~/.sfm/bnc_settings.json")
VERSION = 1

_IN_FIELDS = {
    "label": str, "enabled": bool, "mode": str, "action": str, "release_action": str,
    "rising_action": str, "falling_action": str, "active_high": bool, "min_width_ms": float,
}
_OUT_FIELDS = {
    "label": str, "enabled": bool, "mapping": str, "trigger": str,
    "pulse_width_us": int, "sync_width_us": int, "periodic_interval_s": float,
}


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """
    Write ``payload`` to ``path`` so that a crash or power cut leaves either the
    old file or the new one, never half of each: write a temporary file next to
    it, push it to disk, then rename it over the target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class BncSettings:
    """Small JSON-backed store for the BNC configuration."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path or DEFAULT_BNC_SETTINGS_PATH).expanduser().resolve()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Dict[str, Any]:
        """The stored settings, or {} if there is no usable file."""
        if not self._path.is_file():
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, in1: BNCInputConfig, in2: BNCInputConfig, out: BNCOutputConfig) -> None:
        """Write the current configuration (atomic replace)."""
        payload = {
            "version": VERSION,
            "inputs": {"IN1": _dump(in1, _IN_FIELDS), "IN2": _dump(in2, _IN_FIELDS)},
            "output": _dump(out, _OUT_FIELDS),
        }
        atomic_write_json(self._path, payload)


def _dump(cfg: Any, fields: Dict[str, type]) -> Dict[str, Any]:
    return {name: getattr(cfg, name) for name in fields}


def apply_settings(
    data: Dict[str, Any], in1: BNCInputConfig, in2: BNCInputConfig, out: BNCOutputConfig
) -> List[str]:
    """
    Copy stored settings onto the live configuration. Returns a description of
    each value that was skipped because it didn't fit (wrong type, unknown mode,
    out of range); everything else is applied.
    """
    skipped: List[str] = []
    inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
    for name, cfg in (("IN1", in1), ("IN2", in2)):
        _apply(inputs.get(name), cfg, _IN_FIELDS, f"inputs.{name}", skipped)
        if cfg.mode not in BNC_IN_MODES:
            skipped.append(f"inputs.{name}.mode: unknown mode {cfg.mode!r}")
            cfg.mode = "edge"
        if not 0 < cfg.min_width_ms <= 60_000:
            skipped.append(f"inputs.{name}.min_width_ms: {cfg.min_width_ms!r} out of range")
            cfg.min_width_ms = 1.0
    _apply(data.get("output"), out, _OUT_FIELDS, "output", skipped)
    if out.mapping not in BNC_OUT_MAPPINGS:
        skipped.append(f"output.mapping: unknown mapping {out.mapping!r}")
        out.mapping = "legacy"
    for name in ("pulse_width_us", "sync_width_us"):
        if not 1 <= getattr(out, name) <= MAX_PULSE_US:
            skipped.append(f"output.{name}: {getattr(out, name)!r} out of range")
            setattr(out, name, 10_000 if name == "pulse_width_us" else 500_000)
    if out.periodic_interval_s < 0.5:
        skipped.append(f"output.periodic_interval_s: {out.periodic_interval_s!r} out of range")
        out.periodic_interval_s = 60.0
    return skipped


def _apply(values: Any, cfg: Any, fields: Dict[str, type], where: str, skipped: List[str]) -> None:
    if not isinstance(values, dict):
        return
    for name, kind in fields.items():
        if name not in values:
            continue
        value = values[name]
        if kind is float and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
        elif kind is int and isinstance(value, int) and not isinstance(value, bool):
            pass
        elif not isinstance(value, kind) or (kind in (int, float) and isinstance(value, bool)):
            skipped.append(f"{where}.{name}: expected {kind.__name__}, got {value!r}")
            continue
        setattr(cfg, name, value)
