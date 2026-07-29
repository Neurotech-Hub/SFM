#!/usr/bin/env python3
"""
example_experiment.py — the simplest way to run an experiment headless.

Two equally valid styles are shown:

  A) Configure a built-in template and run it.
  B) Write the behavior inline with the same callback API the templates use.

Run it:   python example_experiment.py         (uses vcan0 by default below)
Stop it:  Ctrl+C   (or it ends on the duration / pellet cap you set)

For a CLI that reads the JSON schema and takes --set overrides instead, use:
    python run_experiment.py free_feeding --interface vcan0 --minutes 10
    python run_experiment.py --list
"""

from base_station.experiment import Experiment
from base_station.experiment.templates.free_feeding import build as build_free_feeding

INTERFACE = "vcan0"   # SocketCAN interface the nodes (or node_simulator) are on


def run_builtin_template() -> None:
    """A) Take a built-in template, set parameters, run blocking."""
    exp = build_free_feeding(nodes=[1, 2, 3], reload_delay_s=30, minutes=10)
    exp.run(interface=INTERFACE, log_dir="~/sfm_logs", use_io=False)


def run_custom_inline() -> None:
    """B) Define behavior inline — no template file needed."""
    exp = Experiment(nodes=[1, 2, 3], name="my_task")

    @exp.on_start
    def _start(ctx):
        for n in ctx.nodes:
            ctx.dispense(n)

    @exp.on_dome_closed
    def _reload(ctx, ev):
        ctx.after(30.0, lambda: ctx.dispense(ev.node_id), node=ev.node_id)

    @exp.on_recover
    def _recover(ctx, ev):
        ctx.dispense(ev.node_id)

    exp.end_after(minutes=10)
    exp.run(interface=INTERFACE, use_io=False)


if __name__ == "__main__":
    run_builtin_template()
    # run_custom_inline()
