"""MongoDB persistence for the Threshold RL.

Collections:
  threshold_q_values  — two docs: _id="struggling" and _id="mastered",
                        each holding {table: {state_key: {action: q_val}}}
  threshold_meta      — singleton epsilon
  threshold_decisions — append-only explainability log
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.database.mongo import get_db
from .config import ThresholdRLConfig, DEFAULT_THRESHOLD_CONFIG


async def load_q_tables() -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    db = get_db()
    struggling: dict[str, dict[str, float]] = {}
    mastered: dict[str, dict[str, float]] = {}
    doc_s = await db.threshold_q_values.find_one({"_id": "struggling"})
    if doc_s and "table" in doc_s:
        struggling = doc_s["table"]
    doc_m = await db.threshold_q_values.find_one({"_id": "mastered"})
    if doc_m and "table" in doc_m:
        mastered = doc_m["table"]
    return struggling, mastered


async def save_q_tables(
    struggling: dict[str, dict[str, float]],
    mastered: dict[str, dict[str, float]],
) -> None:
    db = get_db()
    await db.threshold_q_values.update_one(
        {"_id": "struggling"}, {"$set": {"table": struggling}}, upsert=True,
    )
    await db.threshold_q_values.update_one(
        {"_id": "mastered"}, {"$set": {"table": mastered}}, upsert=True,
    )


async def load_epsilon(config: ThresholdRLConfig = DEFAULT_THRESHOLD_CONFIG) -> float:
    db = get_db()
    doc = await db.threshold_meta.find_one({"_id": "epsilon"})
    return doc["value"] if doc else config.q.epsilon


async def save_epsilon(value: float) -> None:
    db = get_db()
    await db.threshold_meta.update_one(
        {"_id": "epsilon"}, {"$set": {"value": value}}, upsert=True,
    )


async def log_decision(entry: dict, *, decision_ref: str | None = None) -> None:
    db = get_db()
    entry = dict(entry)
    entry.setdefault("timestamp", datetime.now(timezone.utc))
    if decision_ref:
        entry["decision_ref"] = decision_ref
    await db.threshold_decisions.insert_one(entry)


async def load_interaction_buffer(user_id: str, concept_id: str) -> dict | None:
    db = get_db()
    return await db.threshold_meta.find_one(
        {"_id": f"buffer:{user_id}:{concept_id}"}
    )


async def save_interaction_buffer(user_id: str, concept_id: str, buf: dict) -> None:
    db = get_db()
    doc = dict(buf)
    doc["_id"] = f"buffer:{user_id}:{concept_id}"
    await db.threshold_meta.replace_one({"_id": doc["_id"]}, doc, upsert=True)


async def get_decision_trace(user_id: str, concept_id: str,
                              limit: int = 200) -> list[dict]:
    """Explainability API backing store — the full Threshold RL decision trace
    for a learner×concept pair.  Returns every threshold adjustment event
    (tau_struggling/tau_mastered changes, actions chosen, rewards, Q-values
    before/after) ordered by tau_timestep ascending.
    """
    db = get_db()
    query: dict = {"user_id": user_id, "concept_id": concept_id}
    out: list[dict] = []
    async for doc in db.threshold_decisions.find(query).sort("tau_timestep", 1).limit(limit):
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out


async def get_decision_by_ref(decision_ref: str) -> dict | None:
    """Look up a single threshold decision by its decision_ref identifier.

    The decision_ref format is ``threshold:{user_id}:{concept_id}:{tau_timestep}``
    and is deterministically generated at interaction time so that every quiz
    attempt can be linked back to the exact threshold decision that produced the
    cognitive-state classification it was evaluated under.
    """
    db = get_db()
    doc = await db.threshold_decisions.find_one({"decision_ref": decision_ref})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc
