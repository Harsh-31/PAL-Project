"""Knowledge Graph operations — writes to Neo4j.

Responsibilities:
  1. Seed T-Box (Course / Concept / LectureChunk / prerequisite edges)
  2. Create/update Learner (A-Box) with hobbies + baseline
  3. Record interactions (quiz attempts, mastery updates)
  4. Query mastery / next-best chunk for playlist generation
"""
import json
from pathlib import Path
from app.database.neo4j_db import get_driver


DATA_FILE = Path(__file__).parent.parent / "data" / "courses.json"


async def seed_courses_if_empty() -> None:
    """T-Box seed — courses, concepts, prereqs, lecture chunks.

    Now runs on every startup: courses/concepts are idempotently MERGE-upserted,
    and lecture chunks are wiped and rebuilt so edits to courses.json (e.g. new
    YouTube IDs, chunk boundaries) take effect on the next restart without
    needing to nuke the DB manually. Learner MASTERS edges live on Concepts,
    not on LectureChunks, so they're preserved.
    """
    driver = get_driver()
    async with driver.session() as s:
        # Rebuild LectureChunk nodes from scratch — cheap and keeps them in sync
        # with the current seed file. TEACHES edges are dropped with the nodes.
        await s.run("MATCH (l:LectureChunk) DETACH DELETE l")

        courses = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        for course in courses:
            await s.run(
                """MERGE (c:Course {id:$id})
                     SET c.title=$title, c.provider=$provider, c.code=$code,
                         c.description=$desc, c.domain=$domain""",
                id=course["id"], title=course["title"], provider=course["provider"],
                code=course["code"], desc=course["description"], domain=course["domain"],
            )
            for con in course["concepts"]:
                await s.run(
                    """MERGE (k:Concept {id:$id})
                         SET k.name=$name, k.difficulty=$diff, k.topic=$topic
                       WITH k
                       MATCH (c:Course {id:$cid}) MERGE (c)-[:CONTAINS]->(k)""",
                    id=con["id"], name=con["name"], diff=con["difficulty"],
                    topic=con.get("topic", ""), cid=course["id"],
                )
                for pre in con.get("prereqs", []):
                    await s.run(
                        """MATCH (a:Concept {id:$a}), (b:Concept {id:$b})
                           MERGE (b)-[:REQUIRES]->(a)""",
                        a=pre, b=con["id"],
                    )
            for lec in course["lectures"]:
                for ch in lec["chunks"]:
                    await s.run(
                        """MERGE (l:LectureChunk {id:$id})
                             SET l.lecture_id=$lec, l.title=$title,
                                 l.youtube_id=$yt, l.start=$s, l.end=$e,
                                 l.summary=$sum, l.duration=$dur""",
                        id=ch["id"], lec=lec["id"], title=lec["title"],
                        yt=lec["youtube_id"], s=ch["start"], e=ch["end"],
                        sum=ch["summary"], dur=lec["duration_sec"],
                    )
                    for cid in lec["concept_ids"]:
                        await s.run(
                            """MATCH (l:LectureChunk {id:$lid}), (k:Concept {id:$kid})
                               MERGE (l)-[:TEACHES]->(k)""",
                            lid=ch["id"], kid=cid,
                        )
        print(f"[KG] seeded {len(courses)} courses")


async def upsert_learner(user_id: str, hobbies: list[str], baseline: str, goal: str) -> None:
    driver = get_driver()
    async with driver.session() as s:
        await s.run(
            """MERGE (u:Learner {id:$id})
                 SET u.hobbies=$hobbies, u.baseline=$baseline, u.goal=$goal""",
            id=user_id, hobbies=hobbies, baseline=baseline, goal=goal,
        )


async def enrol_learner(user_id: str, course_id: str) -> None:
    driver = get_driver()
    async with driver.session() as s:
        await s.run(
            """MATCH (u:Learner {id:$uid}), (c:Course {id:$cid})
               MERGE (u)-[:ENROLLED_IN]->(c)""",
            uid=user_id, cid=course_id,
        )


