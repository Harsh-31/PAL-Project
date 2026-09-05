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
from datetime import datetime, timezone
from app.database.mongo import get_db
from app.database.neo4j_db import get_driver
from app.services import kg_service
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


# =========================================================================
# PROCESS-KG INTERVENTION -> RECOMMENDATION BRIDGE
#
# The Process KG (app.services.kg_service) decides WHEN a pedagogical
# intervention needs external content; this module decides WHICH content is
# relevant. Nothing below changes the ranking/embedding logic above — it's a
# thin dispatch layer plus one new selection strategy (challenge content),
# built from the exact same primitives recommend_supplementary already uses.
# =========================================================================

async def recommend_challenge(concept_id: str,
                              top_k: int = 2, min_similarity: float = 0.55) -> list[dict]:
    """Enrichment/challenge content for a learner who has done well on `concept_id`.

    Inverse selection criterion from recommend_supplementary: instead of
    looking for content to shore up a WEAK concept, this looks for
    semantically related concepts at a HIGHER difficulty than the one the
    learner just handled well — optional "go deeper" material, not a change
    to the next question's difficulty (that stays Hybrid RL's job).
    """
    all_concepts = await _fetch_all_concepts_with_embeddings()
    if not all_concepts:
        return []
    source = next((c for c in all_concepts if c["concept_id"] == concept_id), None)
    if not source or not source.get("embedding"):
        return []
    source_diff = source.get("difficulty") or 0

    scored = []
    for cand in all_concepts:
        if cand["concept_id"] == concept_id or not cand.get("embedding"):
            continue
        if (cand.get("difficulty") or 0) <= source_diff:
            continue  # only genuinely "harder" material counts as a challenge
        sim = cos_sim(source["embedding"], cand["embedding"])
        if sim < min_similarity:
            continue
        scored.append((sim, cand))
    scored.sort(key=lambda x: -x[0])

    picked: dict[str, dict] = {}
    for sim, cand in scored[:top_k]:
        for lec in _group_chunks_into_lectures(cand["chunks"]):
            if lec["lecture_id"] in picked:
                continue
            for ch in lec["chunks"]:
                ch["concept_id"] = cand["concept_id"]
                ch["concept_name"] = cand["concept_name"]
                ch["difficulty"] = cand["difficulty"]
            picked[lec["lecture_id"]] = {
                **lec,
                "supplementary": True,
                "challenge": True,
                "source_course_id": cand["course_id"],
                "source_course_title": cand["course_title"],
                "for_concept_name": source["concept_name"],
                "similarity": round(sim, 3),
                "recommendation_source": "semantic_challenge",
            }
    return sorted(picked.values(), key=lambda l: -l.get("similarity", 0))


# Which Process-KG intervention actions this dispatcher knows how to serve.
# Anything not listed here returns [] — the orchestrator decides WHETHER a
# given intervention needs content at all (see app/services/pal_agent.py's
# _NEEDS_CONTENT map); this function only decides WHICH content once asked.
async def recommend_for_intervention(user_id: str, intervention_action: str,
                                     concept_id: str, mastery: float) -> list[dict]:
    """Single entry point the orchestrator calls once it has decided a
    Process-KG intervention needs external content. Dispatches to the
    existing struggling-concept recommender or the new challenge recommender
    — the actual ranking/embedding/similarity logic is unchanged either way.
    """
    if intervention_action == "insert_prerequisite_video":
        # The KG fires this rule whenever mastery is below the learner's
        # tau_struggling, which the Threshold RL can move anywhere in [0,1].
        # `mastery + 0.01` therefore does the real work — it guarantees this
        # concept clears _fetch_struggling_target_concepts' bar whatever the
        # learned threshold happens to be — and the 0.6 floor only keeps the
        # long-standing default behaviour for low masteries.
        threshold = max(0.6, mastery + 0.01)
        return await recommend_supplementary(user_id, [concept_id], struggle_threshold=threshold)
    if intervention_action == "offer_challenge_content":
        return await recommend_challenge(concept_id)
    # simplify_with_hobby_analogy (Struggling) deliberately returns no
    # content — that action is served by the existing hobby-analogy mechanism
    # instead (see AdaptiveLearningOrchestrator._NEEDS_CONTENT).
    return []


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


# ---------- goal -> concept scoring (shared by match_goal_to_concepts and
# the onboarding starter-playlist builder below) ----------

