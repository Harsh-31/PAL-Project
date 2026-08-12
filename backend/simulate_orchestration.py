"""Reproducible end-to-end simulation of the integrated AdaptiveLearningOrchestrator
(Hybrid RL + Process KG + Recommendation Engine) across the four scenarios from
the integration spec: struggling / competent / mastered / high-performing learners.

Uses the REAL orchestrator (app.services.pal_agent.AdaptiveLearningOrchestrator)
and the REAL Hybrid RL controller (backed by an in-memory fake Mongo, so this
runs standalone with no live database). The Process KG is replaced by a
faithful in-memory reproduction of the exact same 5-rule table seeded in
app/database/neo4j_db.py (same thresholds, same actions) — this avoids
requiring a live Neo4j connection while keeping the KG's actual decision logic
under test. The Recommendation Engine is replaced by a canned stub, since real
recommendations require live Neo4j concept embeddings — but the orchestrator's
decision of WHETHER to call it is entirely real and unmocked.

Usage:
    cd backend
    python simulate_orchestration.py
"""
from __future__ import annotations
import asyncio
import random

from app.services import kg_service, recommender
from app.services.adaptive import persistence as persistence_module
from app.services.pal_agent import AdaptiveLearningOrchestrator

N_QUESTIONS = 8

# The exact rule table from app/database/neo4j_db.py::_seed_process_kg,
# reproduced here so the simulation doesn't need a live Neo4j connection.
_KG_RULES = [
    ("Frustrated", "OfferSimplerAnalogy", 0.40, "simplify_with_hobby_analogy"),
    ("Struggling", "AddRemedialContent", 0.55, "insert_prerequisite_video"),
    ("OnTrack", "ContinueBaseline", 0.70, "continue_normal"),
    ("Confident", "AdvanceDifficulty", 0.85, "offer_challenge_content"),
    ("Mastered", "SkipRedundant", 0.95, "skip_next_similar_chunk"),
]


class FakeKG:
    """In-memory stand-in for kg_service, faithful to the real Cypher logic in
    record_mastery/get_intervention/swap_intervention_state — no Neo4j
    required for this demo."""

    def __init__(self):
        self.mastery = 0.5
        self.attempts = 0
        self.last_kg_state = None  # mirrors MASTERS.last_kg_state

    async def get_mastery(self, user_id, concept_id):
        return self.mastery

    async def record_mastery(self, user_id, concept_id, delta):
        self.mastery = max(0.0, min(1.0, self.mastery + delta))
        self.attempts += 1
        return self.mastery

    async def get_attempts(self, user_id, concept_id):
        return self.attempts

    async def get_intervention(self, mastery):
        candidates = [(thr, state, rule, action) for state, rule, thr, action in _KG_RULES
                      if mastery <= thr]
        if not candidates:
            return None
        thr, state, rule, action = min(candidates, key=lambda c: c[0])
        return {"state": state, "rule": rule, "action": action, "thr": thr}

    async def swap_intervention_state(self, user_id, concept_id, new_state):
        previous = self.last_kg_state
        self.last_kg_state = new_state
        return previous


class FakeDB:
    """Minimal in-memory Motor-collection stand-in (find_one/insert_one/
    update_one/replace_one/find().sort().limit()) — same pattern used in
    tests/test_adaptive_persistence_integration.py, kept standalone here."""

    class _Cursor:
        def __init__(self, docs):
            self._docs = list(docs)

        def sort(self, field, direction=1):
            self._docs.sort(key=lambda d: d.get(field), reverse=(direction < 0))
            return self

        def limit(self, n):
            self._docs = self._docs[:n]
            return self

        def __aiter__(self):
            self._iter = iter(self._docs)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    class _Collection:
        def __init__(self):
            self.docs = {}
            self._auto_id = 0

        async def find_one(self, query):
            if "_id" in query:
                return self.docs.get(query["_id"])
            for d in self.docs.values():
                if all(d.get(k) == v for k, v in query.items()):
                    return d
            return None

        async def insert_one(self, doc):
            doc = dict(doc)
            if "_id" not in doc:
                self._auto_id += 1
                doc["_id"] = self._auto_id
            self.docs[doc["_id"]] = doc
            return doc

        async def update_one(self, query, update, upsert=False):
            _id = query["_id"]
            doc = self.docs.get(_id)
            if doc is None:
                if not upsert:
                    return
                doc = {"_id": _id}
                self.docs[_id] = doc
            if "$set" in update:
                doc.update(update["$set"])

        async def replace_one(self, query, doc, upsert=False):
            self.docs[query["_id"]] = dict(doc)

        def find(self, query):
            matched = [d for d in self.docs.values() if all(d.get(k) == v for k, v in query.items())]
            return FakeDB._Cursor(matched)

    def __init__(self):
        self._collections = {}

    def __getattr__(self, name):
        if name not in self._collections:
            self._collections[name] = FakeDB._Collection()
        return self._collections[name]


