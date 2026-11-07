"""Remembered node positions: keyed by MAC, safe against bad input and a killed process."""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import time

import pytest

from base_station import node_layout
from base_station.commands import run_command
from base_station.log_manager import LogManager
from base_station.node_layout import NodeLayout, normalize_mac
from base_station.node_registry import NodeRegistry
from base_station.state import build_snapshot

from tests.test_engine import _args, _FakeCan, _RecordingEngine

MAC_A = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])
MAC_B = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x02])


def test_a_position_is_saved_and_comes_back(tmp_path) -> None:
    path = tmp_path / "layout.json"
    NodeLayout(path).set(MAC_A, 0.25, 0.75)
    assert NodeLayout(path).get(MAC_A) == {"x": 0.25, "y": 0.75}
    assert json.loads(path.read_text())["version"] == 1


def test_every_spelling_of_a_mac_is_the_same_key(tmp_path) -> None:
    layout = NodeLayout(tmp_path / "layout.json")
    layout.set("aa:bb:cc:dd:ee:01", 0.1, 0.2)
    assert layout.get(MAC_A) == {"x": 0.1, "y": 0.2}
    assert layout.get("AA-BB-CC-DD-EE-01") == {"x": 0.1, "y": 0.2}
    assert list(layout.all()) == ["AA:BB:CC:DD:EE:01"]
    assert normalize_mac(MAC_A) == "AA:BB:CC:DD:EE:01"


@pytest.mark.parametrize("x,y", [
    (-0.1, 0.5), (1.1, 0.5), (0.5, 2), (float("nan"), 0.5), (0.5, float("inf")),
    ("0.5", 0.5), (None, 0.5), (True, 0.5),
])
def test_bad_positions_are_refused_and_nothing_is_written(tmp_path, x, y) -> None:
    path = tmp_path / "layout.json"
    layout = NodeLayout(path)
    with pytest.raises(ValueError):
        layout.set(MAC_A, x, y)
    assert layout.all() == {} and not path.exists()


def test_a_bad_mac_is_refused(tmp_path) -> None:
    layout = NodeLayout(tmp_path / "layout.json")
    for bad in ("not a mac", "AA:BB", b"\x01\x02", "ZZ:BB:CC:DD:EE:01"):
        with pytest.raises(ValueError):
            layout.set(bad, 0.5, 0.5)
    assert layout.get(None) is None and layout.get("junk") is None


def test_a_damaged_file_is_set_aside_not_fatal(tmp_path) -> None:
    path = tmp_path / "layout.json"
    path.write_text('{"version": 1, "positions": {"AA:BB:CC', encoding="utf-8")
    layout = NodeLayout(path)
    assert layout.all() == {}
    assert "could not be read" in layout.load_problem
    assert (tmp_path / "layout.json.bad").exists() and not path.exists()
    layout.set(MAC_A, 0.5, 0.5)       # and it keeps working
    assert NodeLayout(path).get(MAC_A) == {"x": 0.5, "y": 0.5}


def test_one_bad_entry_does_not_cost_the_others(tmp_path) -> None:
    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"version": 1, "positions": {
        "AA:BB:CC:DD:EE:01": {"x": 0.3, "y": 0.4},
        "nonsense": {"x": 0.1, "y": 0.1},
        "AA:BB:CC:DD:EE:02": {"x": 5, "y": 0.1},
        "AA:BB:CC:DD:EE:03": {"x": 0.1},
        "AA:BB:CC:DD:EE:04": "oops",
    }}), encoding="utf-8")
    layout = NodeLayout(path)
    assert list(layout.all()) == ["AA:BB:CC:DD:EE:01"]
    assert layout.load_problem is None


def test_a_failed_write_keeps_the_old_file_and_the_old_position(tmp_path, monkeypatch) -> None:
    path = tmp_path / "layout.json"
    layout = NodeLayout(path)
    layout.set(MAC_A, 0.1, 0.1)
    before = path.read_text()

    def boom(fd):
        raise OSError("disk full")
    monkeypatch.setattr(node_layout.os, "fsync", boom)
    with pytest.raises(OSError):
        layout.set(MAC_A, 0.9, 0.9)
    with pytest.raises(OSError):
        layout.set(MAC_B, 0.5, 0.5)
    assert path.read_text() == before
    assert layout.get(MAC_A) == {"x": 0.1, "y": 0.1}
    assert layout.get(MAC_B) is None


