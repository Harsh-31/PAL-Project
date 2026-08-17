"""Tests for state-transition-gated recommendation triggering — the
Recommendation Engine must only run on ENTRY into a content-requiring
Process-KG cognitive state (e.g. OnTrack -> Struggling), never merely because
the learner remains in that state across consecutive answers
(Struggling -> Struggling), and never merely because Struggling's specific
firing rule changes (OfferSimplerAnalogy <-> AddRemedialContent) while the
state itself doesn't change.

Two layers are tested:
  1. kg_service.swap_intervention_state() itself — the real function, against
     a fake Neo4j session, proving its atomic read-old/write-new contract.
  2. AdaptiveLearningOrchestrator.process_attempt()'s use of that function —
     the 8 required scenarios from the integration spec, including one that
     walks a full realistic sequence across independent orchestrator calls
     (i.e. separate HTTP requests) to prove persistence.
"""
import asyncio
import pytest
from app.services import kg_service, recommender
from app.services.pal_agent import AdaptiveLearningOrchestrator
from app.services.adaptive.controller import AdaptiveDifficultyController
from app.services.adaptive.actions import Difficulty
from app.services.adaptive.state import LearnerState
from app.services.recommender import (
    record_active_recommendations as _real_record_active_recommendations,
    retire_recommendations_for_concept as _real_retire_recommendations_for_concept,
)
from tests.test_orchestrator import PENDING, _fake_decision, _fake_outcome, _returns
from tests.test_remediation_lifecycle import _FakeDB


@pytest.fixture(autouse=True)
def _safe_remediation_lifecycle_defaults(monkeypatch):
    """Every test in this file exercises process_attempt, which now also
    calls the remedial-recommendation lifecycle bookkeeping (record on
    trigger, retire on Mastered). Default both to safe no-ops so tests that
    don't care about lifecycle behavior aren't tripped up by real (unmocked)
    Mongo access; the dedicated lifecycle tests override these explicitly."""
    monkeypatch.setattr(recommender, "record_active_recommendations", _returns(None))
    monkeypatch.setattr(recommender, "retire_recommendations_for_concept", _returns([]))


def _run(coro):
    return asyncio.run(coro)


def _patch_rl(monkeypatch, action: Difficulty, legacy_difficulty: int):
    decision = _fake_decision(action, legacy_difficulty)
    outcome = _fake_outcome()
    monkeypatch.setattr(AdaptiveDifficultyController, "select_difficulty", _returns(decision))
    monkeypatch.setattr(AdaptiveDifficultyController, "process_outcome", _returns(outcome))


# Struggling has two real production rules (see neo4j_db._seed_process_kg):
# OfferSimplerAnalogy (priority 0) and AddRemedialContent (priority 1). Both
# fixtures below share the same "state": "Struggling" — only the rule/action
# differ — since that's what the real Process KG can actually return for this
# state. STRUGGLING is the one used by most tests here (least disruptive to
# existing assertions); STRUGGLING_ANALOGY is used where a test specifically
# needs the non-content-requiring analogy rule instead.
STRUGGLING = {"state": "Struggling", "rule": "AddRemedialContent",
              "action": "insert_prerequisite_video", "thr": 0.55}
STRUGGLING_ANALOGY = {"state": "Struggling", "rule": "OfferSimplerAnalogy",
                       "action": "simplify_with_hobby_analogy", "thr": 0.4}
ONTRACK = {"state": "OnTrack", "rule": "OnTrackContinue",
           "action": "continue_normal", "thr": 0.7}
MASTERED = {"state": "Mastered", "rule": "MasteredChallenge",
            "action": "offer_challenge_content", "thr": 0.95}


# ---------------------------------------------------------------------------
# Layer 1: kg_service.swap_intervention_state — the real function
# ---------------------------------------------------------------------------

class _FakeNeoRecord(dict):
    def __getitem__(self, key):
        return dict.get(self, key)


class _FakeNeoResult:
    def __init__(self, record):
        self._record = record

    async def single(self):
        return self._record


