"""Action space for the Threshold RL.

Two independent 3-action policies — one for tau_struggling, one for
tau_mastered.  Each action adjusts its threshold by +/- step_delta or
holds it unchanged.
"""
from __future__ import annotations
from enum import Enum


class ThresholdAction(str, Enum):
    DECREASE = "DECREASE"
    KEEP = "KEEP"
    INCREASE = "INCREASE"


THRESHOLD_ACTIONS: list[ThresholdAction] = list(ThresholdAction)
