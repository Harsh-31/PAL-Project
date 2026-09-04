"""Learner-state (x_t) initialization and update-rule tests."""
from app.services.adaptive.actions import Difficulty
from app.services.adaptive.config import DEFAULT_CONFIG
from app.services.adaptive.state import LearnerState


def test_state_initialization_is_neutral_not_random():
    s = LearnerState(user_id="u1", concept_id="c1")
    assert s.timestep == 0
    assert s.skill == 0.5
    assert s.recent_accuracy == 0.5
    assert s.normalized_response_time == 0.5
    assert s.streak_momentum == 0.0
    assert s.learning_velocity == 0.0
    assert s.confidence == 0.0  # no evidence yet


def test_update_after_correct_answer():
    s = LearnerState(user_id="u1", concept_id="c1")
    s.update(correct=True, response_time_sec=10.0, action_taken=Difficulty.MEDIUM)
    assert s.timestep == 1
    assert s.skill > 0.5          # ability estimate moves up
    assert s.recent_accuracy == 1.0
    assert s.current_streak == 1
    assert s.streak_momentum > 0
    assert s.last_action == "MEDIUM"


def test_update_after_incorrect_answer():
    s = LearnerState(user_id="u1", concept_id="c1")
    s.update(correct=False, response_time_sec=10.0, action_taken=Difficulty.MEDIUM)
    assert s.skill < 0.5
    assert s.recent_accuracy == 0.0
    assert s.current_streak == -1
    assert s.streak_momentum < 0


def test_recent_accuracy_uses_configured_window():
    s = LearnerState(user_id="u1", concept_id="c1")
    sequence = [True, True, True, False, False, False, False]
    for correct in sequence:
        s.update(correct=correct, response_time_sec=5.0, action_taken=Difficulty.MEDIUM)
    window = DEFAULT_CONFIG.state.recent_window
    expected = sum(sequence[-window:]) / window
    assert abs(s.recent_accuracy - expected) < 1e-9


def test_response_time_normalization_relative_to_history():
    s = LearnerState(user_id="u1", concept_id="c1")
    for _ in range(6):
        s.update(correct=True, response_time_sec=10.0, action_taken=Difficulty.MEDIUM)
    # much faster than the learner's own history -> normalized toward 1 (fast)
    s.update(correct=True, response_time_sec=1.0, action_taken=Difficulty.MEDIUM)
    assert s.normalized_response_time > 0.5
    # much slower than history -> normalized toward 0 (slow)
    s.update(correct=True, response_time_sec=200.0, action_taken=Difficulty.MEDIUM)
    assert s.normalized_response_time < 0.5


def test_confidence_calculation_grows_with_consistent_evidence():
    s = LearnerState(user_id="u1", concept_id="c1")
    confidences = []
    for _ in range(15):
        s.update(correct=True, response_time_sec=8.0, action_taken=Difficulty.MEDIUM)
        confidences.append(s.confidence)
    assert confidences[-1] > confidences[0]
    assert 0.0 <= confidences[-1] <= 1.0


def test_confidence_drops_with_inconsistent_evidence():
    s = LearnerState(user_id="u1", concept_id="c1")
    for i in range(10):
        s.update(correct=(i % 2 == 0), response_time_sec=8.0, action_taken=Difficulty.MEDIUM)
    erratic_confidence = s.confidence

    s2 = LearnerState(user_id="u2", concept_id="c1")
    for _ in range(10):
        s2.update(correct=True, response_time_sec=8.0, action_taken=Difficulty.MEDIUM)
    consistent_confidence = s2.confidence

    assert consistent_confidence > erratic_confidence


def test_learning_velocity_positive_when_improving():
    s = LearnerState(user_id="u1", concept_id="c1")
    for _ in range(5):
        s.update(correct=False, response_time_sec=8.0, action_taken=Difficulty.MEDIUM)
    for _ in range(5):
        s.update(correct=True, response_time_sec=8.0, action_taken=Difficulty.MEDIUM)
    assert s.learning_velocity > 0


def test_learning_velocity_negative_when_declining():
    s = LearnerState(user_id="u1", concept_id="c1")
    for _ in range(5):
        s.update(correct=True, response_time_sec=8.0, action_taken=Difficulty.MEDIUM)
    for _ in range(5):
        s.update(correct=False, response_time_sec=8.0, action_taken=Difficulty.MEDIUM)
    assert s.learning_velocity < 0


def test_to_dict_from_dict_roundtrip():
    s = LearnerState(user_id="u1", concept_id="c1")
    s.update(correct=True, response_time_sec=7.0, action_taken=Difficulty.HARD)
    restored = LearnerState.from_dict(s.to_dict())
    assert restored.as_vector() == s.as_vector()
    assert restored.last_action == s.last_action
    assert restored.timestep == s.timestep