class _FakeNeoSession:
    """Reproduces swap_intervention_state's exact query contract: MERGE the
    edge, read the old last_kg_state, overwrite it with the new one — all in
    the single call the real Cypher statement performs atomically."""

    def __init__(self, store: dict):
        self.store = store

    async def run(self, query, **params):
        key = (params["uid"], params["cid"])
        previous = self.store.get(key)
        self.store[key] = params["new_state"]
        return _FakeNeoResult(_FakeNeoRecord(previous_state=previous))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeNeoDriver:
    def __init__(self):
        self.store = {}

    def session(self):
        return _FakeNeoSession(self.store)


def test_swap_intervention_state_first_call_returns_none(monkeypatch):
    driver = _FakeNeoDriver()
    monkeypatch.setattr(kg_service, "get_driver", lambda: driver)
    previous = _run(kg_service.swap_intervention_state("u1", "c1", "Struggling"))
    assert previous is None


def test_swap_intervention_state_persists_across_separate_calls(monkeypatch):
    """The core persistence guarantee: a SECOND, independent call sees what
    the FIRST call wrote — this is what makes state-transition detection
    survive separate HTTP requests rather than living in a Python variable."""
    driver = _FakeNeoDriver()
    monkeypatch.setattr(kg_service, "get_driver", lambda: driver)

    first = _run(kg_service.swap_intervention_state("u1", "c1", "Struggling"))
    second = _run(kg_service.swap_intervention_state("u1", "c1", "Struggling"))
    third = _run(kg_service.swap_intervention_state("u1", "c1", "OnTrack"))

    assert first is None
    assert second == "Struggling"
    assert third == "Struggling"


def test_swap_intervention_state_is_scoped_per_learner_and_concept(monkeypatch):
    driver = _FakeNeoDriver()
    monkeypatch.setattr(kg_service, "get_driver", lambda: driver)

    _run(kg_service.swap_intervention_state("u1", "c1", "Struggling"))
    # A different learner (or a different concept) has its own independent history.
    other_learner = _run(kg_service.swap_intervention_state("u2", "c1", "Mastered"))
    other_concept = _run(kg_service.swap_intervention_state("u1", "c2", "Mastered"))

    assert other_learner is None
    assert other_concept is None


# ---------------------------------------------------------------------------
# Layer 2: orchestrator-level scenarios (Tests 1-3, 5-7 from the spec)
# ---------------------------------------------------------------------------

def test_1_ontrack_to_struggling_triggers_recommendation(monkeypatch):
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.53))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(2))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(STRUGGLING))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("OnTrack"))
    fake_recs = [{"lecture_id": "remedial-1"}]
    monkeypatch.setattr(recommender, "recommend_for_intervention", _returns(fake_recs))

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=False, current_difficulty=3,
        question_id="q1", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["cognitive_state"] == {"current": "Struggling", "previous": "OnTrack", "changed": True}
    assert result["recommender_invoked"] is True
    assert result["recommendations"] == fake_recs


# ---------------------------------------------------------------------------
# Struggling has two real rules (OfferSimplerAnalogy, AddRemedialContent).
# The analogy rule is the one state where "needs a pedagogical response" and
# "needs the Recommendation Engine" diverge: the existing hobby-analogy
# mechanism already serves this moment, so the recommender must NEVER fire
# for it — not even on a fresh entry into Struggling, and not when the
# learner merely moves between the two Struggling rules without the
# cognitive state itself changing.
#
# NOTE: under the old (obsolete) 5-state model, "Frustrated" and "Struggling"
# were two separate cognitive states and moving between them counted as a
# real state transition. In the current 3-state model both rules live under
# the single "Struggling" state, so that specific transition no longer
# exists; the tests below were adapted to the closest still-meaningful real
# scenarios (see the per-test docstrings). A pure "same-state-persists"
# analogy-rule variant would have been a byte-for-byte duplicate of
# test_2_struggling_to_struggling_does_not_retrigger below and was removed.
# ---------------------------------------------------------------------------

