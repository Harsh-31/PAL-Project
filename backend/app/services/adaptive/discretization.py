"""Discretization layer: continuous 6-d learner state -> tabular Q-table key.

Each of the 6 dimensions of x_t is split into 3 human-readable buckets —
LOW / MID / HIGH — using the boundaries in ``config.BucketConfig``. This
keeps the Q-table small (3^6 = 729 possible states x 3 actions) and lets a
student inspect exactly which bucket a given learner state falls into.

Bucket boundaries (defaults, see config.py):
  skill            < 0.40 LOW · < 0.60 MID · else HIGH
  recent_accuracy  < 0.40 LOW · < 0.70 MID · else HIGH
  response_time    < 0.35 LOW · < 0.65 MID · else HIGH   (LOW = "slow", HIGH = "fast")
  streak_momentum  < -0.30 LOW · < 0.30 MID · else HIGH
  learning_velocity< -0.05 LOW · < 0.05 MID · else HIGH
  confidence       < 0.40 LOW · < 0.70 MID · else HIGH
"""
from __future__ import annotations
from dataclasses import dataclass

from .config import AdaptiveConfig, DEFAULT_CONFIG
from .state import LearnerState

BUCKET_LABELS = ("LOW", "MID", "HIGH")


def _bucket(value: float, bounds: tuple[float, float]) -> int:
    lo, hi = bounds
    if value < lo:
        return 0
    if value < hi:
        return 1
    return 2


@dataclass(frozen=True)
class DiscreteState:
    skill: int
    accuracy: int
    response_time: int
    streak: int
    velocity: int
    confidence: int

    @property
    def key(self) -> str:
        """Deterministic Q-table key for this discrete state."""
        return (f"sk{self.skill}-ac{self.accuracy}-rt{self.response_time}-"
                f"st{self.streak}-vl{self.velocity}-cf{self.confidence}")

    @property
    def labels(self) -> dict:
        return {
            "skill": BUCKET_LABELS[self.skill],
            "accuracy": BUCKET_LABELS[self.accuracy],
            "response_time": BUCKET_LABELS[self.response_time],
            "streak": BUCKET_LABELS[self.streak],
            "velocity": BUCKET_LABELS[self.velocity],
            "confidence": BUCKET_LABELS[self.confidence],
        }


def discretize(state: LearnerState, config: AdaptiveConfig = DEFAULT_CONFIG) -> DiscreteState:
    b = config.buckets
    return DiscreteState(
        skill=_bucket(state.skill, b.skill),
        accuracy=_bucket(state.recent_accuracy, b.accuracy),
        response_time=_bucket(state.normalized_response_time, b.response_time),
        streak=_bucket(state.streak_momentum, b.streak),
        velocity=_bucket(state.learning_velocity, b.velocity),
        confidence=_bucket(state.confidence, b.confidence),
    )
