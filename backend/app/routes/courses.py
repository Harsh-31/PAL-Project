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
    all_tracks = kg_service.load_tracks()
    track_concept_ids = (
        _tracks_to_concepts(payload.track_ids, all_tracks)
        if payload.track_ids else []
    )

    # Goal-based semantic matching across ALL concepts in the KG.
    # Track concepts get a boost but don't limit the search.
    target_concept_ids = await recommender.match_goal_to_concepts(
        goal_text=payload.goal,
        baseline=payload.baseline,
        track_concept_ids=track_concept_ids or None,
    )

    # Fallback: if goal matching returned nothing, use track concepts directly
    if not target_concept_ids and track_concept_ids:
        target_concept_ids = track_concept_ids

    if not target_concept_ids:
        raise HTTPException(
            status_code=400,
            detail="Could not match your goal to any content. "
                   "Try a more specific goal or select learning tracks.",
        )

    await kg_service.upsert_learner(
        user_id=user["id"],
        hobbies=payload.hobbies,
        baseline=payload.baseline,
        goal=payload.goal,
    )

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
         "$unset": {"current_course_id": ""}},
    )
    return {"ok": True, "track_ids": payload.track_ids,
            "target_concept_ids": target_concept_ids}


@router.get("/playlist")
async def get_playlist(user=Depends(current_user)):
    """Composed playlist across the learner's goal-matched concepts.

    Pipeline: target_concept_ids → prerequisite sort → baseline filter → compose.
    """
    db = get_db()
    doc = await db.users.find_one({"_id": ObjectId(user["id"])})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")

    target_concept_ids = doc.get("target_concept_ids") or []
    if not target_concept_ids:
        raise HTTPException(status_code=400,
                            detail="Complete onboarding first")

    baseline = doc.get("baseline", "beginner")

    # Build difficulty map from concept nodes
    concept_info = await kg_service.get_mastery_for_concepts(user["id"], target_concept_ids)
    difficulty_map = {c["id"]: c["difficulty"] for c in concept_info}

    # Prerequisite-aware ordering
    prereq_graph = await kg_service.get_prerequisite_graph(target_concept_ids)
    ordered = kg_service.topological_sort_concepts(
        target_concept_ids, prereq_graph, difficulty_map
    )

    # Baseline-level filtering (preserves prerequisites)
    prereq_closure = await kg_service.get_prerequisite_closure(target_concept_ids)
    filtered = recommender.apply_baseline_filter(
        ordered, difficulty_map, baseline, prereq_closure
    )

    lectures = await kg_service.get_composed_playlist(
        user["id"], filtered, ordered_concept_ids=filtered
    )
    mastery = await kg_service.get_mastery_for_concepts(user["id"], filtered)

    playlist_lecture_ids = [l["lecture_id"] for l in lectures]
    supplementary = await recommender.recommend_supplementary(
        user["id"], filtered,
        playlist_lecture_ids=playlist_lecture_ids,
    )

    return {
        "track_ids": doc.get("track_ids", []),
        "target_concept_ids": filtered,
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