def _canned_recommendation(action: str, concept_id: str) -> list[dict]:
    if action == "insert_prerequisite_video":
        return [{"lecture_id": f"remedial-for-{concept_id}", "title": "Prerequisite refresher",
                  "for_concept_name": concept_id, "similarity": 0.81}]
    if action == "offer_challenge_content":
        return [{"lecture_id": f"challenge-for-{concept_id}", "title": "Advanced deep-dive",
                  "for_concept_name": concept_id, "similarity": 0.77, "challenge": True}]
    return []


async def _fake_recommend_for_intervention(user_id, action, concept_id, mastery):
    return _canned_recommendation(action, concept_id)


SCENARIOS = {
    "A_struggling": {"true_ability": 0.20, "starting_mastery": 0.50},
    "B_competent":  {"true_ability": 0.65, "starting_mastery": 0.60},
    "C_mastered":   {"true_ability": 0.90, "starting_mastery": 0.93},
    "D_high_performer": {"true_ability": 0.80, "starting_mastery": 0.78},
}


async def run_scenario(name: str, cfg: dict, seed: int) -> None:
    rng = random.Random(seed)
    fake_db = FakeDB()
    fake_kg = FakeKG()
    fake_kg.mastery = cfg["starting_mastery"]

    # Monkeypatch the module-level functions the orchestrator calls — this is
    # a standalone script (not pytest), so we patch and restore manually.
    orig = {
        "get_mastery": kg_service.get_mastery, "record_mastery": kg_service.record_mastery,
        "get_attempts": kg_service.get_attempts, "get_intervention": kg_service.get_intervention,
        "swap_intervention_state": kg_service.swap_intervention_state,
        "get_db": persistence_module.get_db,
        "recommend_for_intervention": recommender.recommend_for_intervention,
    }
    kg_service.get_mastery = fake_kg.get_mastery
    kg_service.record_mastery = fake_kg.record_mastery
    kg_service.get_attempts = fake_kg.get_attempts
    kg_service.get_intervention = fake_kg.get_intervention
    kg_service.swap_intervention_state = fake_kg.swap_intervention_state
    persistence_module.get_db = lambda: fake_db
    recommender.recommend_for_intervention = _fake_recommend_for_intervention

    try:
        orch = AdaptiveLearningOrchestrator()
        user_id, concept_id = f"sim-{name}", "concept-1"

        print(f"\n=== Scenario {name} (starting mastery={cfg['starting_mastery']}, "
              f"true_ability={cfg['true_ability']}) ===")

        for step in range(N_QUESTIONS):
            decision_result = await orch.select_difficulty(user_id, concept_id, "intermediate")
            difficulty_action = decision_result["action"]
            pending = decision_result["decision"].to_pending()

            rank = {"EASY": 0, "MEDIUM": 1, "HARD": 2}[difficulty_action]
            p_correct = max(0.05, min(0.95, cfg["true_ability"] - 0.15 * rank))
            correct = rng.random() < p_correct
            response_time = rng.uniform(3, 20)

            result = await orch.process_attempt(
                user_id=user_id, concept_id=concept_id, correct=correct,
                current_difficulty=decision_result["difficulty"],
                question_id=f"q{step}", response_time_sec=response_time, pending=pending,
            )

            rec_titles = [r["title"] for r in result["recommendations"]]
            cs = result["cognitive_state"]
            transition = f"{cs['previous']}->{cs['current']}" if cs["changed"] else f"{cs['current']} (same)"
            print(f"  t={step} difficulty={difficulty_action:<6} correct={correct!s:<5} "
                  f"mastery={result['beliefs']['mastery']:.2f} "
                  f"state={transition:<22} "
                  f"recommender_invoked={result['recommender_invoked']!s:<5} "
                  f"recs={rec_titles} next_difficulty={result['next_difficulty']}")

        print(f"  -> final mastery={fake_kg.mastery:.3f}, "
              f"final RL skill={result['rl']['learner_state']['skill']:.3f}")
    finally:
        kg_service.get_mastery = orig["get_mastery"]
        kg_service.record_mastery = orig["record_mastery"]
        kg_service.get_attempts = orig["get_attempts"]
        kg_service.get_intervention = orig["get_intervention"]
        kg_service.swap_intervention_state = orig["swap_intervention_state"]
        persistence_module.get_db = orig["get_db"]
        recommender.recommend_for_intervention = orig["recommend_for_intervention"]


async def main():
    print("Simulating 4 learner scenarios through the integrated orchestrator "
          "(Hybrid RL + Process KG + Recommendation Engine)...")
    for i, (name, cfg) in enumerate(SCENARIOS.items()):
        await run_scenario(name, cfg, seed=200 + i)
    print("\nDone. Each row shows: RL's independently-chosen difficulty, the "
          "actual answer outcome, the Process KG's independently-chosen "
          "intervention, and whether/what the Recommendation Engine returned.")


if __name__ == "__main__":
    asyncio.run(main())
