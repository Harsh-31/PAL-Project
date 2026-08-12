"""Configuration for the Hybrid RL adaptive-difficulty engine.

Every tunable parameter described in the AAAI-26 PAL paper lives here so
nothing is hard-coded inside the algorithm modules (state.py, q_learning.py,
statistical_prior.py, hybrid_policy.py, reward.py). Defaults are the paper's
reference values where given, or a documented, reasonable MVP choice where
the paper leaves an implementation detail open (noted inline).
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RewardConfig:
    """r_t = r_acc + r_time + r_prog + r_mom (see reward.py)."""
    correct_reward: float = 1.0
    incorrect_penalty: float = -0.5
    time_reward_max: float = 0.3
    progress_reward_max: float = 0.2
    momentum_reward_max: float = 0.1


@dataclass(frozen=True)
class IRTConfig:
    """2PL IRT-style statistical prior: p_stat(d|x_t) proportional to
    sigma(a_d * (theta_t - b_d)), plus the paper's asymmetric stability
    thresholds and a cooldown/hold mechanism against oscillation."""
    discrimination: dict = field(default_factory=lambda: {
        "EASY": 1.0, "MEDIUM": 1.3, "HARD": 1.6,
    })
    difficulty_b: dict = field(default_factory=lambda: {
        "EASY": -1.0, "MEDIUM": 0.0, "HARD": 1.0,
    })
    promote_threshold: float = 0.75   # recent_accuracy >= this -> promote
    demote_threshold: float = 0.35    # recent_accuracy <= this -> demote
    stability_shift: float = 0.25     # how strongly a promote/demote nudges p_stat toward the neighbor level
    hold_bias: float = 0.35           # how strongly cooldown biases p_stat toward holding the current level
    cooldown_steps: int = 2           # interactions to hold after a level change before another promote/demote nudge is allowed


@dataclass(frozen=True)
class QLearningConfig:
    """Tabular Q-learning: Q_{t+1}(a_t) = Q_t(a_t) + alpha[R_t + gamma*max_a' Q_t(a') - Q_t(a_t)]."""
    alpha: float = 0.15
    gamma: float = 0.90
    epsilon: float = 0.25
    epsilon_decay: float = 0.97
    epsilon_min: float = 0.05
    seed: int = 42


@dataclass(frozen=True)
class HybridConfig:
    """pi_t(d|x_t) = (1-w_t) p_stat(d|x_t) + w_t p_RL(d|x_t)
    w_t = min(w_max, w0 + kappa * confidence_t * progress_t)."""
    w_max: float = 0.8
    w0: float = 0.15
    kappa: float = 0.7
    # timesteps at which progress_t saturates to 1.0 (MVP proxy for "evidence
    # obtained about the learner" — the paper does not pin down progress_t's
    # exact functional form, see README deviations).
    progress_horizon: int = 20


@dataclass(frozen=True)
class BucketConfig:
    """Boundaries for turning the continuous 6-d state into a discrete Q-table
    key. Each dimension is split into 3 buckets: LOW(0) / MID(1) / HIGH(2)."""
    skill: tuple[float, float] = (0.4, 0.6)
    accuracy: tuple[float, float] = (0.4, 0.7)
    response_time: tuple[float, float] = (0.35, 0.65)
    streak: tuple[float, float] = (-0.3, 0.3)
    velocity: tuple[float, float] = (-0.05, 0.05)
    confidence: tuple[float, float] = (0.4, 0.7)


@dataclass(frozen=True)
class StateConfig:
    """Parameters for computing/updating the 6-d learner-state vector x_t."""
    recent_window: int = 5              # rolling window for recent_accuracy
    history_limit: int = 50             # bounded history for response-time / accuracy normalization
    skill_learning_rate: float = 0.20   # theta update step size (Elo-like online IRT ability update)
    streak_scale: float = 3.0           # tanh scale for streak_momentum
    velocity_window: int = 5            # window for learning_velocity comparison
    confidence_horizon: int = 10        # timesteps for confidence's evidence-volume term


@dataclass(frozen=True)
class AdaptiveConfig:
    state: StateConfig = field(default_factory=StateConfig)
    reward: RewardConfig = field(default_factory=RewardConfig)
    irt: IRTConfig = field(default_factory=IRTConfig)
    q: QLearningConfig = field(default_factory=QLearningConfig)
    hybrid: HybridConfig = field(default_factory=HybridConfig)
    buckets: BucketConfig = field(default_factory=BucketConfig)


DEFAULT_CONFIG = AdaptiveConfig()
