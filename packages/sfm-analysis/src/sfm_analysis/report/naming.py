"""naming.py — parse subject/cohort/day identity out of a session name.

There is no subject/animal field anywhere in the log schema (see
sfm_analysis.logs.CSV_HEADER) — the only identifier a session carries is
its operator-typed name. This module recovers subject/cohort/day from
that name via a configurable, ordered list of regex patterns, persisted
the same way the VFM base station's dev_settings.py persists
DevSettings: versioned JSON under ~/.sfm/, atomic write, defaults on
corruption.

Degradation is always explicit. A name that matches nothing returns a
SessionIdentity with every field but ``session`` set to None — callers
must handle that (see report/sections/generic.py's provenance section and
compare.py's subject/learning sections) rather than assume a subject
always exists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_SETTINGS_PATH = Path("~/.sfm/report_settings.json").expanduser()

# Ordered: first pattern that matches wins. Each must define at least one of
# the named groups (cohort, subject, day, date) via `(?P<name>...)`.
DEFAULT_PATTERNS: List[str] = [
    # cohortA_M014_d3 / cohortA-M014-day3
    r"^(?P<cohort>[A-Za-z][A-Za-z0-9]*)[_-](?P<subject>[A-Za-z]*\d+)[_-]d(?:ay)?(?P<day>\d+)$",
    # M014_d3
    r"^(?P<subject>[A-Za-z]*\d+)[_-]d(?:ay)?(?P<day>\d+)$",
    # anything_M014_anything  (subject only)
    r"^.*?[_-](?P<subject>[A-Za-z]{1,3}-?\d{2,4})(?:[_-].*)?$",
    # session_20260812  (daily activity sink)
    r"^session_(?P<date>\d{8})$",
    # session_20260812_150810  (legacy per-process auto-named sink)
    r"^session_(?P<date>\d{8})_\d{6}$",
]


@dataclass
class SessionIdentity:
    """Parsed identity for one session name; unmatched fields stay None."""

    session: str
    subject: Optional[str] = None
    cohort: Optional[str] = None
    day: Optional[int] = None
    date: Optional[str] = None
    matched_pattern: Optional[str] = None   # None => name did not match any pattern

    @property
    def parsed(self) -> bool:
        return self.matched_pattern is not None

    @property
    def display_label(self) -> str:
        """Best short label for a table cell: subject, else cohort, else the raw name."""
        if self.subject:
            return self.subject
        if self.cohort:
            return self.cohort
        return self.session


@dataclass
class NamingSettings:
    """Persisted, versioned pattern list — mirrors DevSettings' shape."""

    version: int = 1
    patterns: List[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS))

    @classmethod
    def load(cls, path: Path = DEFAULT_SETTINGS_PATH) -> "NamingSettings":
        """Load settings, or defaults if the file is missing/corrupt."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            patterns = data.get("patterns")
            if isinstance(patterns, list) and all(isinstance(p, str) for p in patterns):
                return cls(version=int(data.get("version", 1)), patterns=patterns)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return cls()

    def save(self, path: Path = DEFAULT_SETTINGS_PATH) -> None:
        """Atomic write: write to a temp file in the same directory, then replace."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"version": self.version, "patterns": self.patterns}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


def parse_session_name(name: str, patterns: Optional[List[str]] = None) -> SessionIdentity:
    """
    Try each pattern in order against ``name``; the first match wins.

    An unmatched name is not an error — it returns an identity with
    ``session=name`` and everything else None, and callers show the raw
    name plus a "no subject parsed" note rather than fail.
    """
    patterns = patterns if patterns is not None else DEFAULT_PATTERNS
    for pattern in patterns:
        m = re.match(pattern, name)
        if not m:
            continue
        groups = m.groupdict()
        day_raw = groups.get("day")
        return SessionIdentity(
            session=name,
            subject=groups.get("subject"),
            cohort=groups.get("cohort"),
            day=int(day_raw) if day_raw is not None else None,
            date=groups.get("date"),
            matched_pattern=pattern,
        )
    return SessionIdentity(session=name)


def group_by(identities: List[SessionIdentity], key: str) -> Dict[str, List[SessionIdentity]]:
    """
    Group identities by an attribute (``subject`` or ``cohort``).

    Identities where that attribute is None are collected under the empty
    string key rather than dropped, so an unparsed session is still visible
    in a combined-report grouping instead of silently vanishing.
    """
    out: Dict[str, List[SessionIdentity]] = {}
    for ident in identities:
        value = getattr(ident, key, None) or ""
        out.setdefault(value, []).append(ident)
    return out
