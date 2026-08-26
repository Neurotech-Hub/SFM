"""protocol.py — compatibility shim.

The VFM CAN protocol now lives in the sfm-analysis SDK at
``sfm_analysis.protocol`` so that log-analysis tooling can decode frames
without a base-station checkout. This module re-exports it unchanged so
that every existing ``from base_station.protocol import X`` (and
``import base_station.protocol as protocol``) keeps working.

New code should import ``sfm_analysis.protocol`` directly. The star import
below is complete: protocol.py defines exactly one private module-level
name (``_COUNT_EVENTS``) and it has no consumers outside that module.
"""

from sfm_analysis.protocol import *  # noqa: F401,F403
