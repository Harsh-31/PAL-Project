"""Mongo-backed persistence for learner state, the shared Q-table, the shared
epsilon (decayed over time), and the per-decision explainability log.

Collections used (see app/database/mongo.py for indexes):
  learner_states     — one doc per (user_id, concept_id), the LearnerState
  q_values           — one doc per discretized-state key, {q: {EASY,MEDIUM,HARD}}
  rl_meta            — small singleton docs (currently just the shared epsilon)
  adaptive_decisions — append-only explainability log, one doc per interaction
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.database.mongo import get_db
from .config import AdaptiveConfig, DEFAULT_CONFIG
from .state import LearnerState


def _state_id(user_id: str, concept_id: str) -> str:
    return f"{user_id}:{concept_id}"


async def load_learner_state(user_id: str, concept_id: str) -> LearnerState:
    db = get_db()
    doc = await db.learner_states.find_one({"_id": _state_id(user_id, concept_id)})
    if not doc:
        return LearnerState(user_id=user_id, concept_id=concept_id)
    doc.pop("_id", None)
    return LearnerState.from_dict(doc)


async def save_learner_state(state: LearnerState) -> None:
    db = get_db()
    doc = state.to_dict()
    doc["_id"] = _state_id(state.user_id, state.concept_id)
    await db.learner_states.replace_one({"_id": doc["_id"]}, doc, upsert=True)


async def load_q_table() -> dict[str, dict[str, float]]:
    db = get_db()
    table: dict[str, dict[str, float]] = {}
    async for doc in db.q_values.find({}):
        table[doc["_id"]] = doc["q"]
    return table


async def save_q_row(state_key: str, q_row: dict[str, float]) -> None:
    db = get_db()
    await db.q_values.update_one({"_id": state_key}, {"$set": {"q": q_row}}, upsert=True)


async def load_epsilon(config: AdaptiveConfig = DEFAULT_CONFIG) -> float:
    db = get_db()
    doc = await db.rl_meta.find_one({"_id": "epsilon"})
    return doc["value"] if doc else config.q.epsilon


async def save_epsilon(value: float) -> None:
    db = get_db()
    await db.rl_meta.update_one({"_id": "epsilon"}, {"$set": {"value": value}}, upsert=True)


async def log_decision(entry: dict) -> None:
    db = get_db()
    entry = dict(entry)
    entry.setdefault("timestamp", datetime.now(timezone.utc))
    await db.adaptive_decisions.insert_one(entry)


async def get_decision_trace(user_id: str, concept_id: str | None = None,
                              limit: int = 200) -> list[dict]:
    """Explainability API backing store — the full RL decision trace for a
    learner (optionally scoped to one concept/session)."""
    db = get_db()
    query: dict = {"learner_id": user_id}
    if concept_id:
        query["session_id"] = _state_id(user_id, concept_id)
    out = []
    async for doc in db.adaptive_decisions.find(query).sort("timestep", 1).limit(limit):
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out
