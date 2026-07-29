#!/usr/bin/env python3
"""
run_experiment.py — run an SFM experiment headless (no GUI).

Point it at a built-in template name, a ``.json`` schema, or your own ``.py``
script, and it runs blocking against a SocketCAN interface until the session
ends (duration / pellet cap / Ctrl+C).

Examples::

    # Built-in template using its experiments/<name>.json values, on vcan0:
    python run_experiment.py free_feeding --interface vcan0 --minutes 10

    # Override params (scalar / list / per-node map):
    python run_experiment.py probability_delivery --set probabilities=1:20,2:80
    python run_experiment.py fixed_and_random --set node_roles=1:fixed,2:off,3:random

    # Your own script exposing `exp` or `build(**kwargs) -> Experiment`:
    python run_experiment.py path/to/my_task.py --nodes 1,2,4

    # Discover what's available, or build without hardware:
    python run_experiment.py --list
    python run_experiment.py free_feeding --dry-run
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Ensure the package is importable when run as a script from tools/dev_gui/.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from base_station.experiment import Experiment, build_experiment  # noqa: E402
from base_station.experiment.schema import (  # noqa: E402
    DEFAULT_EXPERIMENTS_DIR,
    ExperimentDef,
    load_experiment_def,
    load_experiment_defs,
    resolve_builder,
)


# ---------------------------------------------------------------------------
# --set key=value parsing
# ---------------------------------------------------------------------------

def _coerce_scalar(text: str) -> Any:
    """Best-effort scalar coercion: bool → int → float → str."""
    t = text.strip()
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def parse_set_value(raw: str) -> Any:
    """
    Parse a --set value into a scalar, list, or per-node dict.

    Forms:
      * ``30`` / ``1.5`` / ``true`` / ``text`` → scalar
      * ``1,2,3``                              → list ([1, 2, 3])
      * ``1:fixed,2:off,3:random``             → dict ({1: "fixed", ...})
    Per-node dicts feed node_number / node_choice params; lists feed a nodes
    param; scalars feed everything else. build_experiment coerces per the
    schema type, so these forms map onto the GUI's per-node widgets.
    """
    raw = raw.strip()
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split(",") if p.strip() != ""]
    if parts and all(":" in p for p in parts):
        result: Dict[Any, Any] = {}
        for p in parts:
            k, _, v = p.partition(":")
            result[_coerce_scalar(k)] = _coerce_scalar(v)
        return result
    if len(parts) > 1:
        return [_coerce_scalar(p) for p in parts]
    return _coerce_scalar(raw)


def parse_overrides(pairs: Optional[Sequence[str]]) -> Dict[str, Any]:
    """Turn ``["k=v", ...]`` into ``{k: parsed_v}``."""
    out: Dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--set expects key=value, got: {pair!r}")
        key, _, value = pair.partition("=")
        out[key.strip()] = parse_set_value(value)
    return out


# ---------------------------------------------------------------------------
# Target resolution → Experiment
# ---------------------------------------------------------------------------

def available_defs() -> List[ExperimentDef]:
    """All JSON experiment definitions shipped under experiments/."""
    return load_experiment_defs(DEFAULT_EXPERIMENTS_DIR)


def _def_for_name(name: str) -> Optional[ExperimentDef]:
    for d in available_defs():
        if d.name == name:
            return d
    return None


def _load_script(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _warn_unknown_keys(exp_def: ExperimentDef, overrides: Dict[str, Any]) -> None:
    known = {p.key for p in exp_def.parameters}
    for key in overrides:
        if key not in known:
            print(f"warning: --set {key}=... is not a parameter of "
                  f"'{exp_def.name}' (ignored). Known: {sorted(known)}")


def build_from_target(
    target: str,
    overrides: Optional[Dict[str, Any]] = None,
    nodes: Optional[Sequence[int]] = None,
) -> Experiment:
    """
    Resolve ``target`` into a configured Experiment.

    Resolution order:
      1. a ``.json`` schema path                → build_experiment(def)
      2. a built-in template name with a JSON    → build_experiment(def)   [uses .json values]
      3. a ``.py`` script exposing ``exp``/``build``
      4. a template module name without a JSON   → resolve_builder(name)(**overrides)  [build() defaults]
    """
    overrides = dict(overrides or {})
    path = Path(target)

    # 1) explicit JSON schema file
    if path.suffix == ".json" and path.exists():
        return build_experiment(load_experiment_def(path), overrides, nodes)

    # 2) template name with a matching JSON schema
    exp_def = _def_for_name(target)
    if exp_def is not None:
        _warn_unknown_keys(exp_def, overrides)
        return build_experiment(exp_def, overrides, nodes)

    # 3) a user .py script
    if path.suffix == ".py" and path.exists():
        module = _load_script(path)
        if isinstance(getattr(module, "exp", None), Experiment):
            return module.exp
        if callable(getattr(module, "build", None)):
            kwargs = dict(overrides)
            if nodes is not None:
                kwargs["nodes"] = list(nodes)
            return module.build(**kwargs)
        raise SystemExit(
            f"{path} must define `exp = Experiment(...)` or `def build(...) -> Experiment`"
        )

    # 4) a template module name without a JSON schema
    try:
        builder = resolve_builder(target)
    except ImportError as exc:
        names = ", ".join(d.name for d in available_defs()) or "(none)"
        raise SystemExit(
            f"Unknown target {target!r}. Templates with JSON: {names}. "
            f"Or pass a .py / .json path."
        ) from exc
    kwargs = dict(overrides)
    if nodes is not None:
        kwargs["nodes"] = list(nodes)
    return builder(**kwargs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_list() -> None:
    defs = available_defs()
    if not defs:
        print("No experiment JSON files found in", DEFAULT_EXPERIMENTS_DIR)
        return
    print("Available templates (name — parameters):")
    for d in defs:
        params = ", ".join(f"{p.key}:{p.type}" for p in d.parameters)
        print(f"  {d.name:22s} {d.description}")
        if params:
            print(f"  {'':22s} params: {params}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_experiment.py",
        description="Run an SFM experiment headless (JSON template, or a .py script).",
    )
    parser.add_argument("target", nargs="?",
                        help="Template name, or path to a .json schema / .py script")
    parser.add_argument("--interface", "-i", default="can0",
                        help="SocketCAN interface (default: can0)")
    parser.add_argument("--bitrate", "-b", type=int, default=250_000)
    parser.add_argument("--nodes", default=None,
                        help="Comma-separated node IDs (default: template's own)")
    parser.add_argument("--set", dest="sets", action="append", metavar="KEY=VALUE",
                        help="Override a parameter (repeatable). "
                             "Value may be scalar, 'a,b' list, or '1:x,2:y' per-node map.")
    # Common conveniences (equivalent to --set):
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--seconds", type=float, default=None)
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--max-pellets", type=int, default=None, dest="max_pellets")
    parser.add_argument("--log-dir", default="~/sfm_logs")
    parser.add_argument("--no-io", action="store_true",
                        help="Do not open GPIO/BNC (non-Pi hosts)")
    parser.add_argument("--poll-hz", type=float, default=50.0)
    parser.add_argument("--list", action="store_true", help="List templates and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build the experiment and print it, without opening CAN")
    args = parser.parse_args(argv)

    if args.list:
        _print_list()
        return 0
    if not args.target:
        parser.error("a target (template name / .json / .py) is required (or use --list)")

    overrides = parse_overrides(args.sets)
    for key in ("minutes", "seconds", "hours", "max_pellets"):
        val = getattr(args, key)
        if val is not None:
            overrides.setdefault(key, val)

    nodes = None
    if args.nodes:
        nodes = [int(x) for x in args.nodes.split(",") if x.strip()]

    exp = build_from_target(args.target, overrides, nodes)

    if args.dry_run:
        print(f"[dry-run] built '{exp.name}' nodes={exp.nodes} "
              f"handlers={sorted(k.name for k in exp._handlers)}")
        return 0

    print(f"Starting '{exp.name}' on {args.interface} nodes={exp.nodes}  (Ctrl+C to stop)")
    ctx = exp.run(
        interface=args.interface,
        bitrate=args.bitrate,
        log_dir=args.log_dir,
        use_io=not args.no_io,
        poll_hz=args.poll_hz,
    )
    print(f"Session ended. pellets={ctx.counter('pellets')} elapsed={ctx.elapsed():.1f}s")
    if ctx.log_path:
        print(f"Log: {ctx.log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