# Tightened from the original top_k=40/floor=0.55 defaults after a live
# experiment against the real KG showed that combination routinely matched
# 60-120 lectures for ordinary goals — effectively "most of a course" rather
# than a personalized starter set. 0.65 is the floor where focused goals stay
# sharply relevant without empty-ing out on legitimately broad ones (that's
# what the fallback below is for).
DEFAULT_GOAL_TOP_K = 20
DEFAULT_GOAL_SIMILARITY_FLOOR = 0.65
DEFAULT_GOAL_FALLBACK_MIN_CONCEPTS = 3


async def _select_concepts_for_goal(
    goal_text: str,
    track_concept_ids: list[str] | None = None,
    top_k: int = DEFAULT_GOAL_TOP_K,
    similarity_floor: float = DEFAULT_GOAL_SIMILARITY_FLOOR,
    track_boost: float = 0.10,
    fallback_min_concepts: int = DEFAULT_GOAL_FALLBACK_MIN_CONCEPTS,
) -> tuple[list[tuple[str, float]], bool]:
    """Core goal->concept scoring + selection, shared by match_goal_to_concepts
    (returns bare concept_ids, used to persist target_concept_ids for the
    ongoing/unbounded course playlist) and build_onboarding_starter_playlist
    (needs the similarity scores too, to rank lectures by relevance).

    Returns ([(concept_id, raw_similarity), ...] ranked by relevance, whether
    the top-3-by-similarity fallback activated). Empty list means "no
    embedding available" — caller should fall back to keyword matching.

    Fallback: if fewer than `fallback_min_concepts` concepts clear
    `similarity_floor`, use the top `fallback_min_concepts` concepts by raw
    similarity instead — this NEVER lowers the floor itself (concepts that
    would normally qualify still qualify identically), it only relaxes which
    concepts count as "enough" for a goal that is legitimately broad, so a
    valid goal never produces an empty playlist purely because nothing beat
    an arbitrary cutoff.
    """
    goal_vec = await ollama.embed(goal_text)
    if not goal_vec:
        return [], False

    all_concepts = await _fetch_all_concepts_with_embeddings()
    if not all_concepts:
        return [], False

    track_set = set(track_concept_ids or [])
    # (boosted_score, raw_similarity, concept_id) — boosted score is only used
    # for ranking/ties when a track was selected; raw similarity is what the
    # floor and the fallback both operate on, and what gets propagated to
    # lectures as their relevance score.
    scored: list[tuple[float, float, str]] = []
    for c in all_concepts:
        if not c.get("embedding"):
            continue
        sim = cos_sim(goal_vec, c["embedding"])
        boosted = sim + (track_boost if c["concept_id"] in track_set else 0)
        scored.append((boosted, sim, c["concept_id"]))
    scored.sort(key=lambda x: -x[0])

    above_floor = [row for row in scored if row[1] >= similarity_floor]

    if len(above_floor) < fallback_min_concepts:
        by_raw_similarity = sorted(scored, key=lambda x: -x[1])
        selected = by_raw_similarity[:fallback_min_concepts]
        fallback_activated = True
    else:
        selected = above_floor[:top_k]
        fallback_activated = False

    # de-dupe defensively (concept_ids are already unique in `all_concepts`,
    # but keep this cheap guard so ranking never silently double-counts)
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    for _, sim, cid in selected:
        if cid not in seen:
            seen.add(cid)
            out.append((cid, sim))
    return out, fallback_activated


async def match_goal_to_concepts(
    goal_text: str,
    track_concept_ids: list[str] | None = None,
    top_k: int = DEFAULT_GOAL_TOP_K,
    similarity_floor: float = DEFAULT_GOAL_SIMILARITY_FLOOR,
    track_boost: float = 0.10,
) -> list[str]:
    """Match a free-text learning goal against all concept embeddings in the KG.

    Returns up to top_k concept_ids ranked by semantic similarity to the goal.
    Only concepts above similarity_floor are included, UNLESS that yields
    fewer than 3 concepts, in which case the top 3 by similarity are used
    instead (see _select_concepts_for_goal) — this is what gets persisted as
    the learner's target_concept_ids for the ongoing (uncapped) course
    playlist. Falls back to keyword matching when Ollama is unavailable.

    Deliberately takes NO baseline: what gets persisted is the learner's full
    goal-matched concept set, and apply_baseline_filter runs at READ time in
    the /api/playlist route. Filtering here instead would bake the filter
    into storage and permanently discard concepts the learner would need if
    their baseline ever changed. (The starter playlist below does apply the
    filter, because it is a presentation-time view, not persisted state.)
    """
    selected, _fallback_activated = await _select_concepts_for_goal(
        goal_text, track_concept_ids, top_k, similarity_floor, track_boost,
    )
    if not selected:
        return await _keyword_fallback_match(goal_text, track_concept_ids, top_k)
    return [cid for cid, _sim in selected]


