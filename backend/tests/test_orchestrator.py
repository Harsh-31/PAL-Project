"""Integration tests for AdaptiveLearningOrchestrator — verifies the three
subsystems (Hybrid RL, Process KG, Recommendation Engine) are composed
correctly, with no subsystem overriding another's authority.

RL algorithm internals (Q-learning, IRT prior, hybrid blend) are already
covered by test_adaptive_*.py, and recommender ranking internals by
test_recommender_challenge.py — these tests exercise the SEAM: does the
orchestrator call the right things, with the right data, in the right
order, and does it stay safe when a piece fails.
"""
import asyncio
from app.services import kg_service, recommender
from app.services.pal_agent import AdaptiveLearningOrchestrator
from app.services.adaptive.controller import AdaptiveDifficultyController, DifficultyDecision
from app.services.adaptive.actions import Difficulty
from app.services.adaptive.reward import RewardComponents
from app.services.adaptive.state import LearnerState
from app.services.adaptive import persistence as persistence_module
from app.services.threshold_rl.controller import ThresholdController
from tests.test_adaptive_persistence_integration import _FakeDB


def _returns(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


def _raises(exc):
    async def _fn(*args, **kwargs):
        raise exc
    return _fn


def _fake_decision(action: Difficulty, legacy_difficulty: int) -> DifficultyDecision:
    state = LearnerState(user_id="u1", concept_id="c1")
    even = {"EASY": 1 / 3, "MEDIUM": 1 / 3, "HARD": 1 / 3}
    return DifficultyDecision(
        action=action, legacy_difficulty=legacy_difficulty, state=state,
        discrete_state_key="sk1-ac1-rt1-st1-vl1-cf0",
        p_stat=dict(even), p_rl=dict(even), blend_weight=0.15,
        hybrid_policy=dict(even), q_values={"EASY": 0.0, "MEDIUM": 0.0, "HARD": 0.0},
    )


def _fake_outcome() -> dict:
    state = LearnerState(user_id="u1", concept_id="c1")
    state.timestep = 1
    return {
        "state": state,
        "reward": RewardComponents(r_acc=1.0, r_time=0.1, r_prog=0.0, r_mom=0.0),
        "q_value_before": 0.0, "q_value_after": 0.1,
        "next_discrete_state": "sk1-ac1-rt1-st1-vl1-cf0",
        "log_entry": {},
    }


def _patch_rl(monkeypatch, action: Difficulty, legacy_difficulty: int):
    """Force the RL controller's decisions to a known value so these tests
    exercise ORCHESTRATION logic, not RL's own (separately-tested) sampling."""
    decision = _fake_decision(action, legacy_difficulty)
    outcome = _fake_outcome()
    monkeypatch.setattr(AdaptiveDifficultyController, "select_difficulty", _returns(decision))
    monkeypatch.setattr(AdaptiveDifficultyController, "process_outcome", _returns(outcome))
    return decision, outcome


def _patch_kg(monkeypatch, *, mastery=0.5, new_mastery=0.5, attempts=1, intervention=None,
               previous_state=None):
    """`previous_state` defaults to None, which — since a real intervention's
    `state` is always a non-empty string — always reads as "changed" unless a
    test explicitly overrides it. That keeps every pre-existing test's
    assertions valid without each one having to opt in to the new
    state-transition gate; only the dedicated transition tests need to set it."""
    monkeypatch.setattr(kg_service, "get_mastery", _returns(mastery))
    monkeypatch.setattr(kg_service, "record_mastery", _returns(new_mastery))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(attempts))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(intervention))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns(previous_state))
    # Safe no-op defaults for the remediation lifecycle bookkeeping — real
    # Mongo access, unmocked, would fail and get silently swallowed by
    # process_attempt's own recommender-failure safety net, masking these
    # tests' actual assertions. Tests that care about lifecycle behavior
    # override these explicitly.
    monkeypatch.setattr(recommender, "record_active_recommendations", _returns(None))
    monkeypatch.setattr(recommender, "retire_recommendations_for_concept", _returns([]))
    _patch_kg_side_effects(monkeypatch)


def _patch_kg_side_effects(monkeypatch):
    """Stub the KG writes/reads process_attempt performs around the decision
    itself — the RL state write-through to the MASTERS edge and the two reads
    the Threshold RL needs. They all go straight to Neo4j, so without these
    every orchestration test dies on "Neo4j driver not initialised" before
    reaching its own assertions."""
    monkeypatch.setattr(kg_service, "sync_rl_state_to_edge", _returns(None))
    monkeypatch.setattr(kg_service, "get_last_cognitive_state", _returns("OnTrack"))
    monkeypatch.setattr(kg_service, "get_thresholds", _returns({"tau_timestep": 0}))
    monkeypatch.setattr(ThresholdController, "record_interaction", _returns(None))


