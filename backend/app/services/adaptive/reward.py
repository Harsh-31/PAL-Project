"""Reward function: r_t = r_acc + r_time + r_prog + r_mom.

Every component is a pure function of the actual interaction outcome
(correctness, a response-time speed score, the difficulty attempted, and the
resulting streak momentum) — nothing here is invented or randomly sampled.
`speed_score` and `streak_momentum_after` are taken directly from the
LearnerState update (state.py) so there is exactly one place that defines
"how fast was this response" / "what is the current streak" — no duplicated
normalization logic.
"""
from __future__ import annotations
from dataclasses import dataclass
from ._math import clamp
from .actions import ACTION_RANK, Difficulty
from .config import RewardConfig, DEFAULT_CONFIG


@dataclass(frozen=True)
class RewardComponents:
    r_acc: float
    r_time: float
    r_prog: float
    r_mom: float

    @property
    def total(self) -> float:
        return self.r_acc + self.r_time + self.r_prog + self.r_mom

    def as_dict(self) -> dict:
        return {"r_acc": self.r_acc, "r_time": self.r_time, "r_prog": self.r_prog,
                "r_mom": self.r_mom, "total": self.total}


def accuracy_reward(correct: bool, config: RewardConfig = DEFAULT_CONFIG.reward) -> float:
    """+1 for correct, -0.5 for incorrect."""
    return config.correct_reward if correct else config.incorrect_penalty


def time_reward(speed_score: float, config: RewardConfig = DEFAULT_CONFIG.reward) -> float:
    """Rewards responses that were fast relative to the learner's own history.
    `speed_score` in [0,1] (1 = fast) — see LearnerState.normalized_response_time.
    Independent of correctness: r_acc already scores correctness, so this
    isn't double counted."""
    return config.time_reward_max * clamp(speed_score, 0.0, 1.0)


def progress_reward(action_taken: Difficulty, correct: bool,
                     config: RewardConfig = DEFAULT_CONFIG.reward) -> float:
    """Rewards answering CORRECTLY at harder difficulty levels — this is what
    teaches the Q-policy that pushing difficulty up is only good when the
    learner can actually handle it. Zero on an incorrect answer."""
    if not correct:
        return 0.0
    rank = ACTION_RANK[action_taken]  # EASY=0, MEDIUM=1, HARD=2
    max_rank = len(ACTION_RANK) - 1
    return config.progress_reward_max * (rank / max_rank)


def momentum_reward(streak_momentum_after: float, config: RewardConfig = DEFAULT_CONFIG.reward) -> float:
    """Rewards positive correctness streaks; never penalizes a losing streak
    (range is [0, momentum_reward_max], per the paper)."""
    return config.momentum_reward_max * max(0.0, streak_momentum_after)


def compute_reward(*, correct: bool, speed_score: float, action_taken: Difficulty,
                    streak_momentum_after: float,
                    config: RewardConfig = DEFAULT_CONFIG.reward) -> RewardComponents:
    return RewardComponents(
        r_acc=accuracy_reward(correct, config),
        r_time=time_reward(speed_score, config),
        r_prog=progress_reward(action_taken, correct, config),
        r_mom=momentum_reward(streak_momentum_after, config),
    )