async def _apply_baseline_filter_to_concepts(
    concept_ids: list[str], baseline: str,
) -> tuple[list[str], list[str]]:
    """Fetch what apply_baseline_filter needs, then apply it.

    Wraps the difficulty-map + prerequisite-closure lookups that the
    /api/playlist route does inline, so the onboarding starter playlist can
    apply the SAME concept-level difficulty rule without duplicating them.

    Returns (kept_ids, dropped_ids) with `kept_ids` in the same relative
    order as `concept_ids` — the filter only punches holes in an existing
    order, it never re-sequences.

    "beginner" short-circuits: apply_baseline_filter is the identity function
    for that baseline, so the two Neo4j round-trips would be pure waste.
    """
    if baseline == "beginner" or not concept_ids:
        return list(concept_ids), []

    difficulty_map = await kg_service.get_concept_difficulties(concept_ids)
    prereq_closure = await kg_service.get_prerequisite_closure(concept_ids)

    kept = apply_baseline_filter(concept_ids, difficulty_map, baseline, prereq_closure)
    kept_set = set(kept)
    dropped = [cid for cid in concept_ids if cid not in kept_set]
    return kept, dropped


# ---------- onboarding starter playlist (capped, relevance-ranked) ----------

MAX_ONBOARDING_LECTURES = 10


async def build_onboarding_starter_playlist(
    user_id: str,
    goal_text: str,
    track_concept_ids: list[str] | None = None,
    baseline: str = "beginner",
    max_lectures: int = MAX_ONBOARDING_LECTURES,
) -> dict:
    """Goal -> a small, focused STARTER playlist (<= max_lectures).

    This is deliberately narrower than the learner's full target_concept_ids
    set (which still backs the ongoing, uncapped /api/playlist view) — the
    onboarding starter is "here's where to begin," not "everything this goal
    could ever cover." Later adaptive recommendations (remedial/challenge,
    triggered by the Process KG + Hybrid RL loop) add more content over time;
    this function does not touch that path at all.

    Selection ranks LECTURES by the similarity score of the concept that
    produced them (existing semantic information, no new ranking model), caps
    to `max_lectures`, dedupes by lecture_id (get_composed_playlist already
    does this at the query level; the max() below also handles the
    hypothetical case of one lecture reachable via more than one concept),
    then restores get_composed_playlist's own prerequisite/curriculum
    ordering for just the selected subset — relevance decides WHICH lectures
    make the cut, existing ordering logic decides what SEQUENCE to present
    them in.

    `baseline` applies the SAME concept-level difficulty rule the ongoing
    /api/playlist view uses (apply_baseline_filter: intermediate keeps
    difficulty >= 2, advanced >= 3, prerequisite closure always exempt), so
    an advanced learner's starter set is not identical to a beginner's for
    the same goal. It is applied to CONCEPTS before composition, so a lecture
    survives as long as any one concept it teaches survives. If the filter
    would leave nothing at all, the unfiltered set is used instead — the same
    "a valid goal never yields an empty playlist" guarantee
    _select_concepts_for_goal makes for the similarity floor.
    """
    selected, fallback_activated = await _select_concepts_for_goal(
        goal_text, track_concept_ids,
        top_k=DEFAULT_GOAL_TOP_K, similarity_floor=DEFAULT_GOAL_SIMILARITY_FLOOR,
        fallback_min_concepts=DEFAULT_GOAL_FALLBACK_MIN_CONCEPTS,
    )
    used_keyword_fallback = False
    if not selected:
        keyword_ids = await _keyword_fallback_match(goal_text, track_concept_ids, DEFAULT_GOAL_TOP_K)
        selected = [(cid, 0.0) for cid in keyword_ids]  # no semantic score available here
        used_keyword_fallback = True

    matched_concept_ids = [cid for cid, _sim in selected]
    similarity_by_concept = {cid: sim for cid, sim in selected}

    # Baseline difficulty filter — same rule, same prerequisite exemption as
    # /api/playlist. Order-preserving, so the relevance ranking below and the
    # ordered_concept_ids hint are both unaffected apart from the removals.
    concept_ids, baseline_dropped = await _apply_baseline_filter_to_concepts(
        matched_concept_ids, baseline,
    )
    baseline_filter_emptied = bool(matched_concept_ids) and not concept_ids
    if baseline_filter_emptied:
        # Never hand back an empty starter playlist purely because the
        # learner's baseline outran the difficulty of everything their goal
        # matched — fall back to the unfiltered set and report it.
        concept_ids, baseline_dropped = matched_concept_ids, []

    # Reuse the EXISTING KG composition + prerequisite-aware ordering as-is —
    # relevance order doubles as a reasonable ordered_concept_ids hint too.
    lectures = await kg_service.get_composed_playlist(
        user_id, concept_ids, ordered_concept_ids=concept_ids,
    )

    def _lecture_relevance(lec: dict) -> float:
        lecture_concept_ids = {
            ch.get("concept_id") for ch in lec.get("chunks", []) if ch.get("concept_id")
        }
        return max((similarity_by_concept.get(cid, 0.0) for cid in lecture_concept_ids), default=0.0)

    # get_composed_playlist already returns one row per DISTINCT lecture_id
    # (deduplication happens in its own Cypher query), so no further dedup
    # is needed here — just attach relevance and rank.
    by_relevance = sorted(lectures, key=lambda lec: -_lecture_relevance(lec))
    top_lectures = by_relevance[:max_lectures]

    # Selection is relevance-ranked; presentation ORDER restores
    # get_composed_playlist's own prerequisite/curriculum sequence for just
    # the selected subset, rather than leaving them in pure-relevance order.
    top_lecture_ids = {lec["lecture_id"] for lec in top_lectures}
    ordered_lectures = [lec for lec in lectures if lec["lecture_id"] in top_lecture_ids]

    return {
        "concept_ids": concept_ids,
        "lectures": ordered_lectures,
        "fallback_activated": fallback_activated,
        "used_keyword_fallback": used_keyword_fallback,
        "raw_lecture_count": len(lectures),
        "similarity_by_concept": similarity_by_concept,
        # Baseline provenance — which concepts the difficulty filter removed,
        # and whether it had to be waived to avoid an empty playlist.
        "baseline": baseline,
        "baseline_dropped_concept_ids": baseline_dropped,
        "baseline_filter_waived": baseline_filter_emptied,
    }


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


