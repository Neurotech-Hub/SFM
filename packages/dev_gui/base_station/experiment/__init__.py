"""
experiment — event-driven experiment engine for SFM.

Users write a template whose callbacks decide what to do next in response
to node events. The GUI hosts the runner; JSON schemas drive the Start form.
"""

from .events import EventKind, NodeEvent
from .context import ExperimentContext, ExperimentControl
from .runner import Experiment, ExperimentRunner
from .schema import ExperimentDef, ExperimentParam, load_experiment_defs, build_experiment
from .gui_controller import ExperimentController

__all__ = [
    "EventKind",
    "NodeEvent",
    "ExperimentContext",  # old name, kept as an alias for ExperimentControl
    "ExperimentControl",
    "Experiment",
    "ExperimentRunner",
    "ExperimentDef",
    "ExperimentParam",
    "load_experiment_defs",
    "build_experiment",
    "ExperimentController",
]
