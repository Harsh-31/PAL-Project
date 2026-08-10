"""Knowledge Graph operations — writes to Neo4j.

Responsibilities:
  1. Seed T-Box (Course / Concept / LectureChunk / prerequisite edges)
  2. Create/update Learner (A-Box) with hobbies + baseline
  3. Record interactions (quiz attempts, mastery updates)
  4. Query mastery / next-best chunk for playlist generation

Reads courses.json in the FULL MongoDB/PALMS schema (course_id, lecture_id,
chunk_id, start_time/end_time, summary_text, difficulty_score, etc.).
"""
import json
import re
from collections import deque
from pathlib import Path
from app.database.neo4j_db import get_driver


DATA_FILE = Path(__file__).parent.parent / "data" / "courses.json"


def _extract_youtube_id(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
    return m.group(1) if m else ""


async def seed_courses_if_empty() -> None:
    """T-Box seed — courses, concepts, prereqs, lecture chunks.

    Runs on every startup using batched UNWIND queries for speed.
    Reads the FULL PALMS MongoDB schema from courses.json.
    Courses/concepts are MERGE-upserted; lecture chunks are wiped and rebuilt
    so changes take effect without manual DB cleanup.
    Learner MASTERS edges live on Concepts, not LectureChunks, so they survive.
    """
    driver = get_driver()
    if not DATA_FILE.exists():
        print("[KG] no courses.json — skipping seed")
        return
    courses = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if not courses:
        print("[KG] courses.json is empty — skipping seed")
        return

    async with driver.session() as s:
        await s.run("MATCH (l:LectureChunk) DETACH DELETE l")

        # 1. Batch upsert courses (full schema)
        course_rows = []
        for c in courses:
            course_rows.append({
                "id": c["course_id"],
                "title": c.get("title", ""),
                "description": c.get("description", ""),
                "difficulty_score": c.get("difficulty_score", 1),
                "difficulty_label": c.get("difficulty_label", ""),
                "tags": c.get("tags", []),
                "thumbnail_url": c.get("thumbnail_url", ""),
                "course_url": c.get("course_url", ""),
                "provider": "PALMS",
                "code": c["course_id"],
                "domain": (c.get("tags") or ["General"])[0].title() if c.get("tags") else "General",
            })
        await s.run(
            """UNWIND $rows AS r
               MERGE (c:Course {id: r.id})
                 SET c.title=r.title, c.description=r.description,
                     c.difficulty_score=r.difficulty_score,
                     c.difficulty_label=r.difficulty_label,
                     c.tags=r.tags, c.thumbnail_url=r.thumbnail_url,
                     c.course_url=r.course_url, c.provider=r.provider,
                     c.code=r.code, c.domain=r.domain""",
            rows=course_rows,
        )

        # 2. Batch upsert concepts from lecture topic_id/topic_name + CONTAINS edges
        concept_rows = []
        seen_concepts: set[str] = set()
        for c in courses:
            for lec in c.get("lectures", []):
                tid = lec.get("topic_id")
                if tid and tid not in seen_concepts:
                    seen_concepts.add(tid)
                    concept_rows.append({
                        "id": tid,
                        "name": lec.get("topic_name", tid),
                        "diff": lec.get("difficulty_score", 1),
                        "topic": tid,
                        "cid": c["course_id"],
                    })
        if concept_rows:
            await s.run(
                """UNWIND $rows AS r
                   MERGE (k:Concept {id: r.id})
                     SET k.name=r.name, k.difficulty=r.diff, k.topic=r.topic
                   WITH k, r
                   MATCH (c:Course {id: r.cid}) MERGE (c)-[:CONTAINS]->(k)""",
                rows=concept_rows,
            )

        # 3. Batch upsert prerequisite edges from lecture prerequisite_topic_ids
        prereq_rows = []
        seen_prereqs: set[tuple] = set()
        for c in courses:
            for lec in c.get("lectures", []):
                tid = lec.get("topic_id")
                if not tid:
                    continue
                for pre in lec.get("prerequisite_topic_ids", []):
                    pair = (pre, tid)
                    if pair not in seen_prereqs:
                        seen_prereqs.add(pair)
                        prereq_rows.append({"a": pre, "b": tid})
        if prereq_rows:
            await s.run(
                """UNWIND $rows AS r
                   MATCH (a:Concept {id: r.a}), (b:Concept {id: r.b})
                   MERGE (b)-[:REQUIRES]->(a)""",
                rows=prereq_rows,
            )

        # 4. Batch create lecture chunks (full schema)
        chunk_rows = []
        for c in courses:
            for lec in c.get("lectures", []):
                yt = _extract_youtube_id(lec.get("lecture_url", ""))
                lec_start = lec.get("start_time", 0) or 0
                lec_end = lec.get("end_time", 0) or 0
                dur = lec_end - lec_start
                for ch in lec.get("chunks", []):
                    chunk_rows.append({
                        "id": ch["chunk_id"],
                        "lec": lec["lecture_id"],
                        "course_id": c["course_id"],
                        "title": lec.get("title", ""),
                        "description": lec.get("description", ""),
                        "yt": yt,
                        "s": ch.get("start_time", 0),
                        "e": ch.get("end_time", 0),
                        "sum": ch.get("summary_text", ""),
                        "dur": dur,
                        "diff_score": lec.get("difficulty_score", 1),
                        "diff_label": lec.get("difficulty_label", ""),
                    })
        if chunk_rows:
            await s.run(
                """UNWIND $rows AS r
                   MERGE (l:LectureChunk {id: r.id})
                     SET l.lecture_id=r.lec, l.course_id=r.course_id,
                         l.title=r.title, l.description=r.description,
                         l.youtube_id=r.yt, l.start=r.s, l.end=r.e,
                         l.summary=r.sum, l.duration=r.dur,
                         l.difficulty_score=r.diff_score,
                         l.difficulty_label=r.diff_label""",
                rows=chunk_rows,
            )

        # 5. Batch create TEACHES edges (lecture topic_id → chunk)
        teaches_rows = []
        for c in courses:
            for lec in c.get("lectures", []):
                tid = lec.get("topic_id")
                if not tid:
                    continue
                for ch in lec.get("chunks", []):
                    teaches_rows.append({"lid": ch["chunk_id"], "kid": tid})
        if teaches_rows:
            await s.run(
                """UNWIND $rows AS r
                   MATCH (l:LectureChunk {id: r.lid}), (k:Concept {id: r.kid})
                   MERGE (l)-[:TEACHES]->(k)""",
                rows=teaches_rows,
            )

    print(f"[KG] seeded {len(courses)} courses ({len(chunk_rows)} chunks, {len(concept_rows)} concepts)")


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
                      l.difficulty_score AS diff_score,
                      l.difficulty_label AS diff_label,
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
                "difficulty_score": rec["diff_score"],
                "difficulty_label": rec["diff_label"],
                "chunks": chunks,
            })
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
    """Find supplementary lectures from OTHER courses for concepts the learner is struggling with."""
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """
            MATCH (c:Course {id:$cid})-[:CONTAINS]->(k:Concept)
            MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k)
            WHERE m.score < $thr AND k.topic IS NOT NULL AND k.topic <> ''
            WITH collect({topic: k.topic, concept_name: k.name}) AS struggles

            UNWIND struggles AS st
            MATCH (other:Course)-[:CONTAINS]->(k2:Concept {topic: st.topic})
            WHERE other.id <> $cid
            MATCH (l:LectureChunk)-[:TEACHES]->(k2)
            WITH other, k2, st, l
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
# =========================================================================

_TRACKS_FILE = Path(__file__).parent.parent / "data" / "tracks.json"


def load_tracks() -> list[dict]:
    """Read the tracks catalog from disk (auto-generated by sync_service)."""
    if not _TRACKS_FILE.exists():
        return []
    return json.loads(_TRACKS_FILE.read_text(encoding="utf-8"))


async def get_all_tracks_with_metadata() -> list[dict]:
    """Tracks enriched with the number of courses + lectures each spans."""
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


async def get_prerequisite_graph(concept_ids: list[str]) -> dict[str, list[str]]:
    """Return {concept_id: [prerequisite_ids]} scoped to the given concept set."""
    if not concept_ids:
        return {}
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (k:Concept)-[:REQUIRES]->(pre:Concept)
               WHERE k.id IN $cids AND pre.id IN $cids
               RETURN k.id AS concept_id, collect(DISTINCT pre.id) AS prereqs""",
            cids=concept_ids,
        )
        return {rec["concept_id"]: rec["prereqs"] async for rec in r}


