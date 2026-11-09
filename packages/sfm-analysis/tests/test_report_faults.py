"""Fault intervals, dome holds, pellet reloads, and the sections that show them."""

import json
from pathlib import Path

import pytest

from report_fixtures import (
    can_event_row, dome_hold_row, exp_row, input_changed_row, pellet_reload_row,
    retracting_row, row, write_session,
)
from sfm_analysis.protocol import (
    CanEvent, PelletReloadReason, ServiceStatus, parse_pellet_reload,
)
from sfm_analysis.report import load_runs, render_report_html
from sfm_analysis.report.loader import load_rows
from sfm_analysis.report.metrics import (
    compute_run_metrics, fault_intervals, hold_intervals, pellet_reloads,
)
from sfm_analysis.report.schema import DEFAULT_REPORTS_DIR, SectionContext, resolve_design
from sfm_analysis.report.sections.generic import (
    apparatus_health_section, data_quality_section, faults_section,
)
from sfm_analysis.report.sections.timeline import session_raster_section
from sfm_analysis.report.session import split_runs
from sfm_analysis.report.timeline_data import SPAN_LEGEND, panel_marks_spans


REPO_ROOT = Path(__file__).resolve().parents[3]
FIELD_LOG = REPO_ROOT / "102626_1.2F.csv"
EXAMPLES_DESIGNS = Path(__file__).resolve().parents[1] / "examples" / "report_design" / "designs"


def _run(tmp_path, rows, session="S"):
    path = write_session(tmp_path, rows, session=session)
    loaded, _, _ = load_rows(path)
    return split_runs(loaded, [], path)[0]


def _ctx(run):
    return SectionContext(
        runs=[run], metrics=[compute_run_metrics(run)],
        combined=False, align="relative", opts={},
    )


class TestPelletReloadParse:
    def test_wire_bytes(self):
        info = parse_pellet_reload(bytes.fromhex("14 59 00 01 01"))
        assert info is not None
        assert info.count == 0x59
        assert info.reason == PelletReloadReason.LostDuringRaise
        assert info.attempt == 1

    def test_details_text_fallback(self):
        info = parse_pellet_reload(
            b"", "warning reason=MissingAfterRetract attempt 2/3 session_pellets=114",
        )
        assert info is not None
        assert info.reason == PelletReloadReason.MissingAfterRetract
        assert info.attempt == 2

    def test_logged_row_without_raw_bytes(self, tmp_path):
        rows = [row(
            ts_ms=0, node_id=1, frame_type="EVENT", event_name="Pellet Reload",
            raw_data=b"", details="warning reason=LostDuringRaise attempt 1/3",
        )]
        reloads = pellet_reloads(_run(tmp_path, rows))
        assert len(reloads) == 1
        assert reloads[0].reason == PelletReloadReason.LostDuringRaise
        assert reloads[0].attempt == 1


class TestHoldIntervals:
    def test_closes_at_next_raising(self, tmp_path):
        rows = [
            dome_hold_row(0, 1),
            can_event_row(1500, 1, CanEvent.Raising),
        ]
        holds = hold_intervals(_run(tmp_path, rows))
        assert len(holds) == 1
        assert holds[0].censored is False
        assert abs(holds[0].dur - 1.5) < 1e-6

    def test_stays_open_when_nothing_closes_it(self, tmp_path):
        rows = [
            dome_hold_row(0, 1),
            can_event_row(2000, 1, CanEvent.DomeOpened, bytes([1, 0, 1])),
        ]
        holds = hold_intervals(_run(tmp_path, rows))
        assert len(holds) == 1
        assert holds[0].censored is True
        assert holds[0].dur is not None and holds[0].dur > 1.0


