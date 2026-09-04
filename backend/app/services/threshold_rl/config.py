"""Configuration for the Threshold RL engine.

Mirrors the structure of adaptive/config.py but governs the per-learner
cognitive-state boundary learning (tau_struggling, tau_mastered).
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ThresholdBoundsConfig:
    tau_struggling_min: float = 0.20
    tau_struggling_max: float = 0.60
    tau_mastered_min: float = 0.60
    tau_mastered_max: float = 0.95
    min_gap: float = 0.15

    tau_struggling_default: float = 0.40
    tau_mastered_default: float = 0.85


@dataclass(frozen=True)
class ThresholdRewardConfig:
    w_mastery_delta: float = 0.4
    w_accuracy_delta: float = 0.3
    w_intervention_cost: float = 0.2
    w_unnecessary_remediation: float = 0.1


@dataclass(frozen=True)
class ThresholdQLearningConfig:
    alpha: float = 0.15
    gamma: float = 0.90
    epsilon: float = 0.25
    epsilon_decay: float = 0.97
    epsilon_min: float = 0.05
    seed: int = 137
    step_delta: float = 0.03


@dataclass(frozen=True)
class ThresholdUpdateConfig:
    update_every_n: int = 5


@dataclass(frozen=True)
class ThresholdRLConfig:
    bounds: ThresholdBoundsConfig = field(default_factory=ThresholdBoundsConfig)
    reward: ThresholdRewardConfig = field(default_factory=ThresholdRewardConfig)
    q: ThresholdQLearningConfig = field(default_factory=ThresholdQLearningConfig)
    update: ThresholdUpdateConfig = field(default_factory=ThresholdUpdateConfig)


DEFAULT_THRESHOLD_CONFIG = ThresholdRLConfig()
