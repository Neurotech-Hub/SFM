"""
templates — built-in experiment templates.

Each template is a factory that returns a configured Experiment.
"""

from .free_feeding import build as free_feeding
from .fixed_and_random import build as fixed_and_random
from .probability_delivery import build as probability_delivery
from .two_armed_bandit import build as two_armed_bandit

__all__ = ["free_feeding", "fixed_and_random", "probability_delivery", "two_armed_bandit"]
