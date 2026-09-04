"""Hybrid policy blending.

  pi_t(d | x_t) = (1 - w_t) * p_stat(d | x_t) + w_t * p_RL(d | x_t)
  w_t = min(w_max, w0 + kappa * confidence_t * progress_t)

Early in a learner's interaction stream, confidence_t and progress_t are
both low, so w_t is close to w0 and the statistical (IRT) prior dominates.
As evidence accumulates (more attempts -> higher confidence, higher
progress), w_t rises toward w_max and the learned RL policy gets more
influence — exactly the paper's intended behavior.
"""
from __future__ import annotations
from .actions import ACTIONS, Difficulty
from .config import AdaptiveConfig, DEFAULT_CONFIG


class HybridAdaptivePolicy:
    def __init__(self, config: AdaptiveConfig = DEFAULT_CONFIG):
        self.config = config

    def progress(self, timestep: int) -> float:
        """progress_t in [0,1] — MVP proxy for 'how much evidence has this
        adaptation stream accumulated' (see README deviations)."""
        horizon = max(1, self.config.hybrid.progress_horizon)
        return min(timestep / horizon, 1.0)

    def blend_weight(self, confidence: float, progress: float) -> float:
        h = self.config.hybrid
        w = h.w0 + h.kappa * confidence * progress
        return min(h.w_max, max(0.0, w))

    def blend(self, p_stat: dict[Difficulty, float], p_rl: dict[Difficulty, float],
               w: float) -> dict[Difficulty, float]:
        """Convex combination of the two distributions, renormalized for
        numerical safety."""
        combined = {a: (1 - w) * p_stat[a] + w * p_rl[a] for a in ACTIONS}
        total = sum(combined.values()) or 1e-9
        return {a: v / total for a, v in combined.items()}