async def get_prerequisite_closure(concept_ids: list[str]) -> set[str]:
    """Transitive walk of REQUIRES edges to find all prerequisite concept_ids."""
    if not concept_ids:
        return set()
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (k:Concept)-[:REQUIRES*1..5]->(pre:Concept)
               WHERE k.id IN $cids
               RETURN DISTINCT pre.id AS prereq_id""",
            cids=concept_ids,
        )
        return {rec["prereq_id"] async for rec in r}


def topological_sort_concepts(
    concept_ids: list[str],
    prereq_graph: dict[str, list[str]],
    difficulty_map: dict[str, int],
) -> list[str]:
    """Kahn's algorithm with difficulty tie-breaking (easy first)."""
    cid_set = set(concept_ids)
    in_degree: dict[str, int] = {cid: 0 for cid in concept_ids}
    dependents: dict[str, list[str]] = {cid: [] for cid in concept_ids}
    for cid, prereqs in prereq_graph.items():
        if cid not in cid_set:
            continue
        for pre in prereqs:
            if pre in cid_set:
                in_degree[cid] = in_degree.get(cid, 0) + 1
                dependents.setdefault(pre, []).append(cid)

    ready = sorted(
        [cid for cid in concept_ids if in_degree.get(cid, 0) == 0],
        key=lambda c: (difficulty_map.get(c, 99), c),
    )
    q = deque(ready)
    out: list[str] = []
    while q:
        node = q.popleft()
        out.append(node)
        nexts = []
        for dep in dependents.get(node, []):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                nexts.append(dep)
        nexts.sort(key=lambda c: (difficulty_map.get(c, 99), c))
        q.extend(nexts)

    # any concepts not reached (cycles or missing edges) go at the end
    remaining = [c for c in concept_ids if c not in set(out)]
    remaining.sort(key=lambda c: (difficulty_map.get(c, 99), c))
    return out + remaining