def test_ontrack_to_struggling_with_analogy_rule_uses_analogy_without_recommender(monkeypatch):
    """Fresh entry into Struggling where the primary (priority-0) rule that
    fires is OfferSimplerAnalogy rather than AddRemedialContent — the
    recommender must not fire, even though this IS a state entry. Complements
    test_1_ontrack_to_struggling_triggers_recommendation, which uses the
    AddRemedialContent rule and DOES expect the recommender to fire — proving
    the gate is keyed on the specific action, not just "entered Struggling"."""
    _patch_rl(monkeypatch, Difficulty.EASY, 2)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.35))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(2))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(STRUGGLING_ANALOGY))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("OnTrack"))
    invoked = []

    async def spy(*a, **kw):
        invoked.append(1)
        return []

    monkeypatch.setattr(recommender, "recommend_for_intervention", spy)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=False, current_difficulty=2,
        question_id="qf1", response_time_sec=5.0, pending=PENDING,
    ))

    # Cognitive state genuinely changed (a fresh entry)...
    assert result["cognitive_state"] == {"current": "Struggling", "previous": "OnTrack", "changed": True}
    # ...the analogy intervention is preserved and reported...
    assert result["intervention"]["action"] == "simplify_with_hobby_analogy"
    assert result["intervention"]["rule"] == "OfferSimplerAnalogy"
    # ...but the recommender must not fire, even though this IS a state entry.
    assert result["recommender_invoked"] is False
    assert result["recommendations"] == []
    assert invoked == []


def test_mastered_to_struggling_now_invokes_remedial_recommender(monkeypatch):
    """A big regression straight from Mastered into Struggling (skipping
    OnTrack entirely) is still a fresh entry into a content-requiring state —
    the recommender must fire, exactly once, for the remedial (not analogy)
    content strategy."""
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.45))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(4))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(STRUGGLING))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("Mastered"))
    fake_recs = [{"lecture_id": "remedial-x"}]
    calls = []

    async def spy(user_id, action, concept_id, mastery):
        calls.append(action)
        return fake_recs

    monkeypatch.setattr(recommender, "recommend_for_intervention", spy)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=True, current_difficulty=2,
        question_id="qf3", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["cognitive_state"] == {"current": "Struggling", "previous": "Mastered", "changed": True}
    assert result["recommender_invoked"] is True
    assert calls == ["insert_prerequisite_video"]
    assert result["recommendations"] == fake_recs


def test_mastered_to_struggling_with_analogy_uses_analogy_without_recommender(monkeypatch):
    """Same big regression (Mastered -> Struggling), but the analogy rule
    fires instead of the remedial one — recommender must stay silent."""
    _patch_rl(monkeypatch, Difficulty.EASY, 2)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.38))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(5))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(STRUGGLING_ANALOGY))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("Mastered"))
    invoked = []

    async def spy(*a, **kw):
        invoked.append(1)
        return []

    monkeypatch.setattr(recommender, "recommend_for_intervention", spy)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=False, current_difficulty=2,
        question_id="qf4", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["cognitive_state"] == {"current": "Struggling", "previous": "Mastered", "changed": True}
    assert result["intervention"]["action"] == "simplify_with_hobby_analogy"
    assert result["recommender_invoked"] is False
    assert invoked == []


def test_rl_difficulty_independent_when_struggling_with_analogy(monkeypatch):
    """RL keeps choosing whatever it chooses (HARD here, deliberately) even
    while the learner is in Struggling via the analogy rule — the
    intervention must not suppress, override, or otherwise couple to the
    difficulty decision."""
    _patch_rl(monkeypatch, Difficulty.HARD, 4)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.30))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(2))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(STRUGGLING_ANALOGY))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("OnTrack"))
    monkeypatch.setattr(recommender, "recommend_for_intervention", _returns([]))

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=False, current_difficulty=3,
        question_id="qf5", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["next_difficulty"] == 4  # RL's own choice, unaffected by the analogy rule
    assert result["intervention"]["action"] == "simplify_with_hobby_analogy"
    assert result["recommender_invoked"] is False


def test_2_struggling_to_struggling_does_not_retrigger(monkeypatch):
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.48))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(3))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(STRUGGLING))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("Struggling"))
    invoked = []

    async def spy(*a, **kw):
        invoked.append(1)
        return []

    monkeypatch.setattr(recommender, "recommend_for_intervention", spy)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=False, current_difficulty=3,
        question_id="q2", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["cognitive_state"]["changed"] is False
    assert result["recommender_invoked"] is False
    assert result["recommendations"] == []
    assert invoked == []


