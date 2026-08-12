"""Tabular Q-learning tests: value updates, reward sensitivity, epsilon-greedy
exploration vs exploitation."""
from app.services.adaptive.actions import ACTIONS, Difficulty
from app.services.adaptive.config import AdaptiveConfig, QLearningConfig
from app.services.adaptive.q_learning import QLearningPolicy


def _policy(seed=1, epsilon=0.0, alpha=0.5, gamma=0.9):
    cfg = AdaptiveConfig(q=QLearningConfig(alpha=alpha, gamma=gamma, epsilon=epsilon,
                                            epsilon_decay=0.9, epsilon_min=0.01, seed=seed))
    return QLearningPolicy(cfg, seed=seed)


def test_q_values_change_after_interaction():
    p = _policy()
    before = p.q("s0", Difficulty.MEDIUM)
    p.update("s0", Difficulty.MEDIUM, reward=1.0, next_state_key="s1")
    after = p.q("s0", Difficulty.MEDIUM)
    assert after != before
    assert after > before  # positive reward should raise the Q-value


def test_reward_sign_drives_q_value_direction():
    p_pos = _policy()
    p_pos.update("s0", Difficulty.EASY, reward=1.0, next_state_key="s1")

    p_neg = _policy()
    p_neg.update("s0", Difficulty.EASY, reward=-1.0, next_state_key="s1")

    assert p_pos.q("s0", Difficulty.EASY) > p_neg.q("s0", Difficulty.EASY)


def test_update_uses_bellman_equation_exactly():
    p = _policy(alpha=0.5, gamma=0.9)
    p.q_table["s1"] = {"EASY": 0.0, "MEDIUM": 2.0, "HARD": 1.0}
    q_before, q_after = p.update("s0", Difficulty.MEDIUM, reward=1.0, next_state_key="s1")
    expected = q_before + 0.5 * (1.0 + 0.9 * 2.0 - q_before)
    assert abs(q_after - expected) < 1e-9


def test_repeated_positive_updates_converge_upward():
    p = _policy(alpha=0.3)
    values = []
    for _ in range(30):
        _, after = p.update("s0", Difficulty.HARD, reward=1.0, next_state_key="s0")
        values.append(after)
    assert values[-1] > values[0]
    assert values[-1] <= 1.0 / (1 - 0.9) + 1  # sane upper bound, not runaway


def test_epsilon_greedy_explores_when_epsilon_is_high():
    p = _policy(epsilon=1.0)
    p.q_table["s0"] = {"EASY": 0.0, "MEDIUM": 0.0, "HARD": 5.0}
    picks = {p.select_epsilon_greedy("s0") for _ in range(50)}
    assert len(picks) > 1  # full exploration should hit more than just the best action


def test_exploitation_chooses_best_learned_action():
    p = _policy(epsilon=0.0)
    p.q_table["s0"] = {"EASY": 0.1, "MEDIUM": 0.9, "HARD": 0.2}
    for _ in range(10):
        assert p.select_epsilon_greedy("s0") == Difficulty.MEDIUM


def test_action_probabilities_form_valid_distribution_and_favor_best():
    p = _policy(epsilon=0.3)
    p.q_table["s0"] = {"EASY": 0.1, "MEDIUM": 0.9, "HARD": 0.2}
    probs = p.action_probabilities("s0")
    assert set(probs.keys()) == set(ACTIONS)
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert probs[Difficulty.MEDIUM] > probs[Difficulty.EASY]
    assert probs[Difficulty.MEDIUM] > probs[Difficulty.HARD]


def test_epsilon_decay_reduces_epsilon_toward_minimum():
    p = _policy(epsilon=0.9)
    cfg = p.config.q
    for _ in range(200):
        p.decay_epsilon()
    assert abs(p.epsilon - cfg.epsilon_min) < 1e-6


def test_reproducible_with_fixed_seed():
    p1 = _policy(seed=123, epsilon=0.5)
    p2 = _policy(seed=123, epsilon=0.5)
    p1.q_table["s0"] = {"EASY": 0.0, "MEDIUM": 0.0, "HARD": 0.0}
    p2.q_table["s0"] = {"EASY": 0.0, "MEDIUM": 0.0, "HARD": 0.0}
    seq1 = [p1.select_epsilon_greedy("s0") for _ in range(20)]
    seq2 = [p2.select_epsilon_greedy("s0") for _ in range(20)]
    assert seq1 == seq2
