"""Content-based recommendation engine.

Architecture (matches an item-based content recommender):

  1. Item representation:
       Each Concept node gets a semantic embedding (nomic-embed-text via Ollama)
       built from concept name + short teaching description. Vectors live on the
       Concept node in Neo4j as a float array property.

  2. Candidate generation:
       For each concept the learner is struggling with (mastery < 0.55), pull all
       Concept nodes from OTHER courses. This is the recall step.

  3. Ranking:
       Cosine similarity(struggling_concept, candidate_concept), boosted by
       proximity in difficulty so the bridge lecture isn't wildly harder/easier.

  4. Filtering (via Process KG):
       Only surface lectures for candidates whose similarity clears a floor.

  5. Diversification:
       Return one lecture per candidate concept, top-K overall by score,
       de-duped across source lectures.

Falls back to topic-tag matching when embeddings are missing (e.g. the embed
model isn't installed) so the app degrades gracefully.
"""
from __future__ import annotations
import math, re
from collections import deque
from app.database.neo4j_db import get_driver
from app.services.ollama_service import ollama

_STOP_WORDS = frozenset(
    "i me my we our you your he she it they them a an the and but or so if in on at to for of "
    "with by from is am are was were be been has have had do does did will would shall should "
    "can could may might must not no nor very too also just about above after again all any "
    "between both each few more most other some such than that these this those through under "
    "until up what when where which while who whom why how want learn master understand study "
    "get know".split()
)


# ---------- vector math ----------

def cos_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def difficulty_proximity(d1: int, d2: int) -> float:
    """1.0 when difficulties match, decays with distance. Keeps bridges pedagogically sane."""
    return 1.0 / (1.0 + abs((d1 or 0) - (d2 or 0)))


# ---------- embedding pipeline ----------

def _concept_embed_text(name: str, topic: str, chunk_summaries: list[str]) -> str:
    """Build the string we embed. Concept name + a couple of chunk summaries.

    Concept name is the primary signal; teaching summaries anchor it in domain
    context so 'Variables' in Python doesn't collide with 'Variables' in stats.
    nomic-embed-text has an 8192-token context (~6000 chars safe limit).
    """
    parts = [name]
    if topic:
        parts.append(f"topic: {topic}")
    if chunk_summaries:
        combined = " ".join(chunk_summaries[:3])
        parts.append(combined[:2000])
    text = ". ".join(parts)
    return text[:4000]


async def ensure_concept_embeddings() -> dict:
    """Compute + store an embedding for every Concept that doesn't yet have one.

    Runs on startup (called from lifespan). Cheap after the first run.
    Returns a report dict for the log line.
    """
    driver = get_driver()
    async with driver.session() as s:
        # Pull concepts + their chunk summaries in one query, filter missing embeddings
        r = await s.run(
            """MATCH (k:Concept)
               OPTIONAL MATCH (l:LectureChunk)-[:TEACHES]->(k)
               WITH k, collect(DISTINCT l.summary) AS summaries
               WHERE k.embedding IS NULL
               RETURN k.id AS id, k.name AS name,
                      coalesce(k.topic, '') AS topic,
                      summaries"""
        )
        pending = [dict(rec) async for rec in r]

    embedded, skipped = 0, 0
    for row in pending:
        text = _concept_embed_text(row["name"], row["topic"], row["summaries"] or [])
        vec = await ollama.embed(text)
        if not vec:
            skipped += 1
            continue
        async with driver.session() as s:
            await s.run(
                "MATCH (k:Concept {id:$id}) SET k.embedding = $vec, k.embed_dim = $dim",
                id=row["id"], vec=vec, dim=len(vec),
            )
        embedded += 1

    return {"embedded": embedded, "skipped": skipped, "total_pending": len(pending)}


# ---------- retrieval ----------

async def _fetch_all_concepts_with_embeddings() -> list[dict]:
    """Load every embedded concept with its Course context + candidate lectures."""
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (c:Course)-[:CONTAINS]->(k:Concept)
               WHERE k.embedding IS NOT NULL
               OPTIONAL MATCH (l:LectureChunk)-[:TEACHES]->(k)
               WITH c, k, collect(DISTINCT {
                     id: l.id, start: l.start, end: l.end, summary: l.summary,
                     lecture_id: l.lecture_id, lecture_title: l.title,
                     youtube_id: l.youtube_id, duration: l.duration
               }) AS chunks
               RETURN c.id AS course_id, c.title AS course_title,
                      c.domain AS course_domain,
                      k.id AS concept_id, k.name AS concept_name,
                      k.difficulty AS difficulty, k.embedding AS embedding,
                      chunks"""
        )
        return [dict(rec) async for rec in r]


async def _fetch_struggling_concepts(user_id: str, course_id: str,
                                     threshold: float) -> list[dict]:
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (c:Course {id:$cid})-[:CONTAINS]->(k:Concept)
               MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k)
               WHERE m.score < $thr
               RETURN k.id AS concept_id, k.name AS concept_name,
                      k.difficulty AS difficulty, k.embedding AS embedding,
                      m.score AS score""",
            cid=course_id, uid=user_id, thr=threshold,
        )
        return [dict(rec) async for rec in r]