class TestClearedBy:
    def test_recover_command(self, tmp_path):
        rows = [
            can_event_row(0, 1, CanEvent.Fault, bytes([ServiceStatus.Jam.value])),
            row(ts_ms=500, node_id=1, frame_type="COMMAND", source="CAN",
                direction="TX", event_name="Recover"),
            exp_row(500, "recovered", {"node": 1}, node=1),
        ]
        faults = fault_intervals(_run(tmp_path, rows))
        assert len(faults) == 1
        assert faults[0].cleared_by == "Recover command"

    def test_recovery_without_a_command(self, tmp_path):
        rows = [
            can_event_row(0, 1, CanEvent.Fault, bytes([ServiceStatus.Jam.value])),
            exp_row(500, "recovered", {"node": 1}, node=1),
        ]
        faults = fault_intervals(_run(tmp_path, rows))
        assert faults[0].cleared_by == "auto-recovered"

    def test_still_open_at_run_end(self, tmp_path):
        rows = [can_event_row(0, 1, CanEvent.Fault, bytes([ServiceStatus.FeedTimeout.value]))]
        faults = fault_intervals(_run(tmp_path, rows))
        assert faults[0].censored is True
        assert faults[0].cleared_by == "still open at run end"

    def test_inferred_at_next_loaded(self, tmp_path):
        rows = [
            can_event_row(0, 1, CanEvent.Fault, bytes([ServiceStatus.Jam.value])),
            can_event_row(1000, 1, CanEvent.Loaded),
        ]
        faults = fault_intervals(_run(tmp_path, rows))
        assert faults[0].inferred_close is True
        assert faults[0].cleared_by == "inferred at next Loaded"


class TestFaultsSection:
    def test_table_lives_in_data_quality(self, tmp_path):
        rows = [
            can_event_row(0, 2, CanEvent.Fault, bytes([ServiceStatus.FeedTimeout.value])),
            row(ts_ms=10_000, node_id=2, frame_type="COMMAND", source="CAN",
                direction="TX", event_name="Recover"),
            exp_row(10_000, "node_recovered", {"node": 2}, node=2),
            can_event_row(20_000, 1, CanEvent.Fault, bytes([ServiceStatus.Jam.value])),
        ]
        html = data_quality_section(_ctx(_run(tmp_path, rows))).html
        assert "Faults &amp; Downtime" in html
        assert "What it means" in html
        assert "Cleared by" in html
        assert "Out of pellets" in html
        assert "Recover command" in html
        assert "still open at run end" in html
        assert "Downtime:" in html
        assert "node 2:" in html
        # The table is ordered by start time, so the earlier node-2 fault
        # comes before the later node-1 fault.
        assert html.index("FeedTimeout") < html.index("Jam")
        assert faults_section(_ctx(_run(tmp_path, rows))).empty is True


class TestDataQuality:
    def test_reloads_and_mechanism_table(self, tmp_path):
        rows = [
            dome_hold_row(0, 1),
            retracting_row(500, 1),
            can_event_row(800, 1, CanEvent.Raising),
            pellet_reload_row(900, 1, reason=1, attempt=1),
            can_event_row(1000, 1, CanEvent.Fault, bytes([ServiceStatus.FeedTimeout.value])),
        ]
        html = data_quality_section(_ctx(_run(tmp_path, rows))).html
        assert "Details in Faults" not in html
        assert "<a " not in html
        assert html.index("Pellet reloads") < html.index("Mechanism events per node") < html.index("Faults &amp; Downtime")
        assert "What it means" in html
        assert "pellet fell off during the raise" in html
        assert "1 of 3" in html
        assert "Mechanism events per node" in html
        assert "expected behaviour, not faults" in html
        assert ">1<" in html or "<td>1</td>" in html

    def test_post_session_rows_are_not_noted(self, tmp_path):
        rows = [
            exp_row(0, "session_end", {"reason": "stopped"}),
            can_event_row(1000, 1, CanEvent.Loaded),
        ]
        html = data_quality_section(_ctx(_run(tmp_path, rows))).html
        assert "session_end" not in html
        assert "logged after" not in html
        assert "No data-quality issues detected" not in html

    def test_only_reboot_missed_frames_and_missing_session_end(self, tmp_path):
        rows = [
            input_changed_row(0, 1, 4, True, "MousePresence Detected"),
            input_changed_row(1000, 1, 4, False, "MousePresence Cleared"),
        ]
        html = data_quality_section(_ctx(_run(tmp_path, rows))).html
        assert "No session_end" in html
        assert "presence bout" not in html
        assert "bus loss" not in html
        assert "No data-quality issues detected" not in html

    def test_unmatched_dome_closes_are_not_reported(self, tmp_path):
        rows = [row(
            ts_ms=0, node_id=1, frame_type="EVENT", event_name="Dome closed",
        )]
        html = data_quality_section(_ctx(_run(tmp_path, rows))).html
        assert "dome bout" not in html


