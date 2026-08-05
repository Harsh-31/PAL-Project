"""Dashboard analytics and KG snapshot for the frontend explorer."""
from fastapi import APIRouter, Depends
from app.database.mongo import get_db
from app.services import kg_service
from app.utils.deps import current_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary")
async def summary(user=Depends(current_user)):
    db = get_db()
    total = await db.quiz_attempts.count_documents({"user_id": user["id"]})
    correct = await db.quiz_attempts.count_documents({"user_id": user["id"], "correct": True})
    notes = await db.notes.count_documents({"user_id": user["id"]})
    kg = await kg_service.kg_snapshot(user["id"])
    accuracy = (correct / total) if total else 0.0

    # Recent attempts, most recent first
    cursor = db.quiz_attempts.find({"user_id": user["id"]}).sort("timestamp", -1).limit(10)
    recent = []
    async for a in cursor:
        recent.append({
            "concept_id": a["concept_id"],
            "correct": a["correct"],
            "difficulty": a.get("difficulty"),
            "timestamp": a["timestamp"].isoformat(),
            "intervention": a.get("trace", {}).get("intervention", {}).get("rule"),
        })

    return {
        "total_attempts": total,
        "correct_attempts": correct,
        "accuracy": accuracy,
        "note_count": notes,
        "enrolled_courses": kg["enrolled"],
        "mastery": kg["mastery"],
        "recent_attempts": recent,
    }


@router.get("/kg")
async def kg(user=Depends(current_user)):
    return await kg_service.kg_snapshot(user["id"])
