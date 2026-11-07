"""
experiment_form.py — turn form values into experiment parameters and nodes.

This is the logic the DearPyGui app keeps in ``_collect_experiment_params``,
``_collect_node_param`` and ``_effective_nodes``, with the widgets taken out.
A front-end gives plain values (what the operator typed or ticked) and gets
back what the engine's ``start_experiment`` wants: ``(params, nodes)``.

Rules, unchanged from the DearPyGui app:

* A parameter hidden by ``visible_when`` is submitted with its default.
* The ``nodes`` parameter is not a build parameter. It picks the active nodes.
* The active nodes are, in order: the ticked ``nodes``; else the nodes whose
  ``node_number`` is above 0; else the nodes whose ``node_choice`` is not the
  first option; else every node.

One difference: a number outside its ``min``/``max`` is refused with a message.
The DearPyGui widgets clamped what was typed, so the same input could not
reach the experiment there; this refuses it instead of changing it silently.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .experiment.schema import ExperimentDef, ExperimentParam, coerce_param_value, param_visible


class FormError(ValueError):
    """A form value can't be used. The message is for the operator."""


def default_node_values(param: ExperimentParam, node_ids: Sequence[int]) -> Dict[int, Any]:
    """What each node's widget starts at, for ``nodes`` / ``node_number`` / ``node_choice``."""
    out: Dict[int, Any] = {}
    default_pct = max(0, round(100 / len(node_ids))) if node_ids else 0
    for node_id in node_ids:
        if param.type == "nodes":
            out[node_id] = True
        elif param.type == "node_number":
            lo = int(param.min) if param.min is not None else 0
            hi = int(param.max) if param.max is not None else 100
            out[node_id] = min(max(default_pct, lo), hi)
        else:  # node_choice
            opts = param.options or [str(param.default)]
            out[node_id] = str(param.default if param.default in opts else opts[0])
    return out


def describe(exp_def: ExperimentDef, node_ids: Sequence[int]) -> Dict[str, Any]:
    """The experiment as JSON-safe data, enough to draw its form."""
    params = []
    for p in exp_def.parameters:
        item: Dict[str, Any] = {
            "key": p.key,
            "label": p.label,
            "type": p.type,
            "default": p.default,
            "min": p.min,
            "max": p.max,
            "help": p.help,
            "options": list(p.options),
            "visible_when": p.visible_when,
        }
        if p.is_node_param:
            item["node_defaults"] = {str(n): v for n, v in default_node_values(p, node_ids).items()}
        params.append(item)
    return {
        "name": exp_def.name,
        "label": exp_def.label,
        "template": exp_def.template,
        "description": exp_def.description,
        "parameters": params,
    }


def _node_key(key: Any) -> int:
    try:
        return int(key)
    except (TypeError, ValueError):
        raise FormError(f"'{key}' is not a node number.") from None


def _node_values(param: ExperimentParam, raw: Any, node_ids: Sequence[int]) -> Dict[int, Any]:
    """Per-node values for a node-scoped parameter. Accepts a dict, or for ``nodes`` a list of ticked ids."""
    values = default_node_values(param, node_ids)
    if raw is None:
        return values
    if param.type == "nodes" and isinstance(raw, (list, tuple, set)):
        ticked = {_node_key(n) for n in raw}
        return {n: n in ticked for n in node_ids}
    if not isinstance(raw, Mapping):
        raise FormError(f"{param.label}: expected a value for each node.")
    for key, value in raw.items():
        node_id = _node_key(key)
        if node_id not in values:
            raise FormError(f"{param.label}: node {node_id} is not in this setup.")
        values[node_id] = value
    return {n: _coerce_node_value(param, v) for n, v in values.items()}


def _coerce_node_value(param: ExperimentParam, value: Any) -> Any:
    if param.type == "nodes":
        return bool(value)
    if param.type == "node_number":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise FormError(f"{param.label}: '{value}' is not a whole number.") from None
        lo = int(param.min) if param.min is not None else 0
        hi = int(param.max) if param.max is not None else 100
        if not lo <= number <= hi:
            raise FormError(f"{param.label}: {number} is outside {lo} to {hi}.")
        return number
    opts = param.options
    text = str(value)
    if opts and text not in opts:
        raise FormError(f"{param.label}: '{text}' is not one of {', '.join(opts)}.")
    return text


def _scalar(param: ExperimentParam, raw: Any) -> Any:
    """One scalar value, checked against its type and range."""
    if param.type in ("int", "float"):
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise FormError(f"{param.label}: '{raw}' is not a number.") from None
        if param.type == "int":
            if not number.is_integer():
                raise FormError(f"{param.label}: '{raw}' is not a whole number.")
            number = int(number)
        if param.min is not None and number < param.min:
            raise FormError(f"{param.label}: {number:g} is below the minimum of {param.min:g}.")
        if param.max is not None and number > param.max:
            raise FormError(f"{param.label}: {number:g} is above the maximum of {param.max:g}.")
        return number
    if param.type == "choice":
        text = str(raw)
        if param.options and text not in param.options:
            raise FormError(f"{param.label}: '{text}' is not one of {', '.join(param.options)}.")
        return text
    return coerce_param_value(param, raw)


def collect(
    exp_def: ExperimentDef,
    values: Optional[Mapping[str, Any]],
    node_ids: Sequence[int],
) -> Tuple[Dict[str, Any], List[int]]:
    """
    Build ``(params, nodes)`` from form values.

    ``values`` maps parameter keys to what the form holds: a scalar, or for the
    node parameters a ``{node_id: value}`` map (for ``nodes``, a list of ticked
    ids also works). A key left out takes its default. Raises ``FormError``.
    """
    values = dict(values or {})
    known = {p.key for p in exp_def.parameters}
    unknown = sorted(set(values) - known)
    if unknown:
        raise FormError(f"Unknown setting: {', '.join(unknown)}.")

    # What visible_when is judged against: the scalar values as entered.
    scalars: Dict[str, Any] = {}
    for p in exp_def.parameters:
        if p.is_node_param:
            continue
        scalars[p.key] = _scalar(p, values[p.key]) if p.key in values else p.default

    node_maps: Dict[str, Dict[int, Any]] = {}
    for p in exp_def.parameters:
        if p.is_node_param:
            node_maps[p.key] = _node_values(p, values.get(p.key), node_ids)

    params: Dict[str, Any] = {}
    for p in exp_def.parameters:
        if p.type == "nodes":
            continue
        if not param_visible(p, scalars):
            params[p.key] = p.default
        elif p.is_node_param:
            params[p.key] = node_maps[p.key]
        else:
            params[p.key] = scalars[p.key]

    return params, effective_nodes(exp_def, node_maps, node_ids)


def effective_nodes(
    exp_def: ExperimentDef,
    node_maps: Mapping[str, Mapping[int, Any]],
    node_ids: Sequence[int],
) -> List[int]:
    """The active nodes. See the module docstring for the order of the rules."""
    for p in exp_def.parameters:
        if p.type == "nodes":
            return [n for n, ticked in node_maps[p.key].items() if ticked]
    for p in exp_def.parameters:
        if p.type == "node_number":
            return [n for n, v in node_maps[p.key].items() if (v or 0) > 0]
    for p in exp_def.parameters:
        if p.type == "node_choice":
            inactive = p.options[0] if p.options else "off"
            return [n for n, v in node_maps[p.key].items() if v != inactive]
    return list(node_ids)