class TestApparatusHealthRemoved:
    def test_bundled_designs_do_not_name_it(self):
        roots = [DEFAULT_REPORTS_DIR, EXAMPLES_DESIGNS]
        seen = 0
        for root in roots:
            for path in root.glob("*.json"):
                seen += 1
                text = path.read_text(encoding="utf-8")
                assert "generic.apparatus_health" not in text, path
                assert "generic.faults" not in text, path
                design = json.loads(text)
                for key in ("sections", "combined_sections"):
                    refs = [item["ref"] for item in design.get(key, [])]
                    assert "generic.data_quality" in refs, path
        assert seen >= 8

    def test_alias_renders_nothing(self, tmp_path):
        result = apparatus_health_section(_ctx(_run(tmp_path, [dome_hold_row(0, 1)])))
        assert result.empty is True
        assert result.html == ""


class TestTimelineMechanism:
    def test_hold_span_and_markers_and_legends(self, tmp_path):
        rows = [
            dome_hold_row(0, 1),
            retracting_row(400, 1),
            pellet_reload_row(600, 1, reason=2, attempt=1, count=0x59),
            can_event_row(1000, 1, CanEvent.Raising),
        ]
        run = _run(tmp_path, rows)
        m = compute_run_metrics(run)
        spans, marks = panel_marks_spans(run, m, [1], 0, run.duration_s)
        assert any(s.title == "dome hold" and s.key == 4 and s.hatch for s in spans)
        assert any(mk.title == "Retracting" and mk.glyph == "square" for mk in marks)
        reload_marks = [mk for mk in marks if mk.glyph == "cross" and "Pellet Reload" in mk.title]
        assert len(reload_marks) == 1
        assert "pellet missing after a retract" in reload_marks[0].title
        assert "attempt 1 of 3" in reload_marks[0].title
        assert ("dome hold", 4) in SPAN_LEGEND

        html = session_raster_section(_ctx(run)).html
        assert "dome hold" in html
        assert "Retracting" in html
        assert "Pellet Reload" in html


@pytest.mark.skipif(not FIELD_LOG.is_file(), reason="field log 102626_1.2F.csv is not in the repo")
class TestFieldLogRegression:
    def test_counts_and_report_text(self):
        runs = load_runs(FIELD_LOG)
        assert len(runs) == 1
        m = compute_run_metrics(runs[0])
        assert len(m.faults) == 5
        assert sum(1 for f in m.faults if f.censored) == 2
        assert all(f.cleared_by == "Recover command" for f in m.faults if not f.censored)
        assert all(f.cleared_by == "still open at run end" for f in m.faults if f.censored)
        assert len(m.reloads) == 3
        assert len(m.holds) == 175
        assert len(m.retracts) == 141
        reasons = sorted(r.reason.name for r in m.reloads if r.reason is not None)
        assert reasons.count("LostDuringRaise") == 2
        assert reasons.count("MissingAfterRetract") == 1

        design = resolve_design(runs[0].experiment)
        html = render_report_html(runs, design, include_explorer=False)
        assert "Apparatus Health" not in html
        assert "dome bout" not in html
        assert "No data-quality issues detected" not in html
        assert "What it means" in html
        assert "<h2>Faults" not in html
        assert html.index("Pellet reloads") < html.index("Mechanism events per node") < html.index("Faults &amp; Downtime")
        assert "Cleared by" in html
        assert "still open at run end" in html
        assert "Out of pellets" in html
        assert "pellet fell off during the raise" in html
        assert "pellet missing after a retract" in html
        assert "1 of 3" in html
        assert "dome hold" in html
        assert "Retracting" in html
        assert "Pellet Reload" in html
        assert "<a " not in html
