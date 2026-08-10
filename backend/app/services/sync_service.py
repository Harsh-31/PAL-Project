"""Sync service — pulls course and transcript data from MongoDB (palms DB)
and writes the FULL document schema to local JSON cache files.

On every startup the backend checks MongoDB for new courses and transcripts
and appends any additions to the local JSON files.  The local files then
power the KG seeding (kg_service) and downstream logic — so the system
dynamically picks up new content added by the ingestion pipeline.

The documents are stored AS-IS from MongoDB (no field renaming or lossy
transformation) so all properties are available for Ollama, difficulty
logic, and future features.
"""
import json
import re
from pathlib import Path
from app.database.mongo import get_db

DATA_DIR = Path(__file__).parent.parent / "data"
COURSES_FILE = DATA_DIR / "courses.json"
TRANSCRIPTS_FILE = DATA_DIR / "transcripts.json"
TRACKS_FILE = DATA_DIR / "tracks.json"


def _serialize_doc(doc: dict) -> dict:
    """Convert MongoDB document for JSON serialization (ObjectId → str)."""
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["_id"] = str(v)
        else:
            out[k] = v
    return out


def _generate_tracks(courses: list[dict]) -> list[dict]:
    """Auto-generate one learning track per course.

    Each track's concept_ids are the topic_ids from that course,
    which matches the Concept nodes seeded into Neo4j.
    """
    tracks = []
    for course in courses:
        topic_ids = course.get("topic_ids", [])
        if not topic_ids:
            continue
        tracks.append({
            "id": course["course_id"],
            "title": course.get("title", ""),
            "icon": "📚",
            "description": course.get("description", ""),
            "difficulty_label": course.get("difficulty_label", ""),
            "tags": course.get("tags", []),
            "thumbnail_url": course.get("thumbnail_url", ""),
            "concept_ids": topic_ids,
        })
    return tracks


async def sync_courses() -> dict:
    """Pull courses from MongoDB and store full documents in local JSON.

    Existing courses are updated with the latest MongoDB version;
    new courses are appended.
    """
    db = get_db()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    existing_ids: set[str] = set()
    if COURSES_FILE.exists():
        try:
            existing = json.loads(COURSES_FILE.read_text(encoding="utf-8"))
            existing_ids = {c.get("course_id", "") for c in existing}
        except (json.JSONDecodeError, KeyError):
            existing = []

    cursor = db["courses"].find({})
    mongo_docs = await cursor.to_list(length=None)

    added = 0
    updated = 0
    for doc in mongo_docs:
        serialized = _serialize_doc(doc)
        course_id = serialized.get("course_id", serialized.get("_id", ""))
        if course_id not in existing_ids:
            existing.append(serialized)
            existing_ids.add(course_id)
            added += 1
        else:
            for i, c in enumerate(existing):
                if c.get("course_id", "") == course_id:
                    existing[i] = serialized
                    updated += 1
                    break

    COURSES_FILE.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    tracks = _generate_tracks(existing)
    TRACKS_FILE.write_text(
        json.dumps(tracks, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {"total": len(existing), "added": added, "updated": updated}


async def sync_transcripts() -> dict:
    """Pull transcripts from MongoDB and store full documents in local JSON."""
    db = get_db()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    existing_ids: set[str] = set()
    if TRANSCRIPTS_FILE.exists():
        try:
            existing = json.loads(TRANSCRIPTS_FILE.read_text(encoding="utf-8"))
            existing_ids = {t.get("transcript_id", "") for t in existing}
        except (json.JSONDecodeError, KeyError):
            existing = []

    cursor = db["transcripts"].find({})
    mongo_docs = await cursor.to_list(length=None)

    added = 0
    for doc in mongo_docs:
        serialized = _serialize_doc(doc)
        transcript_id = serialized.get("transcript_id", serialized.get("_id", ""))
        if transcript_id in existing_ids:
            continue
        existing.append(serialized)
        existing_ids.add(transcript_id)
        added += 1

    TRANSCRIPTS_FILE.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {"total": len(existing), "added": added}
