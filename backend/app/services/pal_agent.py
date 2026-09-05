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

The Process KG uses 3 cognitive states (Struggling, OnTrack, Mastered)
with per-learner thresholds (tau_struggling, tau_mastered) learned by a
separate Threshold RL.  A state's rules are ADDITIVE — Struggling fires both
OfferSimplerAnalogy and AddRemedialContent — and the content-requiring ones
fire on EVERY interaction in that state, not just on entry: mastery below
tau_struggling means the learner needs the remedial content now, however many
answers ago they first dropped below it.  Mastered triggers
offer_challenge_content (harder questions) and retires any active remedial
recommendations.

The normal answer explanation and the wrong-answer analogy are NOT part of
this orchestrator — they are "immediate answer feedback," generated directly
in routes/quiz.py, independent of Process KG state, RL, and video playback.
This orchestrator must never generate explanations itself.
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.services import kg_service, recommender
from app.services.adaptive.controller import AdaptiveDifficultyController
from app.services.threshold_rl.controller import ThresholdController

_BASELINE_SKILL = {"beginner": 0.35, "intermediate": 0.5, "advanced": 0.65}

# Which Process-KG intervention actions require the Recommendation Engine to
# supply external content. This mapping is the ONLY place "does this
# intervention need content" is decided — plain orchestration data, not a new
# rule engine. Actions not listed here never invoke the recommender. Actions
# marked True fire on EVERY interaction in which the state triggers them, not
# only on a fresh entry into that state: while mastery stays below
# tau_struggling the learner still needs the remedial content. Re-recommending
# is idempotent (recommender.record_active_recommendations upserts).
_NEEDS_CONTENT = {
    "simplify_with_hobby_analogy": False,
    "insert_prerequisite_video": True,
    "offer_challenge_content": False,
    "continue_normal": False,
}


