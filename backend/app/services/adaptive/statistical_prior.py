"""2PL IRT-style statistical prior.

  p_stat(d | x_t) proportional to sigma(a_d * (theta_t - b_d))

where theta_t is the learner's current skill estimate (in logit space) and
(a_d, b_d) are the discrimination/difficulty item parameters for difficulty
level d (Easy/Medium/Hard). The raw sigma(...) scores are normalized to sum
to 1, producing a real probability distribution over the three actions.

On top of the base IRT distribution we apply the paper's asymmetric
stability rule (promote at recent_accuracy >= 0.75, demote at <= 0.35) and a
cooldown/hold mechanism that biases the distribution toward the CURRENT
level for a few steps after any level change — this prevents oscillation
without ever hard-selecting a difficulty from the thresholds alone; the
thresholds only ever nudge a probability distribution.
"""
from __future__ import annotations
from ._math import logit, sigmoid
from .actions import ACTIONS, Difficulty, next_easier, next_harder
from .config import AdaptiveConfig, DEFAULT_CONFIG
from .state import LearnerState


class StatisticalPrior:
    def __init__(self, config: AdaptiveConfig = DEFAULT_CONFIG):
        self.config = config

    def _irt_probabilities(self, theta: float) -> dict[Difficulty, float]:
        irt = self.config.irt
        raw = {}
        for d in ACTIONS:
            a_d = irt.discrimination[d.value]
            b_d = irt.difficulty_b[d.value]
            raw[d] = sigmoid(a_d * (theta - b_d))
        return self._normalize(raw)

    def compute(self, state: LearnerState) -> dict[Difficulty, float]:
        theta = logit(state.skill)
        probs = self._irt_probabilities(theta)
        probs = self._apply_stability(probs, state)
        return self._normalize(probs)

    def _apply_stability(self, probs: dict[Difficulty, float], state: LearnerState) -> dict[Difficulty, float]:
        irt = self.config.irt
        current = Difficulty(state.last_action) if state.last_action else Difficulty.MEDIUM
        in_cooldown = state.steps_since_action_change < irt.cooldown_steps

        if in_cooldown:
            probs = dict(probs)
            probs[current] = probs[current] + irt.hold_bias * (1 - probs[current])
            return probs

        if state.recent_accuracy >= irt.promote_threshold:
            target = next_harder(current)
        elif state.recent_accuracy <= irt.demote_threshold:
            target = next_easier(current)
        else:
            return probs

        probs = dict(probs)
        probs[target] = probs[target] + irt.stability_shift * (1 - probs[target])
        return probs

    @staticmethod
    def _normalize(probs: dict[Difficulty, float]) -> dict[Difficulty, float]:
        total = sum(probs.values()) or 1e-9
        return {d: v / total for d, v in probs.items()}
