"""Course listing, tracks, onboarding, composed playlist generation."""
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
    """Kept for the KG Explorer / diagnostics — not used in onboarding anymore."""
    return await kg_service.get_all_courses()


@router.get("/tracks")
async def list_tracks(user=Depends(current_user)):
    """Learning tracks — the multi-course curated groupings shown at onboarding.

    PRD: "PAL generates a custom video playlist from a library of open-source
    courses" — tracks are how we surface that library organised by topic
    rather than by publisher.
    """
    return await kg_service.get_all_tracks_with_metadata()


def _tracks_to_concepts(track_ids: list[str], all_tracks: list[dict]) -> list[str]:
    """Union the concept ids across the selected tracks (order-preserving, deduped)."""
    by_id = {t["id"]: t for t in all_tracks}
    seen: set[str] = set()
    out: list[str] = []
    for tid in track_ids:
        track = by_id.get(tid)
        if not track:
            continue
        for cid in track.get("concept_ids", []):
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out


@router.post("/onboarding")
async def onboarding(payload: OnboardingIn, user=Depends(current_user)):
    if not payload.track_ids:
        raise HTTPException(status_code=400, detail="Pick at least one learning track")

    all_tracks = kg_service.load_tracks()
    target_concept_ids = _tracks_to_concepts(payload.track_ids, all_tracks)
    if not target_concept_ids:
        raise HTTPException(status_code=400, detail="Selected tracks are not recognised")

    # A-Box: store hobbies/baseline/goal on the Learner node
    await kg_service.upsert_learner(
        user_id=user["id"],
        hobbies=payload.hobbies,
        baseline=payload.baseline,
        goal=payload.goal,
    )

    # Mirror onboarding state on the user doc so the frontend can hydrate quickly.
    db = get_db()
    await db.users.update_one(
        {"_id": ObjectId(user["id"])},
        {"$set": {
            "onboarded": True,
            "track_ids": payload.track_ids,
            "target_concept_ids": target_concept_ids,
            "baseline": payload.baseline,
            "goal": payload.goal,
            "hobbies": payload.hobbies,
            "evaluation_frequency": payload.evaluation_frequency,
            "onboarded_at": datetime.now(timezone.utc),
        },
         "$unset": {"current_course_id": ""}},  # remove legacy field
    )
    return {"ok": True, "track_ids": payload.track_ids,
            "target_concept_ids": target_concept_ids}


@router.get("/playlist")
async def get_playlist(user=Depends(current_user)):
    """Composed playlist across the learner's chosen tracks (their goal).

    PRD: "custom video playlist from a library of open-source courses ...
    acts as a bridge from the user's current baseline to their target mastery."
    """
    db = get_db()
    doc = await db.users.find_one({"_id": ObjectId(user["id"])})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")

    target_concept_ids = doc.get("target_concept_ids") or []
    if not target_concept_ids:
        raise HTTPException(status_code=400,
                            detail="No learning tracks selected — complete onboarding first")

    lectures = await kg_service.get_composed_playlist(user["id"], target_concept_ids)
    mastery = await kg_service.get_mastery_for_concepts(user["id"], target_concept_ids)

    # Recommendation engine — pass playlist lecture ids so we don't recommend
    # lectures already in the composed playlist.
    playlist_lecture_ids = [l["lecture_id"] for l in lectures]
    supplementary = await recommender.recommend_supplementary(
        user["id"], target_concept_ids,
        playlist_lecture_ids=playlist_lecture_ids,
    )

    return {
        "track_ids": doc.get("track_ids", []),
        "target_concept_ids": target_concept_ids,
        "lectures": lectures,
        "supplementary": supplementary,
        "mastery": mastery,
        "recommendation_source": "semantic" if supplementary else None,
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
        "track_ids": doc.get("track_ids", []),
        "target_concept_ids": doc.get("target_concept_ids", []),
        "hobbies": doc.get("hobbies", []),
        "baseline": doc.get("baseline"),
        "goal": doc.get("goal"),
    }