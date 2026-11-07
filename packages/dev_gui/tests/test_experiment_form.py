"""
Form values -> (params, nodes). The rules are the ones the DearPyGui app applies
to its widgets; these tests pin them without any widgets.
"""

from __future__ import annotations

import json

import pytest

from base_station.experiment.schema import (
    DEFAULT_EXPERIMENTS_DIR,
    ExperimentDef,
    ExperimentParam,
    build_experiment,
    load_experiment_defs,
)
from base_station.experiment_form import FormError, collect, describe

NODES = [1, 2, 3]
DEFS = {d.name: d for d in load_experiment_defs(DEFAULT_EXPERIMENTS_DIR)}


def _def(*params: ExperimentParam) -> ExperimentDef:
    return ExperimentDef(name="t", label="T", template="free_feeding", parameters=list(params))


def test_every_builtin_experiment_builds_from_its_defaults() -> None:
    # Two nodes: two_armed_bandit needs exactly two arms, and the default form
    # ticks every node (the DearPyGui form behaves the same way).
    for name, exp_def in DEFS.items():
        params, nodes = collect(exp_def, {}, [1, 2])
        assert nodes, f"{name}: default form should enable at least one node"
        build_experiment(exp_def, params=params, nodes=nodes)


def test_describe_is_json_safe_and_lists_node_defaults() -> None:
    for exp_def in DEFS.values():
        text = json.dumps(describe(exp_def, NODES))
        assert exp_def.name in text
    nodes_param = next(p for p in describe(DEFS["free_feeding"], NODES)["parameters"] if p["type"] == "nodes")
    assert nodes_param["node_defaults"] == {"1": True, "2": True, "3": True}


def test_nodes_param_picks_active_nodes_from_a_list_or_a_map() -> None:
    d = DEFS["free_feeding"]
    key = next(p.key for p in d.parameters if p.type == "nodes")
    _, nodes = collect(d, {key: [1, 3]}, NODES)
    assert nodes == [1, 3]
    _, nodes = collect(d, {key: {"1": False, "2": True, "3": False}}, NODES)
    assert nodes == [2]
    _, nodes = collect(d, {key: []}, NODES)
    assert nodes == []


def test_nodes_param_is_not_a_build_parameter() -> None:
    d = DEFS["free_feeding"]
    key = next(p.key for p in d.parameters if p.type == "nodes")
    params, _ = collect(d, {}, NODES)
    assert key not in params


def test_hidden_parameters_submit_their_defaults() -> None:
    d = DEFS["free_feeding"]
    # fixed_delay_s is visible only when next_trial_wait == fixed_delay.
    params, _ = collect(d, {"next_trial_wait": "presence_clear", "fixed_delay_s": 999}, NODES)
    assert params["fixed_delay_s"] == 30.0
    params, _ = collect(d, {"next_trial_wait": "fixed_delay", "fixed_delay_s": 12}, NODES)
    assert params["fixed_delay_s"] == 12


def test_node_number_active_nodes_are_those_above_zero() -> None:
    d = DEFS["probability_delivery"]
    key = next(p.key for p in d.parameters if p.type == "node_number")
    params, nodes = collect(d, {key: {"1": 50, "2": 0, "3": 25}}, NODES)
    assert nodes == [1, 3]
    assert params[key] == {1: 50, 2: 0, 3: 25}


def test_node_number_defaults_split_evenly() -> None:
    d = DEFS["probability_delivery"]
    key = next(p.key for p in d.parameters if p.type == "node_number")
    params, nodes = collect(d, {}, NODES)
    assert nodes == NODES
    assert set(params[key].values()) == {33}


def test_node_choice_active_nodes_are_those_not_on_the_first_option() -> None:
    d = DEFS["fixed_and_random"]
    p = next(p for p in d.parameters if p.type == "node_choice")
    first, other = p.options[0], p.options[1]
    params, nodes = collect(d, {p.key: {"1": first, "2": other, "3": first}}, NODES)
    assert nodes == [2]
    assert params[p.key][2] == other


def test_with_no_node_parameter_every_node_runs() -> None:
    d = _def(ExperimentParam(key="minutes", label="Minutes", type="int", default=5))
    assert collect(d, {}, NODES)[1] == NODES


def test_numbers_are_checked_against_their_range() -> None:
    d = _def(ExperimentParam(key="minutes", label="Minutes", type="int", default=5, min=1, max=60))
    assert collect(d, {"minutes": 30}, NODES)[0] == {"minutes": 30}
    with pytest.raises(FormError, match="above the maximum"):
        collect(d, {"minutes": 61}, NODES)
    with pytest.raises(FormError, match="below the minimum"):
        collect(d, {"minutes": 0}, NODES)
    with pytest.raises(FormError, match="not a number"):
        collect(d, {"minutes": "abc"}, NODES)
    with pytest.raises(FormError, match="whole number"):
        collect(d, {"minutes": 2.5}, NODES)


def test_a_hidden_parameter_is_not_range_checked_against_stale_input() -> None:
    d = _def(
        ExperimentParam(key="mode", label="Mode", type="choice", default="a", options=["a", "b"]),
        ExperimentParam(key="x", label="X", type="int", default=1, min=0, max=5, visible_when={"mode": "b"}),
    )
    params, _ = collect(d, {"mode": "a"}, NODES)
    assert params["x"] == 1


def test_bad_choice_and_unknown_keys_are_refused() -> None:
    d = _def(ExperimentParam(key="mode", label="Mode", type="choice", default="a", options=["a", "b"]))
    with pytest.raises(FormError, match="not one of"):
        collect(d, {"mode": "z"}, NODES)
    with pytest.raises(FormError, match="Unknown setting"):
        collect(d, {"nope": 1}, NODES)


def test_node_values_must_belong_to_this_setup() -> None:
    d = DEFS["probability_delivery"]
    key = next(p.key for p in d.parameters if p.type == "node_number")
    with pytest.raises(FormError, match="not in this setup"):
        collect(d, {key: {"9": 10}}, NODES)
    with pytest.raises(FormError, match="outside"):
        collect(d, {key: {"1": 101}}, NODES)
    with pytest.raises(FormError, match="not a node number"):
        collect(d, {key: {"x": 10}}, NODES)
