"""End-to-end simulation: drive a simulated learner through 80 questions using
the real state/prior/Q-learning/hybrid-policy classes (no DB — pure logic,
same composition the AdaptiveDifficultyController performs) and verify the
system-level properties required by the spec."""
import random

from app.services.adaptive.actions import ACTIONS, Difficulty
from app.services.adaptive.discretization import discretize
from app.services.adaptive.hybrid_policy import HybridAdaptivePolicy
from app.services.adaptive.q_learning import QLearningPolicy
from app.services.adaptive.reward import compute_reward
from app.services.adaptive.state import LearnerState
from app.services.adaptive.statistical_prior import StatisticalPrior

_RANK = {Difficulty.EASY: 0, Difficulty.MEDIUM: 1, Difficulty.HARD: 2}


def _simulate(n_steps=80, seed=7, true_ability=0.8):
    rng = random.Random(seed)
    state = LearnerState(user_id="sim", concept_id="c1")
    prior = StatisticalPrior()
    hybrid = HybridAdaptivePolicy()
    q_policy = QLearningPolicy(seed=seed)

    actions_seen = set()
    weights = []
    q_deltas = []

    for _ in range(n_steps):
        discrete = discretize(state)
        p_stat = prior.compute(state)
        p_rl = q_policy.action_probabilities(discrete.key)
        w = hybrid.blend_weight(state.confidence, hybrid.progress(state.timestep))
        pi = hybrid.blend(p_stat, p_rl, w)
        action = q_policy.sample(pi)

        assert action in ACTIONS
        actions_seen.add(action)
        weights.append(w)

        p_correct = max(0.05, min(0.95, true_ability - 0.2 * _RANK[action]))
        correct = rng.random() < p_correct
        response_time = rng.uniform(3, 20)

        state_key_before = discrete.key
        state.update(correct=correct, response_time_sec=response_time, action_taken=action)
        reward = compute_reward(correct=correct, speed_score=state.normalized_response_time,
                                 action_taken=action, streak_momentum_after=state.streak_momentum)
        next_discrete = discretize(state)
        q_before, q_after = q_policy.update(state_key_before, action, reward.total, next_discrete.key)
        q_policy.decay_epsilon()
        q_deltas.append(q_after - q_before)

    return state, q_policy, actions_seen, weights, q_deltas


def test_q_values_actually_change_over_the_run():
    _, _, _, _, q_deltas = _simulate()
    assert any(abs(d) > 1e-9 for d in q_deltas)


def test_learner_state_changes_over_the_run():
    state, *_ = _simulate()
    assert state.timestep == 80
    assert state.skill != 0.5
    assert state.confidence > 0.0


def test_difficulty_decisions_are_not_always_identical():
    _, _, actions_seen, _, _ = _simulate()
    assert len(actions_seen) > 1


def test_all_decisions_are_valid_actions():
    _, _, actions_seen, _, _ = _simulate()
    assert actions_seen.issubset(set(ACTIONS))


def test_rl_influence_increases_as_evidence_accumulates():
    _, _, _, weights, _ = _simulate()
    early = sum(weights[:10]) / 10
    late = sum(weights[-10:]) / 10
    assert late >= early


def test_high_ability_learner_ends_up_with_higher_skill_than_low_ability_learner():
    strong, *_ = _simulate(seed=1, true_ability=0.95)
    weak, *_ = _simulate(seed=2, true_ability=0.2)
    assert strong.skill > weak.skill