def _run(coro):
    return asyncio.run(coro)


PENDING = {
    "action": "MEDIUM", "discrete_state_key": "sk1-ac1-rt1-st1-vl1-cf0",
    "p_stat": {}, "p_rl": {}, "blend_weight": 0.15, "hybrid_policy": {},
    "q_values": {}, "state_snapshot": LearnerState(user_id="u1", concept_id="c1").to_dict(),
}


# ---------------------------------------------------------------------------
# RL / KG independence — the most important property in the whole integration
# ---------------------------------------------------------------------------

def test_rl_difficulty_survives_kg_challenge_intervention(monkeypatch):
    """The core disagreement test from the spec: KG fires a challenge-type
    intervention (previously misread as 'raise difficulty') while RL
    independently picks EASY. Final difficulty must be EASY — the KG must
    never override it, and its own intervention must still fire unmodified."""
    _patch_rl(monkeypatch, Difficulty.EASY, 2)
    _patch_kg(monkeypatch, new_mastery=0.9, intervention={
        "state": "Mastered", "rule": "MasteredChallenge",
        "action": "offer_challenge_content", "thr": 0.85,
    })
    monkeypatch.setattr(recommender, "recommend_for_intervention", _returns([]))

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=True, current_difficulty=3,
        question_id="q1", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["next_difficulty"] == 2  # EASY — from RL, unmodified
    assert result["intervention"]["action"] == "offer_challenge_content"  # KG's own decision, also unmodified
    assert result["intervention"]["rule"] == "MasteredChallenge"


def test_select_difficulty_never_consults_kg_intervention_or_recommender(monkeypatch):
    """select_difficulty (the /generate-time call) must not touch the KG
    intervention lookup or the recommender at all — those only run in
    process_attempt, after an answer exists to react to."""
    _patch_kg(monkeypatch, mastery=0.4)
    intervention_called, recommend_called = [], []

    async def spy_intervention(*a, **kw):
        intervention_called.append(1)
        return None

    async def spy_recommend(*a, **kw):
        recommend_called.append(1)
        return []

    monkeypatch.setattr(kg_service, "get_intervention", spy_intervention)
    monkeypatch.setattr(recommender, "recommend_for_intervention", spy_recommend)
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.select_difficulty("u1", "c1", "intermediate"))

    assert result["difficulty"] == 3
    assert intervention_called == []
    assert recommend_called == []


# ---------------------------------------------------------------------------
# Content authority — recommender invoked only when the intervention needs it
# ---------------------------------------------------------------------------

def test_remediation_invokes_recommender_and_returns_results(monkeypatch):
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    _patch_kg(monkeypatch, new_mastery=0.3, intervention={
        "state": "Struggling", "rule": "AddRemedialContent",
        "action": "insert_prerequisite_video", "thr": 0.55,
    })
    fake_recs = [{"lecture_id": "lec-1", "title": "Prereq refresher", "similarity": 0.8}]
    calls = []

    async def fake_recommend(user_id, action, concept_id, mastery):
        calls.append((user_id, action, concept_id, mastery))
        return fake_recs

    monkeypatch.setattr(recommender, "recommend_for_intervention", fake_recommend)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=False, current_difficulty=3,
        question_id="q1", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["recommendations"] == fake_recs
    assert result["recommender_invoked"] is True
    assert calls == [("u1", "insert_prerequisite_video", "c1", 0.3)]


def test_continue_does_not_invoke_recommender(monkeypatch):
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    _patch_kg(monkeypatch, new_mastery=0.6, intervention={
        "state": "OnTrack", "rule": "ContinueBaseline", "action": "continue_normal", "thr": 0.7,
    })
    invoked = []

    async def fake_recommend(*a, **kw):
        invoked.append(1)
        return []

    monkeypatch.setattr(recommender, "recommend_for_intervention", fake_recommend)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=True, current_difficulty=3,
        question_id="q1", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["recommendations"] == []
    assert result["recommender_invoked"] is False
    assert invoked == []


def test_mastered_challenge_does_not_invoke_recommender(monkeypatch):
    """Mastered no longer 'skips' content (that rule was retired) — it offers
    challenge content instead, and that action must still never invoke the
    Recommendation Engine."""
    _patch_rl(monkeypatch, Difficulty.HARD, 4)
    _patch_kg(monkeypatch, new_mastery=0.97, intervention={
        "state": "Mastered", "rule": "MasteredChallenge", "action": "offer_challenge_content", "thr": 0.95,
    })
    monkeypatch.setattr(recommender, "recommend_for_intervention", _returns(["should not be reached"]))

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=True, current_difficulty=4,
        question_id="q1", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["recommendations"] == []
    assert result["recommender_invoked"] is False
    assert result["intervention"]["action"] == "offer_challenge_content"


