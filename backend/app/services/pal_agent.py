"""AdaptiveLearningOrchestrator — the single coordination point for PAL's
three independent adaptive subsystems.

Responsibility boundaries (do not blur these — each system is the SOLE
authority for its own decision):

  Hybrid RL       (app.services.adaptive)    -> "How hard should the next
                                                  question be?" Easy/Medium/Hard.
  Process KG      (app.services.kg_service)  -> "What pedagogical intervention
                                                  fits the learner's current
                                                  mastery?" Continue / Remediate /
                                                  Analogy / Challenge / Skip.
  Recommendation  (app.services.recommender) -> "Which content resource(s)
                                                  satisfy that intervention,
                                                  if it needs any?"

This module does not implement a new reasoning/planning/memory system — it
sequences three already-independent, already-tested subsystems and composes
their outputs into one response. Process KG never overrides the RL difficulty
decision, and RL never decides an intervention. See README "Hybrid Reinforcement
Learning Adaptation" and the Process KG docs for each subsystem's own detail.

Recommendations trigger on STATE ENTRY, not on every interaction: the
Recommendation Engine only runs when the Process KG's cognitive state just
CHANGED to one that needs content (e.g. OnTrack -> Struggling), not merely
because the learner remains in that state across consecutive answers
(Struggling -> Struggling). The previous state is persisted on the same
Neo4j MASTERS edge record_mastery already writes (see
kg_service.swap_intervention_state), so this survives separate HTTP requests.

Only Struggling ever triggers a recommendation (remedial content). Confident
does NOT — it means "performing well, continue normally," nothing more; RL
may still independently pick a harder question, but that's RL's decision
alone. When a concept reaches Mastered, any still-active remedial
recommendation for that concept is retired (recommender.
retire_recommendations_for_concept) — this orchestrator coordinates that
retirement, it does not implement the recommendation ranking itself.

The normal answer explanation and the wrong-answer analogy are NOT part of
this orchestrator — they are "immediate answer feedback," generated directly
in routes/quiz.py, independent of Process KG state, RL, and video playback.
This orchestrator must never generate explanations itself.
"""
from datetime import datetime, timezone
from app.services import kg_service, recommender
from app.services.adaptive.controller import AdaptiveDifficultyController

_BASELINE_SKILL = {"beginner": 0.35, "intermediate": 0.5, "advanced": 0.65}

# Which Process-KG intervention actions require the Recommendation Engine to
# supply external content. This mapping is the ONLY place "does this
# intervention need content" is decided — plain orchestration data, not a new
# rule engine. Actions not listed here never invoke the recommender. Even for
# actions marked True here, the recommender only actually runs on a fresh
# ENTRY into that state (see _NEEDS_CONTENT usage in process_attempt below).
_NEEDS_CONTENT = {
    # Frustrated is immediate simplification/support via the existing hobby-
    # analogy mechanism, not additional learning material — the recommender
    # must NOT also fire here, or the learner gets two overlapping
    # interventions for the same moment.
    "simplify_with_hobby_analogy": False,  # Frustrated -> OfferSimplerAnalogy
    "insert_prerequisite_video": True,     # Struggling -> AddRemedialContent
    # Confident means "performing well, continue normally" — it does not add
    # enrichment content. RL may still independently pick a harder question;
    # that's RL's decision alone, never triggered by this map.
    "offer_challenge_content": False,      # Confident -> AdvanceDifficulty (reinterpreted; see neo4j_db.py)
    "continue_normal": False,
    "skip_next_similar_chunk": False,      # skipping doesn't itself create a content gap
}