async def get_course(course_id: str) -> dict | None:
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run("MATCH (c:Course {id:$id}) RETURN c", id=course_id)
        rec = await r.single()
        return dict(rec["c"]) if rec else None


async def get_all_courses() -> list[dict]:
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run("MATCH (c:Course) RETURN c ORDER BY c.title")
        return [dict(rec["c"]) async for rec in r]


async def get_playlist_for_course(course_id: str) -> list[dict]:
    """Return an ordered list of lectures (with their chunks) for a course.

    Ordering is by concept difficulty then chunk start — topological-ish.
    """
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (c:Course {id:$cid})-[:CONTAINS]->(k:Concept)<-[:TEACHES]-(l:LectureChunk)
               RETURN DISTINCT l.lecture_id AS lid, l.title AS title,
                      l.youtube_id AS yt, l.duration AS dur,
                      collect(DISTINCT {id:l.id, start:l.start, end:l.end,
                                        summary:l.summary, concept_id:k.id,
                                        concept_name:k.name, difficulty:k.difficulty}) AS chunks""",
            cid=course_id,
        )
        out = []
        async for rec in r:
            chunks = sorted(rec["chunks"], key=lambda c: c["start"])
            out.append({
                "lecture_id": rec["lid"],
                "title": rec["title"],
                "youtube_id": rec["yt"],
                "duration_sec": rec["dur"],
                "chunks": chunks,
            })
        # Order lectures by first chunk difficulty
        out.sort(key=lambda l: (min((c["difficulty"] for c in l["chunks"]), default=99),
                                l["title"]))
        return out


async def get_chunk(chunk_id: str) -> dict | None:
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (l:LectureChunk {id:$id})-[:TEACHES]->(k:Concept)
               RETURN l, k""",
            id=chunk_id,
        )
        rec = await r.single()
        if not rec:
            return None
        chunk = dict(rec["l"])
        chunk["concept"] = dict(rec["k"])
        return chunk


async def record_mastery(user_id: str, concept_id: str, delta: float) -> float:
    """Additive mastery update, clamped to [0,1]. Returns the new score."""
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (u:Learner {id:$uid}), (k:Concept {id:$cid})
               MERGE (u)-[m:MASTERS]->(k)
                 ON CREATE SET m.score = 0.5, m.attempts = 0
               SET m.score   = CASE WHEN m.score + $delta > 1 THEN 1
                                    WHEN m.score + $delta < 0 THEN 0
                                    ELSE m.score + $delta END,
                   m.attempts = coalesce(m.attempts, 0) + 1,
                   m.updated_at = timestamp()
               RETURN m.score AS score""",
            uid=user_id, cid=concept_id, delta=delta,
        )
        rec = await r.single()
        return rec["score"] if rec else 0.5


async def get_attempts(user_id: str, concept_id: str) -> int:
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k:Concept {id:$cid})
               RETURN coalesce(m.attempts, 0) AS n""",
            uid=user_id, cid=concept_id,
        )
        rec = await r.single()
        return rec["n"] if rec else 0


async def get_mastery(user_id: str, concept_id: str) -> float:
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k:Concept {id:$cid})
               RETURN m.score AS score""",
            uid=user_id, cid=concept_id,
        )
        rec = await r.single()
        return rec["score"] if rec else 0.5


async def get_all_mastery(user_id: str, course_id: str) -> list[dict]:
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (c:Course {id:$cid})-[:CONTAINS]->(k:Concept)
               OPTIONAL MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k)
               RETURN k.id AS id, k.name AS name, k.difficulty AS diff,
                      coalesce(m.score, 0.5) AS score,
                      coalesce(m.attempts, 0) AS attempts
               ORDER BY diff""",
            cid=course_id, uid=user_id,
        )
        return [dict(rec) async for rec in r]


