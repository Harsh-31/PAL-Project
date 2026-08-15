"""Delayed reward for the Threshold RL.

Computed over the last N interactions (default 5) rather than per-
interaction like the difficulty RL.

R_t = w1·ΔMastery + w2·ΔAccuracy − w3·InterventionCost − w4·UnnecessaryRemediation
"""
from __future__ import annotations
from dataclasses import dataclass
from .config import ThresholdRewardConfig, DEFAULT_THRESHOLD_CONFIG


@dataclass(frozen=True)
class ThresholdRewardComponents:
    r_mastery_delta: float
    r_accuracy_delta: float
    r_intervention_cost: float
    r_unnecessary_remediation: float

    @property
    def total(self) -> float:
        return (self.r_mastery_delta + self.r_accuracy_delta
                - self.r_intervention_cost - self.r_unnecessary_remediation)

    def as_dict(self) -> dict:
        return {
            "r_mastery_delta": self.r_mastery_delta,
            "r_accuracy_delta": self.r_accuracy_delta,
            "r_intervention_cost": self.r_intervention_cost,
            "r_unnecessary_remediation": self.r_unnecessary_remediation,
            "total": self.total,
        }


def compute_threshold_reward(
    *,
    mastery_before: float,
    mastery_after: float,
    accuracy_before: float,
    accuracy_after: float,
    interventions_triggered: int,
    interventions_without_improvement: int,
    window_size: int,
    config: ThresholdRewardConfig = DEFAULT_THRESHOLD_CONFIG.reward,
) -> ThresholdRewardComponents:
    mastery_delta = mastery_after - mastery_before
    accuracy_delta = accuracy_after - accuracy_before
    intervention_cost = interventions_triggered / max(window_size, 1)
    unnecessary = interventions_without_improvement / max(interventions_triggered, 1)

    return ThresholdRewardComponents(
        r_mastery_delta=config.w_mastery_delta * mastery_delta,
        r_accuracy_delta=config.w_accuracy_delta * accuracy_delta,
        r_intervention_cost=config.w_intervention_cost * intervention_cost,
        r_unnecessary_remediation=config.w_unnecessary_remediation * unnecessary,
    )
