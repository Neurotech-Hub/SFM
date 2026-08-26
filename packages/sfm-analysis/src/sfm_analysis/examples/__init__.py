"""Runnable recipes that ship in the wheel.

Cookbook copies also live in the source tree under ``examples/analysis/``;
those files are not installed. This package exists so a laptop with only
``pip install sfm-analysis`` can still run::

    python -m sfm_analysis.examples.actogram_by_event
    python -m sfm_analysis.examples.actogram_by_event /path/to/MySession.csv
"""
