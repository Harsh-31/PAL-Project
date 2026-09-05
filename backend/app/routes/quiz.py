"""Adaptive quiz endpoints.

Question generation uses Ollama; difficulty is set by the Hybrid RL
AdaptiveDifficultyController (sole authority for Easy/Medium/Hard).

Two distinct things happen on every /submit call, deliberately kept separate:
  1. Immediate answer feedback (this file) — the normal explanation (fixed at
     question-creation time) plus, for wrong answers only, an analogy/
     simplified explanation. Independent of Process KG state, RL, and video
     playback.
  2. AdaptiveLearningOrchestrator (pal_agent.py) — mastery update, Process KG
     intervention, RL difficulty preview, and — whenever the cognitive state
     triggers a rule that needs external content — the Recommendation Engine.
"""
from __future__ import annotations
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from app.database.mongo import get_db
from app.models.schemas import QuizRequest, QuizAttemptIn
from app.services import kg_service, pal_agent
from app.services.adaptive import persistence as rl_persistence
from app.services.threshold_rl import persistence as threshold_persistence
from app.services.ollama_service import ollama
from app.utils.deps import current_user

router = APIRouter(prefix="/api/quiz", tags=["quiz"])


async def _generate_incorrect_answer_analogy(chunk_id: str, hobbies: list[str]) -> str | None:
    """Immediate wrong-answer feedback: a simplified/analogy explanation.

    This is deliberately NOT part of AdaptiveLearningOrchestrator — it is
    "immediate answer feedback," a separate responsibility from adaptive
    orchestration (see pal_agent.py's docstring). It has no dependency on
    Process KG state, RL, or the recommender, and reuses the existing
    hobby-analogy LLM mechanism (ollama.summarize_with_hobby) rather than
    introducing a new one. Returns None on any failure (chunk not found,
    LLM error) so a missing analogy never blocks quiz submission.
    """
    try:
        chunk = await kg_service.get_chunk(chunk_id)
        if not chunk:
            return None
        result = await ollama.summarize_with_hobby(
            concept_name=chunk["concept"]["name"],
            chunk_summary=chunk["summary"],
            hobbies=hobbies,
        )
        return result.get("analogy") or result.get("summary") or None
    except Exception as exc:
        print(f"[Quiz] incorrect-answer analogy generation failed for chunk={chunk_id}: {exc}")
        return None


@router.post("/generate")
async def generate(payload: QuizRequest, user=Depends(current_user)):
    chunk = await kg_service.get_chunk(payload.chunk_id)
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    concept = chunk["concept"]

    db = get_db()
    udoc = await db.users.find_one({"_id": ObjectId(user["id"])})
    hobbies = udoc.get("hobbies", []) if udoc else []
    baseline = udoc.get("baseline", "intermediate") if udoc else "intermediate"

    decision_result = await pal_agent.orchestrator.select_difficulty(
        user["id"], concept["id"], baseline
    )
    difficulty = decision_result["difficulty"]

    cognitive_state = await kg_service.get_last_cognitive_state(
        user["id"], concept["id"]
    ) or "OnTrack"

    q = await ollama.generate_mcq(
        concept_name=concept["name"],
        chunk_summary=chunk["summary"],
        difficulty=difficulty,
        hobbies=hobbies,
        cognitive_state=cognitive_state,
    )

    # Cache the question so we can verify correctness server-side later.
    # `rl_pending` snapshots the hybrid-policy decision that chose this
    # difficulty, so /submit can later attribute reward/Q-update to it.
    await db.questions.insert_one({
        "_id": q["id"],
        "user_id": user["id"],
        "lecture_id": payload.lecture_id,
        "chunk_id": payload.chunk_id,
        "concept_id": concept["id"],
        "difficulty": difficulty,
        "question": q["question"],
        "options": q["options"],
        "correct_index": q["correct_index"],
        "explanation": q["explanation"],
        "created_at": datetime.now(timezone.utc),
        "rl_pending": decision_result["decision"].to_pending(),
    })

    # Never leak the answer to the client
    return {
        "id": q["id"],
        "question": q["question"],
        "options": q["options"],
        "difficulty": difficulty,
        "difficulty_action": decision_result["action"],
        "concept": concept["name"],
    }


@router.post("/submit")
async def submit(payload: QuizAttemptIn, user=Depends(current_user)):
    db = get_db()
    qdoc = await db.questions.find_one({"_id": payload.question_id, "user_id": user["id"]})
    if not qdoc:
        raise HTTPException(status_code=404, detail="Question not found")

    correct = int(payload.selected_index) == int(qdoc["correct_index"])

    # Immediate answer feedback (NOT part of the orchestrator — see
    # pal_agent.py's docstring, "orchestrator MUST NOT generate explanations
    # itself"). The normal `explanation` is already fixed at question-creation
    # time (below); the analogy is generated here, only for wrong answers,
    # independent of Process KG state, RL, or video playback.
    analogy = None
    if not correct:
        udoc = await db.users.find_one({"_id": ObjectId(user["id"])})
        hobbies = udoc.get("hobbies", []) if udoc else []
        analogy = await _generate_incorrect_answer_analogy(payload.chunk_id, hobbies)

    # Orchestrated micro-loop: Process KG (mastery + intervention) + Hybrid RL
    # (reward, state update, Q-update, next-difficulty preview) + Recommendation
    # Engine (only if the chosen intervention needs external content)
    trace = await pal_agent.orchestrator.process_attempt(
        user_id=user["id"],
        concept_id=qdoc["concept_id"],
        correct=correct,
        current_difficulty=qdoc["difficulty"],
        question_id=payload.question_id,
        response_time_sec=payload.time_taken_sec,
        pending=qdoc.get("rl_pending"),
    )

    await db.quiz_attempts.insert_one({
        "user_id": user["id"],
        "question_id": payload.question_id,
        "lecture_id": payload.lecture_id,
        "chunk_id": payload.chunk_id,
        "concept_id": qdoc["concept_id"],
        "selected_index": payload.selected_index,
        "correct_index": qdoc["correct_index"],
        "correct": correct,
        "time_taken_sec": payload.time_taken_sec,
        "difficulty": qdoc["difficulty"],
        "decision_ref": trace.get("decision_ref"),
        "trace": trace,
        "timestamp": datetime.now(timezone.utc),
    })

    return {
        "correct": correct,
        "correct_index": qdoc["correct_index"],
        "explanation": qdoc["explanation"],
        "analogy": analogy,
        "mastery": trace["beliefs"]["mastery"],
        "intervention": trace["intervention"],
        "recommendations": trace["recommendations"],
        "retired_lecture_ids": trace["retired_lecture_ids"],
        "next_difficulty": trace["next_difficulty"],
        "reward": trace["rl"]["reward"],
    }


