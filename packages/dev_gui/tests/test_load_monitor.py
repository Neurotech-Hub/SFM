"""The load monitor's arithmetic and process lookup."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

from bench import load_monitor as lm  # noqa: E402


def test_slope_is_per_hour_and_handles_flat_and_short_series() -> None:
    assert lm.slope_per_hour([(0, 100), (3600, 160)]) == pytest.approx(60.0)
    assert lm.slope_per_hour([(0, 5), (10, 5), (20, 5)]) == 0.0
    assert lm.slope_per_hour([(0, 5)]) == 0.0
    assert lm.slope_per_hour([(7, 1), (7, 9)]) == 0.0, "no time passed, so no trend"


def test_a_busy_process_shows_cpu_and_an_idle_one_does_not() -> None:
    busy = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    idle = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        mon = lm.Monitor({"busy": f"pid:{busy.pid}", "idle": f"pid:{idle.pid}"})
        time.sleep(0.2)
        mon.sample()
        time.sleep(1.0)
        rows = {r["group"]: r for r in mon.sample()}
        one_core = 100.0 / lm.NCPU
        assert rows["busy"]["cpu_pct_machine"] > one_core * 0.6, rows["busy"]
        assert rows["idle"]["cpu_pct_machine"] < one_core * 0.1, rows["idle"]
        assert rows["busy"]["mem_mb"] > 3 and rows["busy"]["procs"] == 1
        assert 0 <= rows["machine"]["cpu_pct_machine"] <= 100
    finally:
        busy.kill(); idle.kill(); busy.wait(); idle.wait()


def test_a_match_group_finds_processes_by_command_line_and_not_itself() -> None:
    marker = f"sfm-test-marker-{os.getpid()}"
    p = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep(30)  # {marker}"])
    try:
        time.sleep(0.2)
        assert p.pid in lm.pids_matching(marker)
        assert os.getpid() not in lm.pids_matching(marker)
    finally:
        p.kill(); p.wait()


def test_a_gone_process_is_not_an_error() -> None:
    assert lm.resolve("pid:99999999") == []
    assert lm.cpu_ticks(99999999) is None
    with pytest.raises(ValueError):
        lm.resolve("bogus:1")


def test_the_summary_reports_mean_worst_and_memory_trend() -> None:
    samples = [{"t": float(t), "group": "engine", "cpu_pct_machine": c, "mem_mb": m, "procs": 1}
               for t, c, m in [(0, 1.0, 100.0), (1800, 3.0, 110.0), (3600, 2.0, 120.0)]]
    text = "\n".join(lm.summarize(samples))
    assert "mean  2.00%" in text and "worst  3.00%" in text and "trend   +20.0 MB/h" in text
