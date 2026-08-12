"""2PL IRT-style statistical prior tests: distribution validity, monotonicity
in skill, promote/demote stability thresholds, and cooldown/hold behavior."""
from app.services.adaptive.actions import ACTIONS, Difficulty
from app.services.adaptive.state import LearnerState
from app.services.adaptive.statistical_prior import StatisticalPrior


def test_probability_distribution_sums_to_one():
    prior = StatisticalPrior()
    s = LearnerState(user_id="u", concept_id="c", skill=0.5)
    probs = prior.compute(s)
    assert set(probs.keys()) == set(ACTIONS)
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert all(0.0 <= v <= 1.0 for v in probs.values())


def test_increasing_skill_increases_probability_of_harder_questions():
    prior = StatisticalPrior()
    low_skill = LearnerState(user_id="u", concept_id="c", skill=0.15)
    high_skill = LearnerState(user_id="u", concept_id="c", skill=0.9)
    p_low = prior.compute(low_skill)
    p_high = prior.compute(high_skill)
    assert p_high[Difficulty.HARD] > p_low[Difficulty.HARD]
    assert p_high[Difficulty.EASY] < p_low[Difficulty.EASY]


def test_promote_threshold_shifts_mass_toward_harder_level():
    prior = StatisticalPrior()
    baseline = prior._irt_probabilities(0.0)  # theta=0 <-> skill=0.5
    s = LearnerState(user_id="u", concept_id="c", skill=0.5, recent_accuracy=0.9,
                      last_action=Difficulty.MEDIUM.value, steps_since_action_change=10)
    probs = prior.compute(s)
    assert probs[Difficulty.HARD] > baseline[Difficulty.HARD]


def test_demote_threshold_shifts_mass_toward_easier_level():
    prior = StatisticalPrior()
    s = LearnerState(user_id="u", concept_id="c", skill=0.5, recent_accuracy=0.1,
                      last_action=Difficulty.MEDIUM.value, steps_since_action_change=10)
    probs = prior.compute(s)
    assert probs[Difficulty.EASY] > probs[Difficulty.HARD]


def test_thresholds_do_not_hard_select_a_single_difficulty():
    """Even after a strong promote nudge, this must remain a real distribution
    (every action retains nonzero probability mass), not a one-hot choice."""
    prior = StatisticalPrior()
    s = LearnerState(user_id="u", concept_id="c", skill=0.5, recent_accuracy=0.95,
                      last_action=Difficulty.MEDIUM.value, steps_since_action_change=10)
    probs = prior.compute(s)
    assert all(v > 0.0 for v in probs.values())


def test_cooldown_biases_toward_holding_current_level():
    prior = StatisticalPrior()
    # High accuracy would normally promote, but we're still inside cooldown
    # (steps_since_action_change=0 < cooldown_steps) right after a change.
    s = LearnerState(user_id="u", concept_id="c", skill=0.5, recent_accuracy=0.95,
                      last_action=Difficulty.EASY.value, steps_since_action_change=0)
    probs = prior.compute(s)
    assert probs[Difficulty.EASY] > probs[Difficulty.HARD]


def test_cooldown_expires_and_allows_promotion():
    prior = StatisticalPrior()
    s = LearnerState(user_id="u", concept_id="c", skill=0.5, recent_accuracy=0.95,
                      last_action=Difficulty.EASY.value, steps_since_action_change=99)
    probs = prior.compute(s)
    baseline = prior._irt_probabilities(0.0)
    assert probs[Difficulty.MEDIUM] > baseline[Difficulty.MEDIUM]
