"""tables.py — tidy list[dict] tables for custom analysis.

Every function here flattens one kind of already-computed data (cycles,
bouts, raw events, trial markers) into a list of plain dicts, one dict
per row, every row carrying the same keys. Feed the result straight to
``pd.DataFrame(...)`` if you have pandas, or iterate it directly.

Deliberately not a query DSL: filter/group/join in pandas (or plain
Python) once you have the table, not through a bespoke API here. See
``to_dataframe()`` for the pandas handoff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from ..report.session import RunData
    from .session import Session


def _runs_and_metrics(session: "Session", run_id: Optional[int]) -> List[Tuple["RunData", Any]]:
    if run_id is not None:
        return [(session.run(run_id), session.metrics_for(run_id))]
    return list(zip(session.runs, session.metrics))


def cycles_table(session: "Session", run_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    One row per reconstructed dispense cycle (``metrics.Cycle``), across
    every run in ``session`` unless ``run_id`` is given.

    Example::

        rows = s.cycles_table()
        latencies = [r["retrieval_latency"] for r in rows if r["retrieval_latency"] is not None]
    """
    out: List[Dict[str, Any]] = []
    for run, m in _runs_and_metrics(session, run_id):
        for c in m.cycles:
            out.append({
                "session": session.name,
                "run_id": run.run_id,
                "node": c.node,
                "trial": c.trial,
                "index": c.index,
                "fed": c.fed,
                "cmd_t": c.cmd_t,
                "lowering_t": c.lowering_t,
                "loading_t": c.loading_t,
                "on_plate_t": c.on_plate_t,
                "raising_t": c.raising_t,
                "seeking_t": c.seeking_t,
                "dwelling_t": c.dwelling_t,
                "ready_t": c.ready_t,
                "first_presence_t": c.first_presence_t,
                "first_dome_t": c.first_dome_t,
                "taken_t": c.taken_t,
                "end_t": c.end_t,
                "censored": c.censored,
                "cycle_duration": c.cycle_duration,
                "approach_latency": c.approach_latency,
                "dome_latency": c.dome_latency,
                "handling_time": c.handling_time,
                "retrieval_latency": c.retrieval_latency,
                "approached_without_dome": c.approached_without_dome,
                "dome_without_take": c.dome_without_take,
            })
    return out


def bouts_table(session: "Session", run_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    One row per presence bout, dome-open bout, and fault interval,
    unified under a ``kind`` column (``"presence"`` | ``"dome"`` |
    ``"fault"``). A ``censored`` row was still open when the run ended —
    its ``t1``/``dur`` are ``None``, not zero; filter deliberately (see
    the "five things to know" section of the README) rather than letting
    a censored bout silently become a zero-duration one in an average.
    """
    out: List[Dict[str, Any]] = []
    for run, m in _runs_and_metrics(session, run_id):
        presence_bouts, _ = m.presence
        dome_bouts, _ = m.dome
        for kind, bouts in (("presence", presence_bouts), ("dome", dome_bouts)):
            for b in bouts:
                out.append({
                    "session": session.name,
                    "run_id": run.run_id,
                    "kind": kind,
                    "node": b.node,
                    "t0": b.t0,
                    "t1": b.t1,
                    "dur": b.dur,
                    "censored": b.censored,
                    "code": None,
                    "inferred_close": None,
                })
        for f in m.faults:
            out.append({
                "session": session.name,
                "run_id": run.run_id,
                "kind": "fault",
                "node": f.node,
                "t0": f.t0,
                "t1": f.t1,
                "dur": f.dur,
                "censored": f.censored,
                "code": f.code.name,
                "inferred_close": f.inferred_close,
            })
    return out


def events_table(
    session: "Session",
    run_id: Optional[int] = None,
    *,
    source: Optional[str] = None,
    frame_type: Optional[str] = None,
    event_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    One row per raw log row (``LogRow``), optionally filtered by
    ``source`` (``"CAN"``/``"EXP"``/``"BNC"``/``"SYS"``), ``frame_type``,
    or ``event_name``.

    This is the escape hatch: every row sfm_analysis parses is here, with
    its ``fields_json`` already decoded into a ``fields`` dict. Prefer
    ``cycles_table()`` / ``bouts_table()`` for anything they already
    cover — this is for the question those don't answer.

    Watch the "Dome Opened" trap if you filter on it directly:
    ``event_name="Dome Opened"`` matches two different wire events (a
    milestone and a renamed edge — see the README) and double-counts
    every physical opening. ``bouts_table(kind="dome")`` already does
    the dedup; use that instead of filtering this table by name.
    """
    out: List[Dict[str, Any]] = []
    for run, _m in _runs_and_metrics(session, run_id):
        for row in run.rows:
            if source is not None and row.source != source:
                continue
            if frame_type is not None and row.frame_type != frame_type:
                continue
            if event_name is not None and row.event_name != event_name:
                continue
            out.append({
                "session": session.name,
                "run_id": run.run_id,
                "t": row.t,
                "iso": row.iso,
                "node_id": row.node_id,
                "trial": row.trial,
                "source": row.source,
                "direction": row.direction,
                "frame_type": row.frame_type,
                "event_name": row.event_name,
                "fields": row.fields,
                "details": row.details,
                "post_session": row.post_session,
            })
    return out


def trials_table(session: "Session", run_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    One row per generic ``trial`` marker row (EXPERIMENT source,
    ``event_name="trial"``) — the trial-boundary event every experiment
    template logs, regardless of what happens inside the trial. For
    template-specific trial structure (choice, reward, block), see the
    corresponding ``sfm_analysis.report.analyses`` module instead (e.g.
    ``analyses.bandit.build_trials`` for ``two_armed_bandit``).
    """
    out: List[Dict[str, Any]] = []
    for run, _m in _runs_and_metrics(session, run_id):
        for row in run.exp("trial"):
            out.append({
                "session": session.name,
                "run_id": run.run_id,
                "t": row.t,
                "trial": row.trial,
                "fields": row.fields,
            })
    return out


def to_dataframe(rows: Sequence[Dict[str, Any]]):
    """
    Convert a tidy table (a ``list[dict]``, e.g. from ``cycles_table()``)
    into a pandas ``DataFrame``. Requires pandas to be installed
    (``pip install sfm-analysis[pandas]``); raises a clear ``ImportError``
    naming that extra if it isn't, rather than a bare
    ``ModuleNotFoundError`` from deep inside pandas.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError(
            "to_dataframe() needs pandas -- install it with "
            "`pip install sfm-analysis[pandas]`"
        ) from exc
    return pd.DataFrame(list(rows))
