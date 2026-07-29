"""Tests for the headless runner CLI (run_experiment.py)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_experiment import (
    build_from_target,
    parse_overrides,
    parse_set_value,
)
from base_station.experiment.schema import DEFAULT_EXPERIMENTS_DIR


# ---------------------------------------------------------------------------
# --set value parsing
# ---------------------------------------------------------------------------

def test_parse_set_value_scalars() -> None:
    assert parse_set_value("30") == 30
    assert parse_set_value("1.5") == 1.5
    assert parse_set_value("true") is True
    assert parse_set_value("false") is False
    assert parse_set_value("timer") == "timer"


def test_parse_set_value_list() -> None:
    assert parse_set_value("1,2,3") == [1, 2, 3]


def test_parse_set_value_per_node_map() -> None:
    assert parse_set_value("1:20,2:80") == {1: 20, 2: 80}
    assert parse_set_value("1:fixed,2:off,3:random") == {
        1: "fixed", 2: "off", 3: "random",
    }


def test_parse_overrides() -> None:
    out = parse_overrides(["minutes=10", "probabilities=1:20,2:80"])
    assert out == {"minutes": 10, "probabilities": {1: 20, 2: 80}}


# ---------------------------------------------------------------------------
# target resolution → Experiment
# ---------------------------------------------------------------------------

def test_build_from_template_name_uses_json() -> None:
    exp = build_from_target("free_feeding", overrides={"minutes": 5}, nodes=[1, 2])
    assert exp.name == "free_feeding"
    assert exp.nodes == [1, 2]
    assert exp._end_after_s == 300.0  # minutes=5 flowed through the JSON schema


def test_build_from_json_path() -> None:
    path = str(DEFAULT_EXPERIMENTS_DIR / "fixed_and_random.json")
    exp = build_from_target(path, overrides={"node_roles": {1: "fixed"}}, nodes=[1, 2, 3])
    assert exp.name == "fixed_and_random"
    assert exp.nodes == [1, 2, 3]


def test_build_from_probability_with_node_map() -> None:
    exp = build_from_target(
        "probability_delivery",
        overrides={"probabilities": {1: 0, 2: 100}, "max_pellets": 5},
        nodes=[1, 2],
    )
    assert exp.name == "probability_delivery"
    assert exp._end_pellets == 5


def test_build_from_py_script(tmp_path) -> None:
    script = tmp_path / "my_task.py"
    script.write_text(
        "from base_station.experiment import Experiment\n"
        "def build(nodes=None, **kw):\n"
        "    exp = Experiment(nodes=list(nodes or [1]), name='scripted')\n"
        "    return exp\n"
    )
    exp = build_from_target(str(script), nodes=[4, 5])
    assert exp.name == "scripted"
    assert exp.nodes == [4, 5]


def test_unknown_target_raises() -> None:
    import pytest

    with pytest.raises(SystemExit):
        build_from_target("does_not_exist")