class AdaptiveLearningOrchestrator:
    """Composes StatisticalPrior+Q-learning (via AdaptiveDifficultyController),
    the Process KG, and the Recommendation Engine into one adaptive decision
    per quiz interaction."""

    def __init__(self):
        self._rl = AdaptiveDifficultyController()

    # -------------------------------------------------------------------
    async def select_difficulty(self, user_id: str, concept_id: str, baseline: str) -> dict:
        """Per-question difficulty decision (called from /api/quiz/generate).

        Hybrid RL is the sole authority here — the Process KG and the
        recommender are not consulted for this decision at all.
        """
        mastery = await kg_service.get_mastery(user_id, concept_id)
        baseline_skill = (_BASELINE_SKILL.get(baseline, 0.5) + mastery) / 2.0

        decision = await self._rl.select_difficulty(user_id, concept_id, baseline_skill=baseline_skill)

        return {
            "difficulty": decision.legacy_difficulty,
            "action": decision.action.value,
            "decision": decision,
        }

    # -------------------------------------------------------------------
    async def process_attempt(
        self,
        *,
        user_id: str,
        concept_id: str,
        correct: bool,
        current_difficulty: int,
        question_id: str,
        response_time_sec: float | None,
        pending: dict | None,
    ) -> dict:
        """Full post-answer coordination: Process KG (mastery + intervention),
        Hybrid RL (reward + state update + Q-update + next-difficulty preview),
        and — only if the chosen intervention needs it — the Recommendation
        Engine. None of the three overrides another's decision.
        """
        # ---- STEP 1-2: Process KG — mastery update + intervention lookup ----
        delta = 0.12 if correct else -0.08
        new_mastery = await kg_service.record_mastery(user_id, concept_id, delta)
        predicted = min(1.0, new_mastery + (0.05 if correct else -0.03))
        attempts = await kg_service.get_attempts(user_id, concept_id)
        kg_confidence = min(1.0, attempts / 5.0)
        rule = await kg_service.get_intervention(new_mastery)
        intervention = rule or {"state": "OnTrack", "rule": "ContinueBaseline",
                                 "action": "continue_normal"}

        # Did the learner just ENTER this cognitive state, or are they still
        # in the same one they were in last interaction? Persisted on the
        # same MASTERS edge record_mastery writes (see
        # kg_service.swap_intervention_state) so this survives separate HTTP
        # requests, not just a Python variable. `previous_state is None`
        # (never assessed before) counts as an entry.
        previous_state = await kg_service.swap_intervention_state(
            user_id, concept_id, intervention["state"]
        )
        state_changed = previous_state != intervention["state"]

        # ---- STEP 3-4: Hybrid RL — reward + state update + Q-update ----
        # The RL controller only ever sees the answer outcome and timing; it
        # never receives `intervention`, so the Process KG cannot influence
        # (and therefore cannot override) the difficulty decision.
        outcome = await self._rl.process_outcome(
            user_id=user_id, concept_id=concept_id, question_id=question_id,
            correct=correct, response_time_sec=response_time_sec, pending=pending,
            current_difficulty=current_difficulty,
        )
        # Preview of the NEXT difficulty decision — reproducible (see
        # AdaptiveDifficultyController docstring): the real decision happens
        # again, identically, the next time /api/quiz/generate is called.
        preview = await self._rl.select_difficulty(user_id, concept_id)

        # ---- STEP 5-7: does this intervention need external content, AND is
        # this a fresh entry into the state that requires it? A recommendation
        # only ever fires on state ENTRY — remaining in the same state across
        # consecutive answers must not repeatedly re-trigger it.
        action = intervention.get("action", "continue_normal")
        recommendations: list[dict] = []
        recommender_invoked = False
        recommender_failed = False
        if state_changed and _NEEDS_CONTENT.get(action, False):
            recommender_invoked = True
            try:
                recommendations = await recommender.recommend_for_intervention(
                    user_id, action, concept_id, new_mastery,
                )
                # Track what was just recommended so it can be retired later
                # if this concept reaches Mastered (see below). A failure here
                # only means the retirement step won't find it later — it
                # must not break quiz submission either.
                await recommender.record_active_recommendations(user_id, concept_id, recommendations)
            except Exception as exc:
                # A recommender failure must never break quiz submission.
                recommender_failed = True
                recommendations = []
                print(f"[Orchestrator] recommend_for_intervention failed "
                      f"user={user_id} concept={concept_id} action={action}: {exc}")

        # ---- Mastered -> retire any pending remediation for this concept.
        # Idempotent (a concept with nothing active is a no-op), so this runs
        # on every Mastered interaction, not just fresh entry — no reason to
        # leave stale remediation active while more Mastered answers land.
        retired_lecture_ids: list[str] = []
        if intervention["state"] == "Mastered":
            try:
                retired_lecture_ids = await recommender.retire_recommendations_for_concept(
                    user_id, concept_id,
                )
            except Exception as exc:
                print(f"[Orchestrator] retire_recommendations_for_concept failed "
                      f"user={user_id} concept={concept_id}: {exc}")

        reflection = (
            "Prediction matched outcome." if correct == (predicted >= 0.5)
            else "Prediction diverged from outcome; recalibrating."
        )

        # ---- STEP 8: composed, structured result ----
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "beliefs": {"mastery": new_mastery, "predicted": predicted},
            "confidence": kg_confidence,
            "intervention": intervention,
            "cognitive_state": {
                "current": intervention["state"],
                "previous": previous_state,
                "changed": state_changed,
            },
            "recommendations": recommendations,
            "recommender_invoked": recommender_invoked,
            "recommender_failed": recommender_failed,
            "retired_lecture_ids": retired_lecture_ids,
            "next_difficulty": preview.legacy_difficulty,
            "reflection": reflection,
            "rl": {
                "reward": outcome["reward"].as_dict(),
                "q_value_before": outcome["q_value_before"],
                "q_value_after": outcome["q_value_after"],
                "learner_state": outcome["state"].to_dict(),
                "next_action": preview.action.value,
                "next_p_stat": preview.p_stat,
                "next_p_rl": preview.p_rl,
                "next_blend_weight": preview.blend_weight,
                "next_hybrid_policy": preview.hybrid_policy,
            },
        }


orchestrator = AdaptiveLearningOrchestrator()
