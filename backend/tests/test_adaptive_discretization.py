"""Discretization-layer tests: deterministic mapping from the continuous 6-d
state to a bounded, human-readable discrete Q-table key."""
from app.services.adaptive.discretization import discretize
from app.services.adaptive.state import LearnerState


def test_discretize_is_deterministic():
    s = LearnerState(user_id="u", concept_id="c", skill=0.8, recent_accuracy=0.9,
                      normalized_response_time=0.1, streak_momentum=0.5,
                      learning_velocity=-0.1, confidence=0.9)
    d1 = discretize(s)
    d2 = discretize(s)
    assert d1.key == d2.key


def test_discretize_buckets_are_bounded():
    s = LearnerState(user_id="u", concept_id="c", skill=0.8, recent_accuracy=0.9,
                      normalized_response_time=0.1, streak_momentum=0.5,
                      learning_velocity=-0.1, confidence=0.9)
    d = discretize(s)
    for v in (d.skill, d.accuracy, d.response_time, d.streak, d.velocity, d.confidence):
        assert v in (0, 1, 2)


def test_discretize_labels_are_readable():
    s = LearnerState(user_id="u", concept_id="c", skill=0.9, recent_accuracy=0.05,
                      confidence=0.5)
    d = discretize(s)
    assert d.labels["skill"] == "HIGH"
    assert d.labels["accuracy"] == "LOW"
    assert d.labels["confidence"] == "MID"


def test_different_states_map_to_different_keys():
    s1 = LearnerState(user_id="u", concept_id="c", skill=0.1)
    s2 = LearnerState(user_id="u", concept_id="c", skill=0.9)
    assert discretize(s1).key != discretize(s2).key
