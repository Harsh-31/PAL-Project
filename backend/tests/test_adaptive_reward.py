"""Reward function tests: r_t = r_acc + r_time + r_prog + r_mom, each
component independently testable and bounded per the AAAI-26 PAL paper."""
from app.services.adaptive.actions import Difficulty
from app.services.adaptive.config import DEFAULT_CONFIG
from app.services.adaptive.reward import (
    accuracy_reward, compute_reward, momentum_reward, progress_reward, time_reward,
)


def test_accuracy_reward_correct_vs_incorrect():
    assert accuracy_reward(True) == DEFAULT_CONFIG.reward.correct_reward == 1.0
    assert accuracy_reward(False) == DEFAULT_CONFIG.reward.incorrect_penalty == -0.5


def test_time_reward_bounded_in_range():
    r_fast = time_reward(1.0)
    r_slow = time_reward(0.0)
    assert 0.0 <= r_slow <= DEFAULT_CONFIG.reward.time_reward_max
    assert 0.0 <= r_fast <= DEFAULT_CONFIG.reward.time_reward_max
    assert r_fast > r_slow
    assert r_fast == DEFAULT_CONFIG.reward.time_reward_max


def test_progress_reward_zero_on_incorrect():
    assert progress_reward(Difficulty.HARD, False) == 0.0
    assert progress_reward(Difficulty.EASY, False) == 0.0


def test_progress_reward_scales_with_difficulty_on_correct():
    easy = progress_reward(Difficulty.EASY, True)
    medium = progress_reward(Difficulty.MEDIUM, True)
    hard = progress_reward(Difficulty.HARD, True)
    assert easy == 0.0
    assert 0.0 < medium < hard
    assert hard == DEFAULT_CONFIG.reward.progress_reward_max


def test_momentum_reward_never_negative():
    assert momentum_reward(-1.0) == 0.0
    assert momentum_reward(0.0) == 0.0
    assert momentum_reward(1.0) == DEFAULT_CONFIG.reward.momentum_reward_max


def test_compute_reward_sums_all_components():
    rc = compute_reward(correct=True, speed_score=0.8, action_taken=Difficulty.HARD,
                         streak_momentum_after=0.5)
    assert abs(rc.total - (rc.r_acc + rc.r_time + rc.r_prog + rc.r_mom)) < 1e-9
    d = rc.as_dict()
    assert set(d.keys()) == {"r_acc", "r_time", "r_prog", "r_mom", "total"}


def test_incorrect_hard_answer_yields_low_total_reward():
    rc = compute_reward(correct=False, speed_score=0.9, action_taken=Difficulty.HARD,
                         streak_momentum_after=0.0)
    # incorrect penalty dominates; no progress/momentum bonus
    assert rc.r_acc == -0.5
    assert rc.r_prog == 0.0
    assert rc.total < 0