async def get_intervention(mastery: float) -> dict | None:
    """Consult the Process KG — pick the highest-priority rule whose threshold fires."""
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (s:CognitiveState)-[:TRIGGERS]->(r:InterventionRule)
               WHERE $m <= r.mastery_threshold
               RETURN s.name AS state, r.name AS rule, r.action AS action,
                      r.mastery_threshold AS thr
               ORDER BY r.mastery_threshold ASC LIMIT 1""",
            m=mastery,
        )
        rec = await r.single()
        return dict(rec) if rec else None


async def kg_snapshot(user_id: str) -> dict:
    """Small snapshot for the KG Explorer UI."""
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k:Concept)
               RETURN k.name AS concept, m.score AS score,
                      m.attempts AS attempts
               ORDER BY m.score DESC""",
            uid=user_id,
        )
        rows = [dict(rec) async for rec in r]

        r2 = await s.run(
            "MATCH (u:Learner {id:$uid})-[:ENROLLED_IN]->(c:Course) RETURN c.title AS title",
            uid=user_id,
        )
        courses = [rec["title"] async for rec in r2]
        return {"enrolled": courses, "mastery": rows}


async def get_supplementary_lectures(user_id: str, course_id: str,
                                     struggle_threshold: float = 0.55) -> list[dict]:
    """Find supplementary lectures from OTHER courses for concepts the learner is struggling with.

    Implements the PRD's "PAL dynamically adds supplementary videos from the upcoming
    playlist queue" — cross-course curation.

    A supplementary lecture:
      - Comes from a course DIFFERENT from the currently enrolled one
      - Teaches a concept with the SAME topic tag as a concept the user is struggling with
        in the enrolled course
      - Is returned in the same shape as normal playlist lectures, plus fields:
          `supplementary=True`, `source_course_id`, `source_course_title`, `for_concept_name`
    """
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """
            // 1. Find topics the learner struggles with in their enrolled course
            MATCH (c:Course {id:$cid})-[:CONTAINS]->(k:Concept)
            MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k)
            WHERE m.score < $thr AND k.topic IS NOT NULL AND k.topic <> ''
            WITH collect({topic: k.topic, concept_name: k.name}) AS struggles

            UNWIND struggles AS st
            // 2. Find OTHER courses whose concepts share the same topic
            MATCH (other:Course)-[:CONTAINS]->(k2:Concept {topic: st.topic})
            WHERE other.id <> $cid
            MATCH (l:LectureChunk)-[:TEACHES]->(k2)
            WITH other, k2, st, l
            // 3. Group chunks by lecture (there are multiple chunks per lecture)
            RETURN other.id AS course_id, other.title AS course_title,
                   k2.id AS concept_id, k2.name AS concept_name,
                   k2.difficulty AS difficulty,
                   st.concept_name AS for_concept_name,
                   l.lecture_id AS lid, l.title AS title,
                   l.youtube_id AS yt, l.duration AS dur,
                   collect(DISTINCT {id:l.id, start:l.start, end:l.end,
                                     summary:l.summary, concept_id:k2.id,
                                     concept_name:k2.name, difficulty:k2.difficulty}) AS chunks
            """,
            cid=course_id, uid=user_id, thr=struggle_threshold,
        )
        seen = set()
        out = []
        async for rec in r:
            lid = rec["lid"]
            if lid in seen:
                continue
            seen.add(lid)
            chunks = sorted(rec["chunks"], key=lambda c: c["start"])
            out.append({
                "lecture_id": lid,
                "title": rec["title"],
                "youtube_id": rec["yt"],
                "duration_sec": rec["dur"],
                "chunks": chunks,
                "supplementary": True,
                "source_course_id": rec["course_id"],
                "source_course_title": rec["course_title"],
                "for_concept_name": rec["for_concept_name"],
            })
        return out