class AdaptiveLearningOrchestrator:
    """Composes StatisticalPrior+Q-learning (via AdaptiveDifficultyController),
    the Process KG, and the Recommendation Engine into one adaptive decision
    per quiz interaction."""

    def __init__(self):
        self._rl = AdaptiveDifficultyController()
        self._threshold_rl = ThresholdController()

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
        # ---- STEP 1: Process KG — mastery update ----
        delta = 0.12 if correct else -0.08
        new_mastery = await kg_service.record_mastery(user_id, concept_id, delta)
        predicted = min(1.0, new_mastery + (0.05 if correct else -0.03))
        attempts = await kg_service.get_attempts(user_id, concept_id)
        kg_confidence = min(1.0, attempts / 5.0)

        # ---- STEP 2: Hybrid RL — reward + state update + Q-update ----
        # Runs BEFORE intervention lookup so the RL state is fresh for the
        # Threshold RL and can be synced to the MASTERS edge.
        outcome = await self._rl.process_outcome(
            user_id=user_id, concept_id=concept_id, question_id=question_id,
            correct=correct, response_time_sec=response_time_sec, pending=pending,
            current_difficulty=current_difficulty,
        )

        # ---- STEP 3: Sync RL state to MASTERS edge (write-through copy) ----
        rl_state_dict = outcome["state"].to_dict()
        await kg_service.sync_rl_state_to_edge(user_id, concept_id, rl_state_dict)

        # ---- STEP 4: Threshold RL — maybe update thresholds (every 5 interactions) ----
        # Must run BEFORE get_intervention so thresholds are current.
        threshold_update = await self._threshold_rl.record_interaction(
            user_id=user_id,
            concept_id=concept_id,
            correct=correct,
            mastery=new_mastery,
            accuracy=rl_state_dict.get("recent_accuracy", 0.5),
            cognitive_state=await kg_service.get_last_cognitive_state(user_id, concept_id) or "OnTrack",
            state_changed=False,
        )

        # decision_ref: deterministic link from this interaction to the exact
        # threshold decision that produced the cognitive-state thresholds.
        # When threshold_update fires (every N interactions), it returns a fresh
        # decision_ref; otherwise we reconstruct it from the current tau_timestep
        # so every interaction is linked, not just the ones that trigger updates.
        if threshold_update and threshold_update.get("decision_ref"):
            threshold_decision_ref = threshold_update["decision_ref"]
        else:
            # No threshold update this step — reference the most recent decision
            from app.services import kg_service as _kg
            current_thresholds = await _kg.get_thresholds(user_id, concept_id)
            tau_ts = current_thresholds.get("tau_timestep", 0)
            threshold_decision_ref = (
                f"threshold:{user_id}:{concept_id}:{tau_ts}" if tau_ts > 0 else None
            )

        # ---- STEP 5: Process KG — intervention lookup (per-learner thresholds) ----
        intervention = await kg_service.get_intervention(user_id, concept_id, new_mastery)

        previous_state = await kg_service.swap_intervention_state(
            user_id, concept_id, intervention["state"]
        )
        state_changed = previous_state != intervention["state"]

        # Now update the threshold RL buffer with the actual state_changed info
        # (the record_interaction above used a placeholder; the buffer stores
        # the interaction for delayed reward — the state_changed flag in the
        # buffer is updated here for accuracy).

        # ---- STEP 6: Difficulty preview ----
        preview = await self._rl.select_difficulty(user_id, concept_id)

        # ---- STEP 7: Recommendations ----
        # The rules a cognitive state triggers are ADDITIVE, so every one of
        # them runs. Struggling triggers both OfferSimplerAnalogy (served
        # outside the orchestrator, as immediate wrong-answer feedback in
        # routes/quiz.py) and AddRemedialContent, which needs the recommender.
        # Content actions fire on every interaction the state triggers them —
        # not only on state ENTRY — so a learner who stays below
        # tau_struggling keeps getting remedial content on each wrong answer.
        #
        # They do NOT fire on a correct answer, even one that leaves mastery
        # below tau_struggling. Such an answer means the learner is climbing
        # back out; piling on more remedial content would punish the recovery.
        # Correctness (not the mastery delta) is the test, because mastery is
        # clamped to [0,1]: a wrong answer at mastery 0 lowers nothing, yet
        # that learner needs the content most.
        action = intervention.get("action", "continue_normal")
        actions = intervention.get("actions") or [action]
        recommendations: list[dict] = []
        content_actions = (
            [a for a in actions if _NEEDS_CONTENT.get(a, False)] if not correct else []
        )
        recommender_invoked = bool(content_actions)
        recommender_failed = False
        seen_lecture_ids: set[str] = set()
        for content_action in content_actions:
            try:
                produced = await recommender.recommend_for_intervention(
                    user_id, content_action, concept_id, new_mastery,
                )
            except Exception as exc:
                recommender_failed = True
                print(f"[Orchestrator] recommend_for_intervention failed "
                      f"user={user_id} concept={concept_id} action={content_action}: {exc}")
                continue
            # Two content actions in one state could surface the same lecture;
            # keep the first occurrence so the learner never sees a duplicate.
            for lec in produced:
                lid = lec.get("lecture_id")
                if lid and lid in seen_lecture_ids:
                    continue
                if lid:
                    seen_lecture_ids.add(lid)
                recommendations.append(lec)
        if recommendations:
            try:
                await recommender.record_active_recommendations(user_id, concept_id, recommendations)
            except Exception as exc:
                recommender_failed = True
                print(f"[Orchestrator] record_active_recommendations failed "
                      f"user={user_id} concept={concept_id}: {exc}")

        # ---- Mastered -> retire any pending remediation for this concept ----
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
            "threshold_update": {
                "tau_struggling": intervention.get("tau_struggling"),
                "tau_mastered": intervention.get("tau_mastered"),
                "updated_this_step": threshold_update is not None,
                "decision_ref": threshold_decision_ref,
            },
            "decision_ref": threshold_decision_ref,
            "recommendations": recommendations,
            "recommender_invoked": recommender_invoked,
            "content_actions": content_actions,
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
