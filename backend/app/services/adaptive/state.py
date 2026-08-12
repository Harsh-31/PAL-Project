"""The PAL learner-state vector x_t and its update rule.

x_t = [skill, recent_accuracy, normalized_response_time,
       streak_momentum, learning_velocity, confidence]

All six components are computed deterministically from real, observed
interaction data (correctness, response time, difficulty attempted) — there
is no random initialization or synthetic filler anywhere in this module.
"""
from __future__ import annotations
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ._math import clamp, logit, sigmoid
from .actions import Difficulty
from .config import AdaptiveConfig, DEFAULT_CONFIG

_STATE_FIELDS = (
    "user_id", "concept_id", "timestep", "skill", "recent_accuracy",
    "normalized_response_time", "streak_momentum", "learning_velocity",
    "confidence", "answer_history", "response_time_history",
    "accuracy_snapshots", "current_streak", "last_action",
    "steps_since_action_change", "updated_at",
)


@dataclass
class LearnerState:
    """Persistent, recomputed-after-every-answer learner state for one
    (learner, concept) adaptation stream.

    A fresh state starts skill-neutral (0.5 = IRT theta of 0) with zero
    confidence — there is no evidence yet, so confidence should be zero,
    not a guess.
    """

    user_id: str
    concept_id: str
    timestep: int = 0

    # ---- the 6-d vector x_t ----
    skill: float = 0.5
    recent_accuracy: float = 0.5
    normalized_response_time: float = 0.5
    streak_momentum: float = 0.0
    learning_velocity: float = 0.0
    confidence: float = 0.0

    # ---- bookkeeping needed to recompute the vector online ----
    answer_history: list = field(default_factory=list)
    response_time_history: list = field(default_factory=list)
    accuracy_snapshots: list = field(default_factory=list)
    current_streak: int = 0
    last_action: str | None = None
    steps_since_action_change: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_vector(self) -> list[float]:
        """The literal x_t vector from the paper."""
        return [self.skill, self.recent_accuracy, self.normalized_response_time,
                self.streak_momentum, self.learning_velocity, self.confidence]

    # -------------------------------------------------------------------
    def update(self, *, correct: bool, response_time_sec: float,
               action_taken: Difficulty, config: AdaptiveConfig = DEFAULT_CONFIG) -> "LearnerState":
        """Recompute the full state vector after one answered question. Mutates
        and returns self."""
        cfg = config.state
        self.timestep += 1

        # 1) skill — online 2PL-IRT ability estimate (Elo-style logit update).
        #    Uses the SAME item parameters (a_d, b_d) as the statistical prior
        #    (see statistical_prior.py), so `skill` really is theta_t in
        #    p_stat(d|x_t) proportional to sigma(a_d(theta_t-b_d)).
        irt = config.irt
        a_d = irt.discrimination[action_taken.value]
        b_d = irt.difficulty_b[action_taken.value]
        theta = logit(self.skill)
        p_expected = sigmoid(a_d * (theta - b_d))
        theta_new = theta + cfg.skill_learning_rate * ((1.0 if correct else 0.0) - p_expected)
        self.skill = clamp(sigmoid(theta_new))

        # 2) recent_accuracy — mean correctness over the configured window.
        self.answer_history.append(bool(correct))
        self.answer_history = self.answer_history[-cfg.history_limit:]
        window = self.answer_history[-cfg.recent_window:]
        self.recent_accuracy = sum(window) / len(window)
        self.accuracy_snapshots.append(self.recent_accuracy)
        self.accuracy_snapshots = self.accuracy_snapshots[-cfg.history_limit:]

        # 3) normalized_response_time — z-score of this response against the
        #    learner's OWN history, squashed to [0,1] where 1 = fast, 0 = slow
        #    relative to how this learner has typically answered.
        history = self.response_time_history
        if len(history) >= 2:
            mean_rt = statistics.mean(history)
            std_rt = statistics.pstdev(history) or 1e-6
            z = (response_time_sec - mean_rt) / std_rt
            self.normalized_response_time = clamp(sigmoid(-z))
        else:
            self.normalized_response_time = 0.5
        history.append(max(0.0, float(response_time_sec)))
        self.response_time_history = history[-cfg.history_limit:]

        # 4) streak_momentum — signed streak length squashed with tanh into
        #    [-1, 1] (positive = correct streak, negative = incorrect streak).
        if correct:
            self.current_streak = max(self.current_streak, 0) + 1
        else:
            self.current_streak = min(self.current_streak, 0) - 1
        self.streak_momentum = math.tanh(self.current_streak / cfg.streak_scale)

        # 5) learning_velocity — change in recent_accuracy between the current
        #    window of snapshots and the window immediately preceding it.
        snaps = self.accuracy_snapshots
        vw = cfg.velocity_window
        if len(snaps) >= 2 * vw:
            now_avg = statistics.mean(snaps[-vw:])
            prev_avg = statistics.mean(snaps[-2 * vw:-vw])
            self.learning_velocity = clamp(now_avg - prev_avg, -1.0, 1.0)
        elif len(snaps) >= 2:
            self.learning_velocity = clamp(snaps[-1] - snaps[0], -1.0, 1.0)
        else:
            self.learning_velocity = 0.0

        # 6) confidence — explicit, documented blend of three observable
        #    signals (no randomness):
        #      (a) evidence volume   — how many attempts we've seen so far
        #      (b) outcome consistency — low variance in recent correctness
        #      (c) timing consistency  — low variability in response time
        evidence = min(self.timestep / cfg.confidence_horizon, 1.0)
        recent_for_var = self.answer_history[-cfg.recent_window:]
        if len(recent_for_var) > 1:
            correctness_var = statistics.pvariance([1.0 if a else 0.0 for a in recent_for_var])
        else:
            correctness_var = 0.25  # max possible variance of a Bernoulli variable
        consistency = 1.0 - min(correctness_var / 0.25, 1.0)
        rt_recent = self.response_time_history[-cfg.recent_window:]
        if len(rt_recent) > 1 and statistics.mean(rt_recent) > 1e-6:
            cv = statistics.pstdev(rt_recent) / statistics.mean(rt_recent)
            time_consistency = 1.0 - min(cv, 1.0)
        else:
            time_consistency = 0.5
        self.confidence = clamp(0.5 * evidence + 0.3 * consistency + 0.2 * time_consistency)

        # bookkeeping for the statistical prior's cooldown/hold mechanism
        if self.last_action == action_taken.value:
            self.steps_since_action_change += 1
        else:
            self.steps_since_action_change = 0
        self.last_action = action_taken.value
        self.updated_at = datetime.now(timezone.utc)
        return self

    # -------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in _STATE_FIELDS}

    @classmethod
    def from_dict(cls, data: dict) -> "LearnerState":
        data = dict(data)
        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            data["updated_at"] = datetime.fromisoformat(updated_at)
        elif updated_at is None:
            data["updated_at"] = datetime.now(timezone.utc)
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in valid})
