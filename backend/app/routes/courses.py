"""Course listing, onboarding, playlist generation."""
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from app.database.mongo import get_db
from app.services import kg_service, recommender
from app.models.schemas import OnboardingIn
from app.utils.deps import current_user

router = APIRouter(prefix="/api", tags=["courses"])


@router.get("/courses")
async def list_courses(user=Depends(current_user)):
    return await kg_service.get_all_courses()


@router.post("/onboarding")
async def onboarding(payload: OnboardingIn, user=Depends(current_user)):
    # T-Box link: enrol the learner in the chosen course
    # A-Box: store hobbies, baseline, goal on the Learner node
    await kg_service.upsert_learner(
        user_id=user["id"],
        hobbies=payload.hobbies,
        baseline=payload.baseline,
        goal=payload.goal,
    )
    course = await kg_service.get_course(payload.course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    await kg_service.enrol_learner(user["id"], payload.course_id)

    # Mirror on the user doc so the frontend can jump straight in on next login
    db = get_db()
    await db.users.update_one(
        {"_id": ObjectId(user["id"])},
        {"$set": {
            "onboarded": True,
            "current_course_id": payload.course_id,
            "baseline": payload.baseline,
            "goal": payload.goal,
            "hobbies": payload.hobbies,
            "evaluation_frequency": payload.evaluation_frequency,
            "onboarded_at": datetime.now(timezone.utc),
        }},
    )
    return {"ok": True, "course_id": payload.course_id}


@router.get("/playlist/{course_id}")
async def get_playlist(course_id: str, user=Depends(current_user)):
    lectures = await kg_service.get_playlist_for_course(course_id)
    if not lectures:
        raise HTTPException(status_code=404, detail="No lectures found for course")
    mastery = await kg_service.get_all_mastery(user["id"], course_id)

    # Recommendation engine: semantic first, topic-tag fallback if empty
    # (empty = embed model not installed, or no similar concepts pass the threshold).
    supplementary = await recommender.recommend_supplementary(user["id"], course_id)
    rec_source = "semantic" if supplementary else None
    if not supplementary:
        supplementary = await kg_service.get_supplementary_lectures(user["id"], course_id)
        if supplementary:
            for s in supplementary:
                s["recommendation_source"] = "topic"
            rec_source = "topic"

    return {
        "course_id": course_id,
        "lectures": lectures,
        "supplementary": supplementary,
        "mastery": mastery,
        "recommendation_source": rec_source,
    }


@router.get("/me")
async def me(user=Depends(current_user)):
    db = get_db()
    doc = await db.users.find_one({"_id": ObjectId(user["id"])})
    if not doc:
        raise HTTPException(status_code=404)
    return {
        "id": str(doc["_id"]),
        "name": doc["name"],
        "email": doc["email"],
        "onboarded": doc.get("onboarded", False),
        "current_course_id": doc.get("current_course_id"),
        "hobbies": doc.get("hobbies", []),
        "baseline": doc.get("baseline"),
        "goal": doc.get("goal"),
    }