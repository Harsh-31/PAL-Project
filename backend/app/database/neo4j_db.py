"""Neo4j driver — holds the three-layer neuro-symbolic Knowledge Graph.

Layers (per PRD):
  T-Box (static):   Course, Concept, LectureChunk, AssessmentItem + prerequisite/contains edges
  A-Box (dynamic):  Learner (hobbies), Interaction, MASTERS/CONSUMED edges w/ scores
  Process KG:       CognitiveState, InterventionRule + deterministic triggers
"""
from neo4j import AsyncGraphDatabase, AsyncDriver
from app.config import settings


class Neo4jDB:
    driver: AsyncDriver | None = None


neo = Neo4jDB()


async def connect_to_neo4j() -> None:
    neo.driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    await neo.driver.verify_connectivity()
    await _init_schema()
    await _seed_process_kg()
    print("[Neo4j] connected & schema/process-KG ensured")


async def close_neo4j_connection() -> None:
    if neo.driver:
        await neo.driver.close()
        print("[Neo4j] connection closed")


def get_driver() -> AsyncDriver:
    assert neo.driver is not None, "Neo4j driver not initialised"
    return neo.driver


async def _init_schema() -> None:
    """Uniqueness constraints — idempotent."""
    stmts = [
        "CREATE CONSTRAINT course_id IF NOT EXISTS FOR (c:Course) REQUIRE c.id IS UNIQUE",
        "CREATE CONSTRAINT concept_id IF NOT EXISTS FOR (c:Concept) REQUIRE c.id IS UNIQUE",
        "CREATE CONSTRAINT chunk_id  IF NOT EXISTS FOR (l:LectureChunk) REQUIRE l.id IS UNIQUE",
        "CREATE CONSTRAINT learner_id IF NOT EXISTS FOR (u:Learner) REQUIRE u.id IS UNIQUE",
        "CREATE CONSTRAINT state_name IF NOT EXISTS FOR (s:CognitiveState) REQUIRE s.name IS UNIQUE",
        "CREATE CONSTRAINT rule_name  IF NOT EXISTS FOR (r:InterventionRule) REQUIRE r.name IS UNIQUE",
    ]
    async with neo.driver.session() as s:
        for q in stmts:
            await s.run(q)


async def _seed_process_kg() -> None:
    """Seed pedagogical rules — the deterministic anchor that stops the LLM hallucinating."""
    rules = [
        # (state, rule, mastery_lt, action)
        ("Frustrated", "OfferSimplerAnalogy", 0.4, "simplify_with_hobby_analogy"),
        ("Struggling", "AddRemedialContent", 0.55, "insert_prerequisite_video"),
        # NOTE: action reinterpreted from "raise_question_difficulty" — question
        # difficulty is now (and was already, in practice) the Hybrid RL
        # controller's sole authority. This rule stays named AdvanceDifficulty
        # (backward compatible — nothing keys off the action string persisting
        # across restarts) but now maps to a content-side enrichment action so
        # it can never conflict with the RL difficulty decision.
        ("Confident", "AdvanceDifficulty", 0.85, "offer_challenge_content"),
        ("Mastered", "SkipRedundant", 0.95, "skip_next_similar_chunk"),
        ("OnTrack", "ContinueBaseline", 0.7, "continue_normal"),
    ]
    async with neo.driver.session() as s:
        for state, rule, thr, action in rules:
            await s.run(
                """
                MERGE (st:CognitiveState {name:$state})
                MERGE (r:InterventionRule {name:$rule})
                  SET r.mastery_threshold=$thr, r.action=$action
                MERGE (st)-[:TRIGGERS]->(r)
                """,
                state=state, rule=rule, thr=thr, action=action,
            )