def test_3_struggling_to_ontrack_resets_without_recommendation(monkeypatch):
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.60))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(4))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(ONTRACK))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("Struggling"))
    invoked = []

    async def spy(*a, **kw):
        invoked.append(1)
        return []

    monkeypatch.setattr(recommender, "recommend_for_intervention", spy)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=True, current_difficulty=3,
        question_id="q3", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["cognitive_state"] == {"current": "OnTrack", "previous": "Struggling", "changed": True}
    assert result["recommender_invoked"] is False
    assert invoked == []


def test_5_ontrack_to_mastered_does_not_trigger_recommender(monkeypatch):
    """Test F (final policy): Mastered is entered fresh, but must NOT
    trigger the Recommendation Engine or any enrichment content — it offers
    challenge content directly, not via the Recommendation Engine. RL still
    independently selects difficulty.

    (Under the old 5-state model this scenario was "OnTrack -> Confident";
    Confident's role — the tier reached from OnTrack that offers challenge
    content — is what Mastered does in the current 3-state model.)"""
    _patch_rl(monkeypatch, Difficulty.HARD, 4)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.78))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(5))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(MASTERED))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("OnTrack"))
    invoked = []

    async def spy(*a, **kw):
        invoked.append(1)
        return [{"lecture_id": "should-not-be-recommended"}]

    monkeypatch.setattr(recommender, "recommend_for_intervention", spy)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=True, current_difficulty=3,
        question_id="q4", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["cognitive_state"] == {"current": "Mastered", "previous": "OnTrack", "changed": True}
    assert result["recommender_invoked"] is False
    assert result["recommendations"] == []
    assert invoked == []
    assert result["next_difficulty"] == 4  # RL still independently controls difficulty


def test_6_mastered_repeated_three_times_zero_recommender_calls(monkeypatch):
    """Test G: Mastered -> Mastered -> Mastered. Zero recommendation calls
    and zero enrichment content across all three interactions."""
    _patch_rl(monkeypatch, Difficulty.HARD, 4)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.80))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(6))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(MASTERED))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("Mastered"))
    invoked = []

    async def spy(*a, **kw):
        invoked.append(1)
        return [{"lecture_id": "should-not-be-recommended"}]

    monkeypatch.setattr(recommender, "recommend_for_intervention", spy)

    orch = AdaptiveLearningOrchestrator()
    for question_id in ["q5a", "q5b", "q5c"]:
        result = _run(orch.process_attempt(
            user_id="u1", concept_id="c1", correct=True, current_difficulty=4,
            question_id=question_id, response_time_sec=5.0, pending=PENDING,
        ))
        assert result["cognitive_state"]["changed"] is False
        assert result["recommender_invoked"] is False
        assert result["recommendations"] == []

    assert invoked == []  # zero calls across all three interactions


def test_7_rl_and_kg_independence_survives_state_gating(monkeypatch):
    """Test H: Process KG says Mastered (state just entered) while Hybrid RL
    independently selects EASY. Neither overrides the other; Mastered
    triggers no enrichment recommendation either way (it offers challenge
    content directly, not via the Recommendation Engine)."""
    _patch_rl(monkeypatch, Difficulty.EASY, 2)
    monkeypatch.setattr(kg_service, "record_mastery", _returns(0.90))
    monkeypatch.setattr(kg_service, "get_attempts", _returns(7))
    monkeypatch.setattr(kg_service, "get_intervention", _returns(MASTERED))
    monkeypatch.setattr(kg_service, "swap_intervention_state", _returns("OnTrack"))
    invoked = []

    async def spy(*a, **kw):
        invoked.append(1)
        return [{"lecture_id": "should-not-be-recommended"}]

    monkeypatch.setattr(recommender, "recommend_for_intervention", spy)

    orch = AdaptiveLearningOrchestrator()
    result = _run(orch.process_attempt(
        user_id="u1", concept_id="c1", correct=True, current_difficulty=3,
        question_id="q6", response_time_sec=5.0, pending=PENDING,
    ))

    assert result["next_difficulty"] == 2  # EASY — RL's own choice, unmodified
    assert result["intervention"]["action"] == "offer_challenge_content"  # KG's own choice, unmodified
    assert result["cognitive_state"]["changed"] is True
    assert result["recommender_invoked"] is False  # Mastered never triggers content via the recommender
    assert invoked == []


