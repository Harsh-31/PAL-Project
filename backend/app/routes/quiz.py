"""Adaptive quiz endpoints.

Generation uses Ollama, difficulty is set by PAL-Agent, and every attempt
runs the full micro-loop (Observe -> Update Beliefs -> ... -> Memory Update).
"""
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from app.database.mongo import get_db
from app.models.schemas import QuizRequest, QuizAttemptIn
from app.services import kg_service, pal_agent
from app.services.ollama_service import ollama
from app.utils.deps import current_user

router = APIRouter(prefix="/api/quiz", tags=["quiz"])


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

    difficulty = await pal_agent.decide_initial_difficulty(
        user["id"], concept["id"], baseline
    )

    q = await ollama.generate_mcq(
        concept_name=concept["name"],
        chunk_summary=chunk["summary"],
        difficulty=difficulty,
        hobbies=hobbies,
    )

    # Cache the question so we can verify correctness server-side later
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
    })

    # Never leak the answer to the client
    return {
        "id": q["id"],
        "question": q["question"],
        "options": q["options"],
        "difficulty": difficulty,
        "concept": concept["name"],
    }


@router.post("/submit")
async def submit(payload: QuizAttemptIn, user=Depends(current_user)):
    db = get_db()
    qdoc = await db.questions.find_one({"_id": payload.question_id, "user_id": user["id"]})
    if not qdoc:
        raise HTTPException(status_code=404, detail="Question not found")

    correct = int(payload.selected_index) == int(qdoc["correct_index"])

    # PAL-Agent micro-loop
    trace = await pal_agent.decide_after_attempt(
        user_id=user["id"],
        concept_id=qdoc["concept_id"],
        correct=correct,
        current_difficulty=qdoc["difficulty"],
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
        "trace": trace,
        "timestamp": datetime.now(timezone.utc),
    })

    return {
        "correct": correct,
        "correct_index": qdoc["correct_index"],
        "explanation": qdoc["explanation"],
        "mastery": trace["beliefs"]["mastery"],
        "intervention": trace["intervention"],
        "next_difficulty": trace["next_difficulty"],
    }


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
