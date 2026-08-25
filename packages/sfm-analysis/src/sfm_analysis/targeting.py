"""targeting.py — turn a session name / glob / path into concrete CSV files.

Shared by the ``sfm-report`` CLI (``cli/report.py``) and the analysis API
(``analysis.load_session``), so a session can be pointed at the same way
in both places: an exact session name, a shell glob over session names, or
an explicit path to a ``.csv`` file.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import List, Sequence

from .report.loader import discover_sessions


def resolve_targets(targets: Sequence[str], log_dir: Path) -> List[Path]:
    """
    Turn targets (session names, globs, or paths) into concrete CSV paths.

    Resolution per target:
      1. an explicit path that exists → used as-is
      2. an exact session name found in ``log_dir`` → used
      3. otherwise treated as a shell glob over session names in log_dir

    Preserves target order, and never returns the same path twice even if
    it's matched by more than one target.
    """
    refs = discover_sessions(log_dir)
    by_name = {r.session: r.path for r in refs}
    resolved: List[Path] = []
    seen = set()

    for target in targets:
        path = Path(target)
        if path.suffix == ".csv" and path.exists():
            if path not in seen:
                resolved.append(path)
                seen.add(path)
            continue
        direct = by_name.get(target)
        if direct is not None:
            if direct not in seen:
                resolved.append(direct)
                seen.add(direct)
            continue
        matched = [p for name, p in by_name.items() if fnmatch.fnmatch(name, target)]
        for p in sorted(matched):
            if p not in seen:
                resolved.append(p)
                seen.add(p)

    return resolved