# ---------------------------------------------------------------------------
# Test 4 + Test 8: full realistic sequence across independent orchestrator
# calls (simulating separate HTTP requests), proving both re-entry (Test 4)
# and cross-request persistence (Test 8) together.
# ---------------------------------------------------------------------------

class _SequentialKGState:
    """In-memory stand-in with the exact same semantics as the real
    kg_service.swap_intervention_state — used here (rather than a static
    _returns(...)) so each call in the sequence genuinely depends on what the
    PREVIOUS, separate call persisted, exactly as separate HTTP requests would
    behave against the real Neo4j-backed MASTERS edge."""

    def __init__(self):
        self.store: dict = {}

    async def swap(self, user_id, concept_id, new_state):
        key = (user_id, concept_id)
        previous = self.store.get(key)
        self.store[key] = new_state
        return previous


def test_4_and_8_full_transition_sequence_across_separate_calls(monkeypatch):
    """OnTrack -> Struggling (trigger) -> Struggling -> Struggling (no
    retrigger, x2) -> OnTrack (reset, no recommendation) -> Struggling again
    (new entry, triggers again). Each step is an independent
    orchestrator.process_attempt() call sharing nothing but the persisted
    kg_state store — i.e. exactly what happens across separate HTTP requests."""
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    kg_state = _SequentialKGState()
    kg_state.store[("u1", "c1")] = "OnTrack"  # learner's state before this sequence begins
    monkeypatch.setattr(kg_service, "swap_intervention_state", kg_state.swap)
    monkeypatch.setattr(kg_service, "get_attempts", _returns(1))

    invocations = []

    async def counting_recommend(user_id, action, concept_id, mastery):
        invocations.append(action)
        return [{"lecture_id": f"rec-{len(invocations)}"}]

    monkeypatch.setattr(recommender, "recommend_for_intervention", counting_recommend)

    orch = AdaptiveLearningOrchestrator()

    def _attempt(new_mastery, intervention):
        monkeypatch.setattr(kg_service, "record_mastery", _returns(new_mastery))
        monkeypatch.setattr(kg_service, "get_intervention", _returns(intervention))
        return _run(orch.process_attempt(
            user_id="u1", concept_id="c1", correct=False, current_difficulty=3,
            question_id="q", response_time_sec=5.0, pending=PENDING,
        ))

    # Step 1: OnTrack -> Struggling (new entry -> trigger)
    r1 = _attempt(0.53, STRUGGLING)
    assert r1["cognitive_state"] == {"current": "Struggling", "previous": "OnTrack", "changed": True}
    assert r1["recommender_invoked"] is True

    # Step 2: Struggling -> Struggling (still struggling -> no retrigger)
    r2 = _attempt(0.51, STRUGGLING)
    assert r2["cognitive_state"]["changed"] is False
    assert r2["recommender_invoked"] is False

    # Step 3: Struggling -> Struggling again (still no retrigger)
    r3 = _attempt(0.48, STRUGGLING)
    assert r3["cognitive_state"]["changed"] is False
    assert r3["recommender_invoked"] is False

    # Step 4: Struggling -> OnTrack (exit remediation -> reset, no recommendation)
    r4 = _attempt(0.58, ONTRACK)
    assert r4["cognitive_state"] == {"current": "OnTrack", "previous": "Struggling", "changed": True}
    assert r4["recommender_invoked"] is False

    # Step 5: OnTrack -> Struggling AGAIN (new entry after reset -> trigger again)
    r5 = _attempt(0.50, STRUGGLING)
    assert r5["cognitive_state"] == {"current": "Struggling", "previous": "OnTrack", "changed": True}
    assert r5["recommender_invoked"] is True

    # Exactly two recommendation calls across the whole 5-step sequence.
    assert invocations == ["insert_prerequisite_video", "insert_prerequisite_video"]