def test_ontrack_never_invokes_recommender_while_rl_stays_independent(monkeypatch):
    """Final policy: OnTrack means 'performing well, continue normally' —
    it must NOT trigger enrichment content, even on a fresh state entry.
    RL still independently keeps choosing difficulty (HARD here, arbitrarily)."""
    _patch_rl(monkeypatch, Difficulty.HARD, 4)
    _patch_kg(monkeypatch, new_mastery=0.8, intervention={
        "state": "OnTrack", "rule": "OnTrackContinue",
        "action": "continue_normal", "thr": 0.85,
    })
    invoked = []

    async def spy(*a, **kw):
        invoked.append(1)
        return [{"lecture_id": "should-not-be-recommended"}]

    monkeypatch.setattr(recommender, "recommend_for_intervention", spy)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=True, current_difficulty=4,
        question_id="q1", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["recommendations"] == []
    assert result["recommender_invoked"] is False
    assert invoked == []
    assert result["next_difficulty"] == 4  # RL's own choice, not derived from the KG action


def test_no_recommendation_field_is_empty_list_not_none(monkeypatch):
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    _patch_kg(monkeypatch, new_mastery=0.6, intervention={
        "state": "OnTrack", "rule": "ContinueBaseline", "action": "continue_normal", "thr": 0.7,
    })
    monkeypatch.setattr(recommender, "recommend_for_intervention", _returns([]))

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=True, current_difficulty=3,
        question_id="q1", response_time_sec=5.0, pending=PENDING,
    ))
    assert result["recommendations"] == []
    assert isinstance(result["recommendations"], list)


# ---------------------------------------------------------------------------
# Failure safety
# ---------------------------------------------------------------------------

def test_recommender_failure_does_not_break_attempt_processing(monkeypatch):
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    _patch_kg(monkeypatch, new_mastery=0.3, intervention={
        "state": "Struggling", "rule": "AddRemedialContent",
        "action": "insert_prerequisite_video", "thr": 0.55,
    })
    monkeypatch.setattr(recommender, "recommend_for_intervention", _raises(RuntimeError("neo4j down")))

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=False, current_difficulty=3,
        question_id="q1", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["recommendations"] == []
    assert result["recommender_invoked"] is True
    assert result["recommender_failed"] is True
    # the rest of the loop still completed successfully despite the failure
    assert result["beliefs"]["mastery"] == 0.3
    assert result["next_difficulty"] == 3
    assert result["rl"]["q_value_after"] == 0.1


# ---------------------------------------------------------------------------
# Full realistic wiring — REAL RL controller (fake Mongo), mocked KG/recommender
# ---------------------------------------------------------------------------

def test_full_flow_with_real_rl_controller_and_mocked_kg_recommender(monkeypatch):
    """Uses the REAL AdaptiveDifficultyController (backed by an in-memory fake
    Mongo) so this proves the orchestrator's argument-wiring is correct
    against the actual RL implementation, not just a mock that ignores its
    inputs. KG and recommender are mocked (they need Neo4j, unavailable in
    unit tests) — RL algorithm behavior itself is covered by test_adaptive_*.py.
    """
    fake_db = _FakeDB()
    monkeypatch.setattr(persistence_module, "get_db", lambda: fake_db)
    _patch_kg(monkeypatch, new_mastery=0.3, intervention={
        "state": "Struggling", "rule": "AddRemedialContent",
        "action": "insert_prerequisite_video", "thr": 0.55,
    })
    fake_recs = [{"lecture_id": "lec-1", "title": "Prereq refresher"}]
    monkeypatch.setattr(recommender, "recommend_for_intervention", _returns(fake_recs))

    orch = AdaptiveLearningOrchestrator()

    decision_result = _run(orch.select_difficulty("u1", "c1", "intermediate"))
    pending = decision_result["decision"].to_pending()

    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=False,
        current_difficulty=decision_result["difficulty"],
        question_id="q1", response_time_sec=4.0, pending=pending,
    ))

    assert result["rl"]["q_value_before"] != result["rl"]["q_value_after"]
    assert result["recommendations"] == fake_recs
    assert result["beliefs"]["mastery"] == 0.3

    # A second, independent select_difficulty call reflects the persisted update.
    decision2 = _run(orch.select_difficulty("u1", "c1", "intermediate"))
    assert decision2["decision"].state.timestep == 1