# =========================================================================
# LEARNING TRACKS + COMPOSED (multi-course) PLAYLIST
# PRD: "PAL generates a custom video playlist from a library of open-source
# courses" — a track is a curated group of concepts spanning multiple courses,
# and the playlist is the union of lectures teaching those concepts.
# =========================================================================

_TRACKS_FILE = Path(__file__).parent.parent / "data" / "tracks.json"


def load_tracks() -> list[dict]:
    """Read the static tracks catalog from disk."""
    if not _TRACKS_FILE.exists():
        return []
    return json.loads(_TRACKS_FILE.read_text(encoding="utf-8"))


async def get_all_tracks_with_metadata() -> list[dict]:
    """Tracks enriched with the number of courses + lectures each spans.

    Used by the onboarding UI to show what a track actually contains.
    """
    tracks = load_tracks()
    if not tracks:
        return []
    driver = get_driver()
    async with driver.session() as s:
        for track in tracks:
            cids = track["concept_ids"]
            r = await s.run(
                """MATCH (c:Course)-[:CONTAINS]->(k:Concept)
                   WHERE k.id IN $cids
                   OPTIONAL MATCH (l:LectureChunk)-[:TEACHES]->(k)
                   RETURN count(DISTINCT c) AS courses,
                          count(DISTINCT l.lecture_id) AS lectures""",
                cids=cids,
            )
            rec = await r.single()
            track["stats"] = {
                "courses": rec["courses"] if rec else 0,
                "lectures": rec["lectures"] if rec else 0,
            }
    return tracks


async def get_composed_playlist(user_id: str, target_concept_ids: list[str]) -> list[dict]:
    """Compose a cross-course playlist from lectures that teach the target concepts.

    Ordering:
      1. Concept difficulty ASC (easier first)
      2. Then by lecture title for stability
    Returns lecture dicts with the same shape as get_playlist_for_course.
    """
    if not target_concept_ids:
        return []
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (c:Course)-[:CONTAINS]->(k:Concept)
               WHERE k.id IN $cids
               MATCH (l:LectureChunk)-[:TEACHES]->(k)
               RETURN DISTINCT
                   l.lecture_id AS lid, l.title AS title,
                   l.youtube_id AS yt, l.duration AS dur,
                   c.id AS course_id, c.title AS course_title,
                   collect(DISTINCT {
                       id: l.id, start: l.start, end: l.end,
                       summary: l.summary, concept_id: k.id,
                       concept_name: k.name, difficulty: k.difficulty
                   }) AS chunks""",
            cids=target_concept_ids,
        )
        rows = [dict(rec) async for rec in r]
    lectures = []
    for row in rows:
        chunks = sorted(row["chunks"], key=lambda c: c["start"])
        min_diff = min((c["difficulty"] for c in chunks if c["difficulty"]), default=99)
        lectures.append({
            "lecture_id": row["lid"],
            "title": row["title"],
            "youtube_id": row["yt"],
            "duration_sec": row["dur"],
            "source_course_id": row["course_id"],
            "source_course_title": row["course_title"],
            "chunks": chunks,
            "_min_diff": min_diff,
        })
    lectures.sort(key=lambda l: (l["_min_diff"], l["title"]))
    for l in lectures:
        l.pop("_min_diff", None)
    return lectures


async def get_mastery_for_concepts(user_id: str, concept_ids: list[str]) -> list[dict]:
    """Same shape as get_all_mastery but scoped to a concept list rather than a course."""
    if not concept_ids:
        return []
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (k:Concept) WHERE k.id IN $cids
               OPTIONAL MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k)
               RETURN k.id AS id, k.name AS name, k.difficulty AS difficulty,
                      coalesce(m.score, 0) AS score,
                      coalesce(m.attempts, 0) AS attempts""",
            cids=concept_ids, uid=user_id,
        )
        return [dict(rec) async for rec in r]