# =========================================================================
# REMEDIAL RECOMMENDATION LIFECYCLE
#
# Minimal state so the orchestrator can retire remediation that's no longer
# needed once a concept is Mastered — not a general recommendation history/
# lifecycle system. Three statuses only: active (just recommended, still
# relevant) and retired (concept since mastered). Nothing else in this app
# reads this collection; it exists solely to answer "is this remediation
# still relevant?" at Mastered time.
# =========================================================================

async def record_active_recommendations(user_id: str, concept_id: str, lectures: list[dict]) -> None:
    """Persist the lecture_ids just recommended for remediation as 'active'
    so retire_recommendations_for_concept can find them later. Idempotent —
    re-recommending the same lecture doesn't create a duplicate row."""
    if not lectures:
        return
    db = get_db()
    now = datetime.now(timezone.utc)
    for lec in lectures:
        lecture_id = lec.get("lecture_id")
        if not lecture_id:
            continue
        await db.remedial_recommendations.update_one(
            {"user_id": user_id, "concept_id": concept_id, "lecture_id": lecture_id},
            {"$setOnInsert": {
                "user_id": user_id, "concept_id": concept_id, "lecture_id": lecture_id,
                "status": "active", "created_at": now,
            }},
            upsert=True,
        )


async def retire_recommendations_for_concept(user_id: str, concept_id: str) -> list[str]:
    """Mark any ACTIVE remedial recommendations for this (learner, concept) as
    retired — the learner has mastered the concept, so the remediation is no
    longer relevant. Returns the retired lecture_ids so the caller can tell
    the frontend to stop showing them. Only rows for THIS concept in `active`
    status are touched — completed/consumed history (rows already retired)
    and unrelated concepts' recommendations are untouched. Safe to call
    repeatedly: a concept with no active rows is simply a no-op.
    """
    db = get_db()
    lecture_ids = [
        doc["lecture_id"] async for doc in
        db.remedial_recommendations.find({"user_id": user_id, "concept_id": concept_id, "status": "active"})
    ]
    if lecture_ids:
        await db.remedial_recommendations.update_many(
            {"user_id": user_id, "concept_id": concept_id, "status": "active"},
            {"$set": {"status": "retired", "retired_at": datetime.now(timezone.utc)}},
        )
    return lecture_ids