async def get_composed_playlist(
    user_id: str,
    target_concept_ids: list[str],
    ordered_concept_ids: list[str] | None = None,
) -> list[dict]:
    """Compose a cross-course playlist from lectures that teach the target concepts.

    If ordered_concept_ids is provided, lectures are sorted by the earliest
    position of their concepts in that ordered list (prerequisite-aware).
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

    concept_pos = {}
    if ordered_concept_ids:
        concept_pos = {cid: i for i, cid in enumerate(ordered_concept_ids)}

    lectures = []
    for row in rows:
        chunks = sorted(row["chunks"], key=lambda c: c["start"])
        min_diff = min((c["difficulty"] for c in chunks if c["difficulty"]), default=99)
        if concept_pos:
            earliest_pos = min(
                (concept_pos.get(c["concept_id"], 9999) for c in chunks),
                default=9999,
            )
        else:
            earliest_pos = 0
        lectures.append({
            "lecture_id": row["lid"],
            "title": row["title"],
            "youtube_id": row["yt"],
            "duration_sec": row["dur"],
            "source_course_id": row["course_id"],
            "source_course_title": row["course_title"],
            "chunks": chunks,
            "_sort_key": (earliest_pos, min_diff, row["title"] or ""),
        })
    lectures.sort(key=lambda l: l["_sort_key"])
    for l in lectures:
        l.pop("_sort_key", None)
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
