"""Tabular Q-learning for threshold adjustment.

Same Bellman update as adaptive/q_learning.py but with a 3-action space
per threshold (DECREASE / KEEP / INCREASE).  Two independent Q-tables
are managed — one for tau_struggling, one for tau_mastered — keyed by
the same discretized 6-d learner state.
"""
from __future__ import annotations
import random
from .actions import THRESHOLD_ACTIONS, ThresholdAction
from .config import ThresholdRLConfig, DEFAULT_THRESHOLD_CONFIG


class ThresholdQLearning:
    def __init__(
        self,
        config: ThresholdRLConfig = DEFAULT_THRESHOLD_CONFIG,
        q_table_struggling: dict[str, dict[str, float]] | None = None,
        q_table_mastered: dict[str, dict[str, float]] | None = None,
        epsilon: float | None = None,
        seed: int | None = None,
    ):
        self.config = config
        self.q_struggling: dict[str, dict[str, float]] = q_table_struggling or {}
        self.q_mastered: dict[str, dict[str, float]] = q_table_mastered or {}
        self.epsilon = epsilon if epsilon is not None else config.q.epsilon
        self.rng = random.Random(seed if seed is not None else config.q.seed)

    def _table(self, which: str) -> dict[str, dict[str, float]]:
        return self.q_struggling if which == "struggling" else self.q_mastered

    def q(self, which: str, state_key: str, action: ThresholdAction) -> float:
        return self._table(which).get(state_key, {}).get(action.value, 0.0)

    def _set_q(self, which: str, state_key: str, action: ThresholdAction, value: float) -> None:
        self._table(which).setdefault(state_key, {})[action.value] = value

    def best_action(self, which: str, state_key: str) -> ThresholdAction:
        values = {a: self.q(which, state_key, a) for a in THRESHOLD_ACTIONS}
        best = max(values.values())
        candidates = sorted(
            (a for a, v in values.items() if v == best), key=lambda a: a.value
        )
        return candidates[0]

    def select_epsilon_greedy(self, which: str, state_key: str) -> ThresholdAction:
        if self.rng.random() < self.epsilon:
            return self.rng.choice(THRESHOLD_ACTIONS)
        return self.best_action(which, state_key)

    def update(
        self, which: str, state_key: str, action: ThresholdAction,
        reward: float, next_state_key: str
    ) -> tuple[float, float]:
        q_before = self.q(which, state_key, action)
        max_next = max(self.q(which, next_state_key, a) for a in THRESHOLD_ACTIONS)
        cfg = self.config.q
        q_after = q_before + cfg.alpha * (reward + cfg.gamma * max_next - q_before)
        self._set_q(which, state_key, action, q_after)
        return q_before, q_after

    def decay_epsilon(self) -> None:
        cfg = self.config.q
        self.epsilon = max(cfg.epsilon_min, self.epsilon * cfg.epsilon_decay)