def test_struggling_analogy_remedial_alternation_sequence_across_separate_calls(monkeypatch):
    """Struggling has two real rules (AddRemedialContent, OfferSimplerAnalogy).
    This proves the recommender gate is keyed on the cognitive STATE changing,
    not on which of Struggling's two rules currently fires: OnTrack ->
    Struggling (remedial rule, triggers) -> Struggling (analogy rule now
    fires instead, but the state itself hasn't changed -> no retrigger) ->
    Struggling (analogy rule persists -> still no recommendation). Each step
    is an independent orchestrator call against the same persisted kg_state.

    (Under the old 5-state model this was the Frustrated<->Struggling
    narrative; in the current 3-state model both rules live under the single
    Struggling state, so switching between them is not a state transition.)"""
    _patch_rl(monkeypatch, Difficulty.EASY, 2)
    kg_state = _SequentialKGState()
    kg_state.store[("u1", "c1")] = "OnTrack"  # learner's state before this sequence begins
    monkeypatch.setattr(kg_service, "swap_intervention_state", kg_state.swap)
    monkeypatch.setattr(kg_service, "get_attempts", _returns(1))

    invocations = []

    async def counting_recommend(user_id, action, concept_id, mastery):
        invocations.append(action)
        return [{"lecture_id": f"rec-{len(invocations)}"}]

    monkeypatch.setattr(recommender, "recommend_for_intervention", counting_recommend)

    orch = AdaptiveLearningOrchestrator()

    def _attempt(new_mastery, intervention):
        monkeypatch.setattr(kg_service, "record_mastery", _returns(new_mastery))
        monkeypatch.setattr(kg_service, "get_intervention", _returns(intervention))
        return _run(orch.process_attempt(
            user_id="u1", concept_id="c1", correct=False, current_difficulty=2,
            question_id="q", response_time_sec=5.0, pending=PENDING,
        ))

    # Step 1: OnTrack -> Struggling (remedial rule) — a real content-requiring entry, triggers.
    r1 = _attempt(0.45, STRUGGLING)
    assert r1["cognitive_state"] == {"current": "Struggling", "previous": "OnTrack", "changed": True}
    assert r1["recommender_invoked"] is True

    # Step 2: Struggling -> Struggling, but the analogy rule fires instead of
    # the remedial one. The cognitive state itself did not change, so no
    # retrigger — regardless of which rule/action is currently returned.
    r2 = _attempt(0.38, STRUGGLING_ANALOGY)
    assert r2["cognitive_state"]["changed"] is False
    assert r2["intervention"]["action"] == "simplify_with_hobby_analogy"
    assert r2["recommender_invoked"] is False

    # Step 3: Struggling (analogy) persists — still no recommendation.
    r3 = _attempt(0.33, STRUGGLING_ANALOGY)
    assert r3["cognitive_state"]["changed"] is False
    assert r3["recommender_invoked"] is False

    # Exactly one recommendation call across the whole 3-step sequence (step 1 only).
    assert invocations == ["insert_prerequisite_video"]


# ---------------------------------------------------------------------------
# Test D + Test E: remediation lifecycle end-to-end through the orchestrator,
# using the REAL record_active_recommendations/retire_recommendations_for_concept
# (overriding this file's autouse no-op defaults) against the fake Mongo
# collection from test_remediation_lifecycle.py.
# ---------------------------------------------------------------------------

def _use_real_remediation_lifecycle(monkeypatch):
    """Override this file's autouse no-op defaults so the REAL lifecycle
    functions run, against an isolated fake Mongo DB. Returns that fake DB."""
    fake_db = _FakeDB()
    monkeypatch.setattr(recommender, "get_db", lambda: fake_db)
    monkeypatch.setattr(recommender, "record_active_recommendations", _real_record_active_recommendations)
    monkeypatch.setattr(recommender, "retire_recommendations_for_concept", _real_retire_recommendations_for_concept)
    return fake_db