def test_a_leftover_temp_file_is_ignored(tmp_path) -> None:
    path = tmp_path / "layout.json"
    NodeLayout(path).set(MAC_A, 0.2, 0.2)
    (tmp_path / "layout.json.tmp").write_text("half written {{{")
    assert NodeLayout(path).get(MAC_A) == {"x": 0.2, "y": 0.2}


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_killing_the_writer_at_random_moments_never_corrupts_the_file(tmp_path) -> None:
    path = tmp_path / "layout.json"
    NodeLayout(path).set(MAC_A, 0.5, 0.5)
    code = (
        "import random, sys\n"
        "from base_station.node_layout import NodeLayout\n"
        "layout = NodeLayout(sys.argv[1])\n"
        "print('go', flush=True)\n"
        "while True:\n"
        "    for i in range(1, 7):\n"
        "        layout.set('AA:BB:CC:DD:EE:0%d' % i, random.random(), random.random())\n"
    )
    rng = random.Random(7)
    for _ in range(8):
        child = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
        assert child.stdout.readline().strip() == "go"
        time.sleep(rng.uniform(0.0, 0.15))
        child.send_signal(signal.SIGKILL)
        child.wait(5)
        reloaded = NodeLayout(path)
        assert reloaded.load_problem is None, "a killed writer left an unreadable file"
        assert reloaded.get(MAC_A) is not None


# ---------------------------------------------------------------------------
# Through the engine: positions follow the MAC, not the node ID
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    e = _RecordingEngine(_args())
    e._registry = NodeRegistry(3)
    e._log = LogManager(log_dir=str(tmp_path), auto_save=False)
    e._can = _FakeCan()
    e._layout = NodeLayout(tmp_path / "layout.json")
    yield e
    e._log.close()


def _ok(engine, name, **a):
    out = run_command(engine, name, a)
    assert out["ok"], out
    return out


def test_positions_follow_the_mac_when_rediscover_reassigns_ids(engine) -> None:
    engine._registry.register_node(1, MAC_A)
    engine._registry.register_node(2, MAC_B)
    _ok(engine, "set_node_position", node=1, x=0.1, y=0.2)
    _ok(engine, "set_node_position", node=2, x=0.8, y=0.9)

    # Re-discover: the same boards come back with their IDs swapped.
    engine._registry.clear_identities()
    engine._registry.register_node(1, MAC_B)
    engine._registry.register_node(2, MAC_A)

    nodes = {n["id"]: n for n in build_snapshot(engine)["nodes"]}
    assert nodes[1]["mac"] == "AA:BB:CC:DD:EE:02" and nodes[1]["position"] == {"x": 0.8, "y": 0.9}
    assert nodes[2]["mac"] == "AA:BB:CC:DD:EE:01" and nodes[2]["position"] == {"x": 0.1, "y": 0.2}


def test_a_node_without_a_mac_is_not_saved(engine) -> None:
    out = _ok(engine, "set_node_position", node=3, x=0.5, y=0.5)
    assert out["saved"] is False and "MAC" in out["reason"]
    assert engine._layout.all() == {} and not engine._layout.path.exists()
    assert build_snapshot(engine)["nodes"][2]["position"] is None


def test_bad_positions_are_operator_errors(engine) -> None:
    engine._registry.register_node(1, MAC_A)
    for x, y in [(2, 0.5), (0.5, -1), ("a", 0.5)]:
        out = run_command(engine, "set_node_position", {"node": 1, "x": x, "y": y})
        assert out["ok"] is False and "between 0 and 1" in out["error"] or "number" in out["error"]
    assert "not in this setup" in run_command(engine, "set_node_position", {"node": 9, "x": 0.5, "y": 0.5})["error"]


def test_get_and_clear_layout(engine) -> None:
    engine._registry.register_node(1, MAC_A)
    _ok(engine, "set_node_position", node=1, x=0.3, y=0.3)
    assert _ok(engine, "get_layout")["positions"] == {"AA:BB:CC:DD:EE:01": {"x": 0.3, "y": 0.3}}
    _ok(engine, "clear_layout")
    assert _ok(engine, "get_layout")["positions"] == {}
    assert NodeLayout(engine._layout.path).all() == {}


def test_without_a_layout_the_snapshot_still_builds(engine) -> None:
    engine._layout = None
    engine._registry.register_node(1, MAC_A)
    assert build_snapshot(engine)["nodes"][0]["position"] is None
    assert _ok(engine, "get_layout")["positions"] == {}
    assert run_command(engine, "set_node_position", {"node": 1, "x": 0.5, "y": 0.5})["ok"] is False


def test_a_damaged_layout_file_raises_a_notice(tmp_path) -> None:
    path = tmp_path / "layout.json"
    path.write_text("{{{", encoding="utf-8")
    e = _RecordingEngine(_args())
    e.enable_node_layout(str(path))
    assert e.last_notice[0] == "warning" and "layout" in e.last_notice[1]