@router.get("/decisions/{concept_id}")
async def decisions(concept_id: str, user=Depends(current_user)):
    """Explainability API — returns the full Hybrid RL decision trace (learner
    state, p_stat, p_rl, blend weight, hybrid policy, selected action, reward,
    Q-value before/after) for every interaction this learner has had with this
    concept. Answers: "why did PAL choose HARD for this learner at this point?"
    """
    trace = await rl_persistence.get_decision_trace(user["id"], concept_id)
    return {"count": len(trace), "decisions": trace}


@router.get("/summary/{chunk_id}")
async def summary(chunk_id: str, user=Depends(current_user)):
    """Post-lecture personalised summary using hobby analogies."""
    chunk = await kg_service.get_chunk(chunk_id)
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    db = get_db()
    udoc = await db.users.find_one({"_id": ObjectId(user["id"])})
    hobbies = udoc.get("hobbies", []) if udoc else []
    result = await ollama.summarize_with_hobby(
        concept_name=chunk["concept"]["name"],
        chunk_summary=chunk["summary"],
        hobbies=hobbies,
    )
    return result


# ---------------------------------------------------------------------------
# Decision Traceability APIs
# ---------------------------------------------------------------------------

@router.get("/threshold-decisions/{concept_id}")
async def threshold_decisions(concept_id: str, user=Depends(current_user)):
    """Explainability API — returns the full Threshold RL decision trace for a
    learner×concept pair.  Each entry records the raw mastery and accuracy
    changes, personalized threshold updates (tau_struggling, tau_mastered),
    the Q-learning actions chosen, rewards, and Q-values before/after.

    Answers: "why did PAL set *these* cognitive-state boundaries for this
    learner at this point?"
    """
    trace = await threshold_persistence.get_decision_trace(user["id"], concept_id)
    return {"count": len(trace), "decisions": trace}


@router.get("/decision-pathway/{question_id}")
async def decision_pathway(question_id: str, user=Depends(current_user)):
    """End-to-end decision traceability — reconstructs the complete pathway:

        mastery → personalized thresholds → cognitive state →
        Process KG rule → pedagogical action

    for a single quiz interaction identified by question_id.  Joins the
    quiz_attempt record (which carries the full orchestrator trace) with the
    threshold decision it was linked to via decision_ref, producing one
    self-contained JSON object that explains every decision PAL made for
    that interaction.
    """
    db = get_db()

    # 1. Find the quiz attempt for this question + user
    attempt = await db.quiz_attempts.find_one({
        "question_id": question_id, "user_id": user["id"],
    })
    if not attempt:
        raise HTTPException(status_code=404, detail="Quiz attempt not found")

    attempt["_id"] = str(attempt["_id"])
    trace = attempt.get("trace", {})

    # 2. Resolve the linked threshold decision (if any)
    decision_ref = attempt.get("decision_ref")
    threshold_decision = None
    if decision_ref:
        threshold_decision = await threshold_persistence.get_decision_by_ref(decision_ref)

    # 3. Compose the full end-to-end pathway
    pathway = {
        "question_id": question_id,
        "concept_id": attempt.get("concept_id"),
        "timestamp": attempt.get("timestamp"),
        "correct": attempt.get("correct"),
        "difficulty": attempt.get("difficulty"),

        # --- Layer 1: Mastery ---
        "mastery": {
            "value": trace.get("beliefs", {}).get("mastery"),
            "predicted": trace.get("beliefs", {}).get("predicted"),
            "confidence": trace.get("confidence"),
        },

        # --- Layer 2: Personalized Thresholds (from Threshold RL) ---
        "thresholds": {
            "tau_struggling": trace.get("threshold_update", {}).get("tau_struggling"),
            "tau_mastered": trace.get("threshold_update", {}).get("tau_mastered"),
            "updated_this_step": trace.get("threshold_update", {}).get("updated_this_step"),
            "decision_ref": decision_ref,
            "threshold_decision": threshold_decision,
        },

        # --- Layer 3: Cognitive State (classified using thresholds) ---
        "cognitive_state": trace.get("cognitive_state"),

        # --- Layer 4: Process KG Rule → Pedagogical Action ---
        "intervention": trace.get("intervention"),

        # --- Layer 5: Recommendations (if intervention required content) ---
        "recommendations": trace.get("recommendations", []),
        "retired_lecture_ids": trace.get("retired_lecture_ids", []),

        # --- Hybrid RL context (difficulty selection provenance) ---
        "rl": trace.get("rl"),
    }

    return pathway