def test_D_recovery_preserves_active_remediation_until_mastered(monkeypatch):
    """Test D: Struggling -> OnTrack. No new remediation, no duplicate
    recommendation — AND the remediation recorded while Struggling stays
    ACTIVE (recovery to OnTrack is not a retirement condition; only Mastered
    retires it, see Test E)."""
    _patch_rl(monkeypatch, Difficulty.MEDIUM, 3)
    fake_db = _use_real_remediation_lifecycle(monkeypatch)
    monkeypatch.setattr(kg_service, "get_attempts", _returns(1))
    monkeypatch.setattr(recommender, "recommend_for_intervention", _returns([{"lecture_id": "remedial-1"}]))

    orch = AdaptiveLearningOrchestrator()

    def _attempt(new_mastery, intervention, previous_state, correct):
        monkeypatch.setattr(kg_service, "record_mastery", _returns(new_mastery))
        monkeypatch.setattr(kg_service, "get_intervention", _returns(intervention))
        monkeypatch.setattr(kg_service, "swap_intervention_state", _returns(previous_state))
        return _run(orch.process_attempt(
            user_id="u1", concept_id="c1", correct=correct, current_difficulty=3,
            question_id="q", response_time_sec=5.0, pending=PENDING,
        ))

    # OnTrack -> Struggling: triggers remediation, recorded active.
    r1 = _attempt(0.53, STRUGGLING, previous_state="OnTrack", correct=False)
    assert r1["recommender_invoked"] is True
    assert len(fake_db.remedial_recommendations.docs) == 1
    assert fake_db.remedial_recommendations.docs[0]["status"] == "active"

    # Struggling -> OnTrack: recovery. No new remediation, nothing retired,
    # the earlier active row is untouched.
    r2 = _attempt(0.60, ONTRACK, previous_state="Struggling", correct=True)
    assert r2["recommender_invoked"] is False
    assert r2["retired_lecture_ids"] == []
    assert fake_db.remedial_recommendations.docs[0]["status"] == "active"


def test_E_mastered_retires_pending_remediation_preserves_unrelated(monkeypatch):
    """Test E: create a pending remedial recommendation, then move the
    learner to Mastered. Expect it retired and returned in
    retired_lecture_ids (so the frontend can remove the sidebar card);
    unrelated concepts' active recommendations must be preserved."""
    _patch_rl(monkeypatch, Difficulty.EASY, 2)
    fake_db = _use_real_remediation_lifecycle(monkeypatch)
    monkeypatch.setattr(kg_service, "get_attempts", _returns(1))
    monkeypatch.setattr(recommender, "recommend_for_intervention", _returns([{"lecture_id": "remedial-1"}]))

    # An unrelated concept's active recommendation must survive untouched.
    _run(recommender.record_active_recommendations("u1", "other-concept", [{"lecture_id": "unrelated-lec"}]))

    orch = AdaptiveLearningOrchestrator()

    def _attempt(new_mastery, intervention, previous_state, correct):
        monkeypatch.setattr(kg_service, "record_mastery", _returns(new_mastery))
        monkeypatch.setattr(kg_service, "get_intervention", _returns(intervention))
        monkeypatch.setattr(kg_service, "swap_intervention_state", _returns(previous_state))
        return _run(orch.process_attempt(
            user_id="u1", concept_id="c1", correct=correct, current_difficulty=3,
            question_id="q", response_time_sec=5.0, pending=PENDING,
        ))

    # OnTrack -> Struggling: creates the pending remediation for c1.
    r1 = _attempt(0.50, STRUGGLING, previous_state="OnTrack", correct=False)
    assert r1["recommender_invoked"] is True

    # Struggling -> Mastered: retires c1's remediation.
    r2 = _attempt(0.97, MASTERED, previous_state="Struggling", correct=True)
    assert r2["retired_lecture_ids"] == ["remedial-1"]
    assert r2["recommender_invoked"] is False  # Mastered triggers nothing new itself

    c1_row = next(r for r in fake_db.remedial_recommendations.docs if r["concept_id"] == "c1")
    assert c1_row["status"] == "retired"
    other_row = next(r for r in fake_db.remedial_recommendations.docs if r["concept_id"] == "other-concept")
    assert other_row["status"] == "active"  # unrelated content preserved
