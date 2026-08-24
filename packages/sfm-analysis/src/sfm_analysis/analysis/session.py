"""analysis/session.py — the one-line entry point: name/glob/path -> Session.

``load_session`` is what makes analysis start with one import and one call
instead of five imports across four modules (loader, session, metrics,
logs). See the module docstring of ``sfm_analysis.analysis`` for a worked
example.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from ..report import load_runs
from ..report.metrics import RunMetrics, compute_run_metrics
from ..storage import default_log_dir
from ..targeting import resolve_targets

if TYPE_CHECKING:
    from ..report.session import RunData


class SessionNotFoundError(LookupError):
    """Raised by load_session when a target matches no session CSV."""


class AmbiguousSessionError(LookupError):
    """Raised by load_session when a target matches more than one session
    CSV — pass an exact session name or an explicit path to disambiguate,
    or use sfm_analysis.report.loader.discover_sessions directly if you
    actually want every match."""


@dataclass
class Session:
    """One session CSV, loaded and split into runs, with metrics for every
    run precomputed once. Returned by ``load_session`` — see that
    function's docstring for how to get one.

    A CSV can hold more than one *run* (reopening a session name appends
    to the file and starts a new run_id — see ``.runs``); most of what
    you want is on the run that actually happened, not an aborted
    restart, which is what ``.run()`` picks by default.
    """

    name: str
    path: Path
    runs: "List[RunData]" = field(default_factory=list)
    metrics: List[RunMetrics] = field(default_factory=list)

    def run(self, run_id: Optional[int] = None) -> "RunData":
        """
        The run to use when exactly one is needed.

        With ``run_id`` given, returns that run or raises LookupError.
        Otherwise: the only run if there's just one, else the run with
        the most rows -- a session file with a 9-row aborted restart
        followed by the real 500-row session should hand you the real
        one by default, not silently the first (usually abortive) run_id.
        Use ``.runs`` directly if you want every run, or pass
        ``run_id=...`` to every table method to be explicit.
        """
        if run_id is not None:
            for r in self.runs:
                if r.run_id == run_id:
                    return r
            available = sorted(r.run_id for r in self.runs)
            raise LookupError(f"No run_id={run_id} in {self.name!r} (available: {available})")
        if len(self.runs) == 1:
            return self.runs[0]
        return max(self.runs, key=lambda r: len(r.rows))

    def metrics_for(self, run_id: Optional[int] = None) -> RunMetrics:
        """RunMetrics for the same run ``.run(run_id)`` would return."""
        target = self.run(run_id)
        idx = self.runs.index(target)
        return self.metrics[idx]

    def cycles_table(self, run_id: Optional[int] = None) -> List[dict]:
        from .tables import cycles_table
        return cycles_table(self, run_id=run_id)

    def bouts_table(self, run_id: Optional[int] = None) -> List[dict]:
        from .tables import bouts_table
        return bouts_table(self, run_id=run_id)

    def events_table(self, run_id: Optional[int] = None, **filters) -> List[dict]:
        from .tables import events_table
        return events_table(self, run_id=run_id, **filters)

    def trials_table(self, run_id: Optional[int] = None) -> List[dict]:
        from .tables import trials_table
        return trials_table(self, run_id=run_id)


def load_session(target: str, *, log_dir: Optional[Path] = None) -> Session:
    """
    Resolve ``target`` — a session name, shell glob, or explicit path to a
    ``.csv`` — against ``log_dir`` (default: the same auto-detection
    ``sfm-report`` uses: ``$SFM_LOG_DIR``, else an external drive's
    ``sfm_logs/``, else ``~/sfm_logs``) and load it.

    Raises ``SessionNotFoundError`` if nothing matches, or
    ``AmbiguousSessionError`` if a glob matches more than one session —
    pass an exact name or path instead.

    Example::

        from sfm_analysis.analysis import load_session

        s = load_session("EXP-Test-02")
        s = load_session("cohortA_M014_d3.csv", log_dir="/data/sfm_logs")
    """
    resolved_log_dir = Path(log_dir) if log_dir is not None else Path(default_log_dir())
    matches = resolve_targets([target], resolved_log_dir)
    if not matches:
        raise SessionNotFoundError(f"No session matched {target!r} in {resolved_log_dir}")
    if len(matches) > 1:
        names = ", ".join(p.stem for p in matches)
        raise AmbiguousSessionError(
            f"{target!r} matched {len(matches)} sessions in {resolved_log_dir}: {names}"
        )
    path = matches[0]
    runs = load_runs(path)
    metrics = [compute_run_metrics(r) for r in runs]
    return Session(name=path.stem, path=path, runs=runs, metrics=metrics)
