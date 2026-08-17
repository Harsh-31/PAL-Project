"""Neo4j driver — holds the three-layer neuro-symbolic Knowledge Graph.

Layers (per PRD):
  T-Box (static):   Course, Concept, LectureChunk, AssessmentItem + prerequisite/contains edges
  A-Box (dynamic):  Learner (hobbies), Interaction, MASTERS/CONSUMED edges w/ scores
  Process KG:       CognitiveState, InterventionRule + deterministic triggers
"""
from __future__ import annotations
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
    """Seed the 3-state Process KG.

    Thresholds are NOT on InterventionRule nodes — they live as learned
    per-learner values (tau_struggling, tau_mastered) on each MASTERS edge,
    managed by the Threshold RL.  The rules here are pure state→action maps.
    """
    rules = [
        # (state, [(rule, action), ...])
        ("Struggling", [
            ("OfferSimplerAnalogy", "simplify_with_hobby_analogy"),
            ("AddRemedialContent", "insert_prerequisite_video"),
        ]),
        ("OnTrack", [
            ("OnTrackContinue", "continue_normal"),
        ]),
        ("Mastered", [
            ("MasteredChallenge", "offer_challenge_content"),
        ]),
    ]
    async with neo.driver.session() as s:
        # Remove legacy 5-state nodes that no longer exist.
        await s.run(
            """
            MATCH (st:CognitiveState)
            WHERE st.name IN ['Frustrated', 'Confident']
            OPTIONAL MATCH (st)-[t:TRIGGERS]->(r:InterventionRule)
            DETACH DELETE st
            WITH r WHERE r IS NOT NULL
            AND NOT EXISTS { MATCH ()-[:TRIGGERS]->(r) }
            DELETE r
            """
        )
        # Remove the legacy SkipRedundant rule (Mastered no longer skips).
        await s.run(
            """
            MATCH (r:InterventionRule {name: 'SkipRedundant'})
            DETACH DELETE r
            """
        )
        for state, actions in rules:
            for priority, (rule, action) in enumerate(actions):
                await s.run(
                    """
                    MERGE (st:CognitiveState {name:$state})
                    MERGE (r:InterventionRule {name:$rule})
                      SET r.action=$action
                    MERGE (st)-[t:TRIGGERS]->(r)
                      SET t.priority=$priority
                    """,
                    state=state, rule=rule, action=action, priority=priority,
                )