def _group_chunks_into_lectures(chunks: list[dict]) -> list[dict]:
    """Neo4j returns per-chunk rows; the frontend wants lecture-level items."""
    by_lecture: dict[str, dict] = {}
    for ch in chunks:
        if not ch.get("lecture_id"):
            continue
        lid = ch["lecture_id"]
        if lid not in by_lecture:
            by_lecture[lid] = {
                "lecture_id": lid,
                "title": ch["lecture_title"],
                "youtube_id": ch["youtube_id"],
                "duration_sec": ch["duration"],
                "chunks": [],
            }
        by_lecture[lid]["chunks"].append({
            "id": ch["id"], "start": ch["start"], "end": ch["end"],
            "summary": ch["summary"],
        })
    for lec in by_lecture.values():
        lec["chunks"].sort(key=lambda c: c["start"])
    return list(by_lecture.values())


async def _fetch_struggling_target_concepts(user_id: str, target_concept_ids: list[str],
                                            threshold: float) -> list[dict]:
    """Concepts in the learner's TARGET set (their goal) that they're struggling with."""
    if not target_concept_ids:
        return []
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (c:Course)-[:CONTAINS]->(k:Concept)
               WHERE k.id IN $cids
               MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k)
               WHERE m.score < $thr
               RETURN DISTINCT
                      k.id AS concept_id, k.name AS concept_name,
                      k.difficulty AS difficulty, k.embedding AS embedding,
                      c.domain AS course_domain, m.score AS score""",
            cids=target_concept_ids, uid=user_id, thr=threshold,
        )
        return [dict(rec) async for rec in r]


async def recommend_supplementary(user_id: str, target_concept_ids: list[str],
                                  struggle_threshold: float = 0.55,
                                  k_per_struggle: int = 2,
                                  min_similarity: float = 0.55,
                                  cross_domain_threshold: float = 0.72,
                                  playlist_lecture_ids: list[str] | None = None) -> list[dict]:
    """Cross-course supplementary recommendations.

    Candidates are any embedded concept in the KG EXCEPT the struggling concept
    itself. If the caller passes `playlist_lecture_ids`, we drop lectures the
    learner is already seeing — so a recommendation is only surfaced when it's
    genuinely new material.

    Same-domain candidates get a score boost and clear a lower similarity bar.
    Cross-domain candidates must clear a higher bar so unrelated topics don't
    surface as "bridges".
    """
    if not target_concept_ids:
        return []
    struggling = await _fetch_struggling_target_concepts(
        user_id, target_concept_ids, struggle_threshold
    )
    if not struggling:
        return []
    all_concepts = await _fetch_all_concepts_with_embeddings()
    if not all_concepts:
        return []

    struggling_ids = {s["concept_id"] for s in struggling}
    playlist_ids = set(playlist_lecture_ids or [])

    picked: dict[str, dict] = {}
    for st in struggling:
        if not st.get("embedding"):
            continue
        source_domain = st.get("course_domain")
        # Candidates: any embedded concept that isn't itself struggling
        candidates = [c for c in all_concepts
                      if c["concept_id"] not in struggling_ids and c["embedding"]]
        scored = []
        for cand in candidates:
            sim = cos_sim(st["embedding"], cand["embedding"])
            prox = difficulty_proximity(st["difficulty"], cand["difficulty"])
            same_domain = (cand.get("course_domain") == source_domain) if source_domain else False
            score = sim * (0.7 + 0.3 * prox)
            if same_domain:
                score *= 1.2
            scored.append((score, sim, cand, same_domain))
        scored.sort(key=lambda x: x[0], reverse=True)
        for score, sim, cand, same_domain in scored[:k_per_struggle]:
            needed = min_similarity if same_domain else cross_domain_threshold
            if sim < needed:
                continue
            lectures = _group_chunks_into_lectures(cand["chunks"])
            for lec in lectures:
                # Skip lectures already in the composed playlist — don't tell the
                # learner to watch what they'd naturally reach anyway.
                if lec["lecture_id"] in playlist_ids:
                    continue
                if lec["lecture_id"] in picked:
                    continue
                for ch in lec["chunks"]:
                    ch["concept_id"] = cand["concept_id"]
                    ch["concept_name"] = cand["concept_name"]
                    ch["difficulty"] = cand["difficulty"]
                picked[lec["lecture_id"]] = {
                    **lec,
                    "supplementary": True,
                    "source_course_id": cand["course_id"],
                    "source_course_title": cand["course_title"],
                    "same_domain": same_domain,
                    "for_concept_name": st["concept_name"],
                    "similarity": round(sim, 3),
                    "recommendation_source": "semantic",
                }
    return sorted(picked.values(), key=lambda l: -l.get("similarity", 0))


# ---------- goal-based concept matching ----------

async def _fetch_all_concepts_basic() -> list[dict]:
    """Lightweight fetch of all concepts (no embeddings, no chunks)."""
    driver = get_driver()
    async with driver.session() as s:
        r = await s.run(
            """MATCH (c:Course)-[:CONTAINS]->(k:Concept)
               RETURN k.id AS concept_id, k.name AS concept_name,
                      coalesce(k.topic, '') AS topic,
                      k.difficulty AS difficulty, c.id AS course_id,
                      c.tags AS course_tags"""
        )
        return [dict(rec) async for rec in r]


def _extract_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9+#]+", text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


async def _keyword_fallback_match(
    goal_text: str,
    track_concept_ids: list[str] | None = None,
    top_k: int = 40,
) -> list[str]:
    keywords = _extract_keywords(goal_text)
    if not keywords:
        return []
    all_concepts = await _fetch_all_concepts_basic()
    track_set = set(track_concept_ids or [])
    scored: list[tuple[float, str]] = []
    for c in all_concepts:
        haystack = f"{c['concept_name']} {c['topic']}".lower()
        tags = " ".join(c.get("course_tags") or []).lower()
        hits = sum(1 for kw in keywords if kw in haystack or kw in tags)
        if hits == 0 and c["concept_id"] not in track_set:
            continue
        score = hits + (0.5 if c["concept_id"] in track_set else 0)
        scored.append((score, c["concept_id"]))
    scored.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    out: list[str] = []
    for _, cid in scored[:top_k]:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


async def match_goal_to_concepts(
    goal_text: str,
    baseline: str = "beginner",
    track_concept_ids: list[str] | None = None,
    top_k: int = 40,
    similarity_floor: float = 0.55,
    track_boost: float = 0.10,
) -> list[str]:
    """Match a free-text learning goal against all concept embeddings in the KG.

    Returns up to top_k concept_ids ranked by semantic similarity to the goal.
    Only concepts above similarity_floor are included — this prevents
    unrelated courses from leaking into the playlist.
    If tracks are selected, those concepts get a small additive score boost
    but still must clear the similarity floor independently.
    Falls back to keyword matching when Ollama is unavailable.
    """
    goal_vec = await ollama.embed(goal_text)
    if not goal_vec:
        return await _keyword_fallback_match(goal_text, track_concept_ids, top_k)

    all_concepts = await _fetch_all_concepts_with_embeddings()
    if not all_concepts:
        return await _keyword_fallback_match(goal_text, track_concept_ids, top_k)

    track_set = set(track_concept_ids or [])
    scored: list[tuple[float, float, str]] = []
    for c in all_concepts:
        sim = cos_sim(goal_vec, c["embedding"])
        if sim < similarity_floor:
            continue
        boosted = sim + (track_boost if c["concept_id"] in track_set else 0)
        scored.append((boosted, sim, c["concept_id"]))

    scored.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    out: list[str] = []
    for _, _, cid in scored[:top_k]:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


# ---------- baseline-level filtering ----------

def apply_baseline_filter(
    concept_ids: list[str],
    difficulty_map: dict[str, int],
    baseline: str,
    prerequisite_ids: set[str],
) -> list[str]:
    """Filter concepts by the user's proficiency level.

    Beginner: keep all (ordering handles easy→hard).
    Intermediate: keep difficulty >= 2, plus difficulty=1 if it's a prerequisite.
    Advanced: keep difficulty >= 3, plus lower if it's a prerequisite.
    """
    if baseline == "beginner":
        return concept_ids

    min_difficulty = 3 if baseline == "advanced" else 2
    return [
        cid for cid in concept_ids
        if (difficulty_map.get(cid, 1)) >= min_difficulty
        or cid in prerequisite_ids
    ]