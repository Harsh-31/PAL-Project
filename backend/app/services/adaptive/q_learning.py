"""Tabular Q-learning over the discretized 6-d state and the 3-action space.

  Q_{t+1}(a_t) = Q_t(a_t) + alpha [ R_t + gamma * max_a' Q_t(a') - Q_t(a_t) ]

The Q-table is keyed by (discretized state, action) and is persisted GLOBALLY
(shared across learners — see persistence.py) rather than per-user: the
discretized state already encodes learner-specific skill/accuracy/etc, so a
shared table lets the policy learn from every learner's interactions and
converges far faster than a per-learner table would with MVP traffic volumes.

p_RL(d | x_t) is the epsilon-greedy policy expressed as a probability
simplex: the greedy (argmax) action gets mass (1 - epsilon), the remaining
epsilon is split uniformly across the other actions. This is both a real
exploration mechanism AND a real probability distribution the hybrid
blender can combine with p_stat — never a hard action choice in isolation.
"""
from __future__ import annotations
import random
from .actions import ACTIONS, Difficulty
from .config import AdaptiveConfig, DEFAULT_CONFIG


class QLearningPolicy:
    def __init__(self, config: AdaptiveConfig = DEFAULT_CONFIG,
                 q_table: dict[str, dict[str, float]] | None = None,
                 epsilon: float | None = None, seed: int | None = None):
        self.config = config
        self.q_table: dict[str, dict[str, float]] = q_table if q_table is not None else {}
        self.epsilon = epsilon if epsilon is not None else config.q.epsilon
        self.rng = random.Random(seed if seed is not None else config.q.seed)

    def q(self, state_key: str, action: Difficulty) -> float:
        return self.q_table.get(state_key, {}).get(action.value, 0.0)

    def _set_q(self, state_key: str, action: Difficulty, value: float) -> None:
        self.q_table.setdefault(state_key, {})[action.value] = value

    def best_action(self, state_key: str) -> Difficulty:
        values = {a: self.q(state_key, a) for a in ACTIONS}
        best = max(values.values())
        candidates = sorted((a for a, v in values.items() if v == best), key=lambda a: a.value)
        return candidates[0]

    def action_probabilities(self, state_key: str) -> dict[Difficulty, float]:
        """p_RL(d|x_t) — the epsilon-greedy policy as a probability distribution."""
        best = self.best_action(state_key)
        eps = self.epsilon
        n = len(ACTIONS)
        probs = {a: eps / n for a in ACTIONS}
        probs[best] += (1.0 - eps)
        return probs

    def select_epsilon_greedy(self, state_key: str) -> Difficulty:
        """Standalone epsilon-greedy action selection (used directly by tests /
        callers that want the RL policy without hybrid blending)."""
        if self.rng.random() < self.epsilon:
            return self.rng.choice(ACTIONS)
        return self.best_action(state_key)

    def sample(self, probs: dict[Difficulty, float]) -> Difficulty:
        """Sample an action from an arbitrary probability distribution over
        ACTIONS (used to sample from the blended hybrid policy)."""
        r = self.rng.random()
        cumulative = 0.0
        for a in ACTIONS:
            cumulative += probs[a]
            if r <= cumulative:
                return a
        return ACTIONS[-1]

    def update(self, state_key: str, action: Difficulty, reward: float,
               next_state_key: str) -> tuple[float, float]:
        """Applies the Q-learning update rule. Returns (q_before, q_after)."""
        q_before = self.q(state_key, action)
        max_next = max(self.q(next_state_key, a) for a in ACTIONS)
        q_after = q_before + self.config.q.alpha * (
            reward + self.config.q.gamma * max_next - q_before
        )
        self._set_q(state_key, action, q_after)
        return q_before, q_after

    def decay_epsilon(self) -> None:
        self.epsilon = max(self.config.q.epsilon_min, self.epsilon * self.config.q.epsilon_decay)
