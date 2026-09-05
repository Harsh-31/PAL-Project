"""Read-only traceability queries.

Every function here only reads. The audit service is an inspector, never a
mutator of learner state.

Collections it reads (written by the main app):
  users               — learner profile
  quiz_attempts       — one doc per answered question, carrying the full
                        orchestrator `trace` (TRACE-01)
  threshold_decisions — Threshold RL log: tau changes, actions, rewards, Q
                        (TRACE-02)
  adaptive_decisions  — Hybrid RL log: state, p_stat/p_rl blend, action, reward
  learner_states      — current Hybrid RL state vector per user x concept
"""
from __future__ import annotations

import re
from datetime import datetime, date
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from .store import get_db, get_neo


# ---------------------------------------------------------------------------
# JSON sanitising — Mongo types are not JSON-serialisable
# ---------------------------------------------------------------------------

def clean(obj: Any) -> Any:
    """Recursively convert Mongo/BSON types into JSON-safe values."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, float) and (obj != obj):  # NaN
        return None
    return obj


class UserNotFound(Exception):
    pass


class AmbiguousUser(Exception):
    def __init__(self, matches: list[dict]):
        self.matches = matches
        super().__init__("Multiple users match")


# ---------------------------------------------------------------------------
# User resolution — accept an id, an email, or a name
# ---------------------------------------------------------------------------

_PUBLIC_USER_FIELDS = {
    "name": 1, "email": 1, "onboarded": 1, "created_at": 1, "onboarded_at": 1,
    "goal": 1, "hobbies": 1, "baseline": 1, "evaluation_frequency": 1,
    "target_concept_ids": 1, "track_ids": 1,
}


def _ci_exact(value: str) -> dict:
    return {"$regex": f"^{re.escape(value)}$", "$options": "i"}


def _ci_contains(value: str) -> dict:
    return {"$regex": re.escape(value), "$options": "i"}


async def resolve_user(key: str) -> dict:
    """Resolve `key` (Mongo id | email | name, any case) to a user document.

    Raises UserNotFound, or AmbiguousUser when a name matches several people.
    """
    db = get_db()
    key = key.strip()

    # 1. Exact Mongo _id
    try:
        oid = ObjectId(key)
    except (InvalidId, TypeError):
        oid = None
    if oid is not None:
        doc = await db.users.find_one({"_id": oid}, _PUBLIC_USER_FIELDS)
        if doc:
            return doc

    # 2. Exact email, then exact name (case-insensitive)
    doc = await db.users.find_one({"email": _ci_exact(key)}, _PUBLIC_USER_FIELDS)
    if doc:
        return doc

    exact_name = [d async for d in db.users.find(
        {"name": _ci_exact(key)}, _PUBLIC_USER_FIELDS).limit(10)]
    if len(exact_name) == 1:
        return exact_name[0]
    if len(exact_name) > 1:
        raise AmbiguousUser([clean(d) for d in exact_name])

    # 3. Substring match on name or email
    partial = [d async for d in db.users.find(
        {"$or": [{"name": _ci_contains(key)}, {"email": _ci_contains(key)}]},
        _PUBLIC_USER_FIELDS,
    ).limit(10)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise AmbiguousUser([clean(d) for d in partial])

    raise UserNotFound(key)


async def list_users(q: str | None = None, limit: int = 100) -> list[dict]:
    """All learners with their traceability-record counts, busiest first."""
    db = get_db()
    query: dict = {}
    if q:
        query = {"$or": [{"name": _ci_contains(q)}, {"email": _ci_contains(q)}]}

    users = [u async for u in db.users.find(query, _PUBLIC_USER_FIELDS).limit(limit)]
    if not users:
        return []

    ids = [str(u["_id"]) for u in users]

    # One aggregation per collection rather than N queries per user.
    attempts = await _count_by(db.quiz_attempts, "user_id", ids)
    thresholds = await _count_by(db.threshold_decisions, "user_id", ids)
    rl = await _count_by(db.adaptive_decisions, "learner_id", ids)
    last_seen = await _max_by(db.quiz_attempts, "user_id", ids, "timestamp")

    out = []
    for u in users:
        uid = str(u["_id"])
        out.append({
            **clean(u),
            "id": uid,
            "counts": {
                "quiz_attempts": attempts.get(uid, 0),
                "threshold_decisions": thresholds.get(uid, 0),
                "rl_decisions": rl.get(uid, 0),
            },
            "last_activity": clean(last_seen.get(uid)),
        })
    out.sort(key=lambda r: r["counts"]["quiz_attempts"], reverse=True)
    return out


async def _count_by(coll, field: str, ids: list[str]) -> dict[str, int]:
    cursor = coll.aggregate([
        {"$match": {field: {"$in": ids}}},
        {"$group": {"_id": f"${field}", "n": {"$sum": 1}}},
    ])
    return {d["_id"]: d["n"] async for d in cursor}


# Concept id for an adaptive_decisions doc: it has no top-level concept_id, so
# fall back to learner_state.concept_id, then to the tail of session_id.
_RL_CONCEPT_EXPR = {
    "$ifNull": [
        "$concept_id",
        {"$ifNull": [
            "$learner_state.concept_id",
            {"$arrayElemAt": [{"$split": [{"$ifNull": ["$session_id", ""]}, ":"]}, 1]},
        ]},
    ]
}


async def _max_by(coll, field: str, ids: list[str], value_field: str) -> dict[str, Any]:
    cursor = coll.aggregate([
        {"$match": {field: {"$in": ids}}},
        {"$group": {"_id": f"${field}", "v": {"$max": f"${value_field}"}}},
    ])
    return {d["_id"]: d["v"] async for d in cursor}


# ---------------------------------------------------------------------------
# Per-user overview
# ---------------------------------------------------------------------------

async def user_overview(user: dict) -> dict:
    """Profile + per-concept activity summary for one learner."""
    db = get_db()
    uid = str(user["_id"])

    concepts: dict[str, dict] = {}

    cursor = db.quiz_attempts.aggregate([
        {"$match": {"user_id": uid}},
        {"$group": {
            "_id": "$concept_id",
            "attempts": {"$sum": 1},
            "correct": {"$sum": {"$cond": ["$correct", 1, 0]}},
            "first": {"$min": "$timestamp"},
            "last": {"$max": "$timestamp"},
        }},
    ])
    async for d in cursor:
        cid = d["_id"]
        concepts[cid] = {
            "concept_id": cid,
            "attempts": d["attempts"],
            "correct": d["correct"],
            "accuracy": round(d["correct"] / d["attempts"], 3) if d["attempts"] else None,
            "first_attempt": clean(d["first"]),
            "last_attempt": clean(d["last"]),
            "threshold_decisions": 0,
            "rl_decisions": 0,
        }

    for coll, field, key, group_by in (
        (db.threshold_decisions, "user_id", "threshold_decisions", "$concept_id"),
        # adaptive_decisions has no top-level concept_id — it lives in
        # learner_state.concept_id and in session_id ("{user_id}:{concept_id}").
        (db.adaptive_decisions, "learner_id", "rl_decisions", _RL_CONCEPT_EXPR),
    ):
        cursor = coll.aggregate([
            {"$match": {field: uid}},
            {"$group": {"_id": group_by, "n": {"$sum": 1}}},
        ])
        async for d in cursor:
            cid = d["_id"]
            if cid is None:
                continue
            concepts.setdefault(cid, {
                "concept_id": cid, "attempts": 0, "correct": 0, "accuracy": None,
                "first_attempt": None, "last_attempt": None,
                "threshold_decisions": 0, "rl_decisions": 0,
            })
            concepts[cid][key] = d["n"]

    # Current Hybrid RL state vector + live mastery per concept
    async for s in db.learner_states.find({"user_id": uid}):
        cid = s.get("concept_id")
        if cid is None:
            continue
        row = concepts.setdefault(cid, {
            "concept_id": cid, "attempts": 0, "correct": 0, "accuracy": None,
            "first_attempt": None, "last_attempt": None,
            "threshold_decisions": 0, "rl_decisions": 0,
        })
        row["learner_state"] = clean({k: v for k, v in s.items() if k != "_id"})

    names = await concept_names(list(concepts))
    for cid, row in concepts.items():
        row["concept_name"] = names.get(cid)

    rows = sorted(concepts.values(),
                  key=lambda r: (r["last_attempt"] or "", r["attempts"]), reverse=True)

    return {
        "user": {**clean(user), "id": uid},
        "totals": {
            "quiz_attempts": await db.quiz_attempts.count_documents({"user_id": uid}),
            "threshold_decisions": await db.threshold_decisions.count_documents({"user_id": uid}),
            "rl_decisions": await db.adaptive_decisions.count_documents({"learner_id": uid}),
            "concepts": len(rows),
        },
        "concepts": rows,
    }


# ---------------------------------------------------------------------------
# Attempts + decision logs
# ---------------------------------------------------------------------------

async def attempts(user: dict, concept_id: str | None, limit: int, skip: int) -> dict:
    """Attempt list, newest first. Summary rows only — the full trace comes
    from the pathway endpoint."""
    db = get_db()
    uid = str(user["_id"])
    query: dict = {"user_id": uid}
    if concept_id:
        query["concept_id"] = concept_id

    total = await db.quiz_attempts.count_documents(query)
    rows = []
    cursor = db.quiz_attempts.find(query).sort("timestamp", -1).skip(skip).limit(limit)
    async for a in cursor:
        trace = a.get("trace") or {}
        intervention = trace.get("intervention") or {}
        cog = trace.get("cognitive_state") or {}
        rows.append({
            "question_id": a.get("question_id"),
            "concept_id": a.get("concept_id"),
            "timestamp": clean(a.get("timestamp")),
            "correct": a.get("correct"),
            "difficulty": a.get("difficulty"),
            "time_taken_sec": a.get("time_taken_sec"),
            "mastery": (trace.get("beliefs") or {}).get("mastery"),
            "cognitive_state": cog.get("current") or intervention.get("state"),
            "state_changed": cog.get("changed"),
            "rule": intervention.get("rule"),
            "action": intervention.get("action"),
            # Every rule the state triggered, not just the priority-0 one.
            # Older records predate the additive-rules change and carry only
            # the single pair, so fall back to it.
            "rules": intervention.get("rules") or (
                [intervention["rule"]] if intervention.get("rule") else []),
            "actions": intervention.get("actions") or intervention.get("all_actions") or (
                [intervention["action"]] if intervention.get("action") else []),
            "content_actions": trace.get("content_actions") or [],
            "recommendation_count": len(trace.get("recommendations") or []),
            "tau_struggling": intervention.get("tau_struggling"),
            "tau_mastered": intervention.get("tau_mastered"),
            "next_difficulty": trace.get("next_difficulty"),
            "decision_ref": _attempt_decision_ref(a),
            "has_trace": bool(trace),
        })
    return {"total": total, "skip": skip, "limit": limit, "attempts": rows}


def _attempt_decision_ref(attempt: dict) -> str | None:
    """Best-effort decision_ref for an attempt.

    Records written after the traceability commit carry a top-level
    `decision_ref` string. Older records only have
    `trace.threshold_update.threshold_decision_ref`, which was a dict of
    {user_id, concept_id, tau_timestep} — normalise that to the canonical
    `threshold:{user}:{concept}:{t}` string so both generations resolve.
    """
    ref = attempt.get("decision_ref")
    if isinstance(ref, str) and ref:
        return ref

    tu = (attempt.get("trace") or {}).get("threshold_update") or {}
    for candidate in (tu.get("decision_ref"), tu.get("threshold_decision_ref")):
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, dict):
            u, c, t = (candidate.get("user_id"), candidate.get("concept_id"),
                       candidate.get("tau_timestep"))
            if u and c and t is not None:
                return f"threshold:{u}:{c}:{t}"
    return None


async def _find_threshold_decision(ref: str | None, attempt: dict) -> tuple[dict | None, str]:
    """Resolve a threshold decision, reporting how it was found."""
    db = get_db()
    if ref:
        doc = await db.threshold_decisions.find_one({"decision_ref": ref})
        if doc:
            return clean(doc), "decision_ref"

    # Legacy fallback: match on the (user, concept, tau_timestep) triple the
    # ref encodes — those records were written before decision_ref existed.
    tau_ts = None
    if ref:
        tail = ref.rsplit(":", 1)[-1]
        if tail.isdigit():
            tau_ts = int(tail)
    if tau_ts is None:
        intervention = (attempt.get("trace") or {}).get("intervention") or {}
        tau_ts = intervention.get("tau_timestep")

    if tau_ts is not None:
        doc = await db.threshold_decisions.find_one({
            "user_id": attempt.get("user_id"),
            "concept_id": attempt.get("concept_id"),
            "tau_timestep": tau_ts,
        })
        if doc:
            return clean(doc), "reconstructed:user+concept+tau_timestep"
    return None, "unresolved"


async def threshold_decisions(user: dict, concept_id: str | None, limit: int = 200) -> dict:
    db = get_db()
    query: dict = {"user_id": str(user["_id"])}
    if concept_id:
        query["concept_id"] = concept_id
    rows = [clean(d) async for d in
            db.threshold_decisions.find(query).sort("tau_timestep", 1).limit(limit)]
    return {"count": len(rows), "decisions": rows}


async def rl_decisions(user: dict, concept_id: str | None, limit: int = 200) -> dict:
    db = get_db()
    uid = str(user["_id"])
    query: dict = {"learner_id": uid}
    if concept_id:
        # session_id is "{user_id}:{concept_id}"; the concept also appears on
        # the embedded learner_state, and some docs carry it top-level.
        query["$or"] = [
            {"session_id": f"{uid}:{concept_id}"},
            {"concept_id": concept_id},
            {"learner_state.concept_id": concept_id},
        ]
    rows = [clean(d) async for d in
            db.adaptive_decisions.find(query).sort("timestep", 1).limit(limit)]
    return {"count": len(rows), "decisions": rows}


# ---------------------------------------------------------------------------
# The full pathway: mastery -> thresholds -> state -> rule -> action
# ---------------------------------------------------------------------------

async def pathway(user: dict, question_id: str) -> dict:
    """Reconstruct the end-to-end decision chain for one answered question."""
    db = get_db()
    uid = str(user["_id"])

    attempt = await db.quiz_attempts.find_one({"question_id": question_id, "user_id": uid})
    if not attempt:
        return {}

    trace = attempt.get("trace") or {}
    ref = _attempt_decision_ref(attempt)
    tdec, resolution = await _find_threshold_decision(ref, attempt)

    # Hybrid RL record for this exact question (adaptive_decisions carries question_id)
    rl_doc = await db.adaptive_decisions.find_one({
        "learner_id": uid, "question_id": question_id,
    })

    beliefs = trace.get("beliefs") or {}
    intervention = trace.get("intervention") or {}
    tu = trace.get("threshold_update") or {}
    names = await concept_names([attempt.get("concept_id")])

    return {
        "question_id": question_id,
        "user": {"id": uid, "name": user.get("name"), "email": user.get("email")},
        "concept_id": attempt.get("concept_id"),
        "concept_name": names.get(attempt.get("concept_id")),
        "timestamp": clean(attempt.get("timestamp")),
        "correct": attempt.get("correct"),
        "difficulty": attempt.get("difficulty"),
        "selected_index": attempt.get("selected_index"),
        "correct_index": attempt.get("correct_index"),
        "time_taken_sec": attempt.get("time_taken_sec"),
        "lecture_id": attempt.get("lecture_id"),
        "chunk_id": attempt.get("chunk_id"),

        # Layer 1 — mastery belief (Process KG / User KG)
        "mastery": {
            "before": beliefs.get("mastery_before"),
            "after": beliefs.get("mastery"),
            "predicted": beliefs.get("predicted"),
            "confidence": trace.get("confidence"),
        },

        # Layer 2 — personalised thresholds (Threshold RL)
        "thresholds": {
            "tau_struggling": tu.get("tau_struggling", intervention.get("tau_struggling")),
            "tau_mastered": tu.get("tau_mastered", intervention.get("tau_mastered")),
            "tau_timestep": intervention.get("tau_timestep"),
            "updated_this_step": tu.get("updated_this_step"),
            "decision_ref": ref,
            "decision_ref_resolution": resolution,
            "threshold_decision": tdec,
        },

        # Layer 3 — cognitive state (mastery classified against the thresholds)
        "cognitive_state": clean(trace.get("cognitive_state")),

        # Layer 4 — Process-KG rule and the pedagogical action it fired
        "intervention": clean(intervention),

        # Layer 5 — content the action produced
        "recommendations": clean(trace.get("recommendations") or []),
        "content_actions": clean(trace.get("content_actions") or []),
        "recommender_invoked": trace.get("recommender_invoked"),
        "recommender_failed": trace.get("recommender_failed"),
        "retired_lecture_ids": clean(trace.get("retired_lecture_ids") or []),

        # Difficulty provenance (Hybrid RL)
        "rl": clean(trace.get("rl")),
        "rl_decision_record": clean(rl_doc) if rl_doc else None,
        "next_difficulty": trace.get("next_difficulty"),
        "reflection": trace.get("reflection"),
        "raw_trace": clean(trace),
    }


async def timeline(user: dict, concept_id: str | None, limit: int = 300) -> dict:
    """All three decision logs merged into one chronological stream."""
    db = get_db()
    uid = str(user["_id"])
    events: list[dict] = []

    q: dict = {"user_id": uid}
    if concept_id:
        q["concept_id"] = concept_id
    async for a in db.quiz_attempts.find(q).sort("timestamp", -1).limit(limit):
        trace = a.get("trace") or {}
        intervention = trace.get("intervention") or {}
        cog = trace.get("cognitive_state") or {}
        events.append({
            "kind": "attempt",
            "timestamp": clean(a.get("timestamp")),
            "concept_id": a.get("concept_id"),
            "question_id": a.get("question_id"),
            "summary": {
                "correct": a.get("correct"),
                "difficulty": a.get("difficulty"),
                "mastery": (trace.get("beliefs") or {}).get("mastery"),
                "state": cog.get("current") or intervention.get("state"),
                "state_changed": cog.get("changed"),
                "rule": intervention.get("rule"),
                "action": intervention.get("action"),
                "rules": intervention.get("rules") or (
                    [intervention["rule"]] if intervention.get("rule") else []),
            },
        })

    async for d in db.threshold_decisions.find(q).sort("tau_timestep", -1).limit(limit):
        events.append({
            "kind": "threshold_decision",
            "timestamp": clean(d.get("timestamp")),
            "concept_id": d.get("concept_id"),
            "summary": {
                "tau_timestep": d.get("tau_timestep"),
                "tau_struggling": [d.get("tau_struggling_before"), d.get("tau_struggling_after")],
                "tau_mastered": [d.get("tau_mastered_before"), d.get("tau_mastered_after")],
                "action_struggling": d.get("action_struggling"),
                "action_mastered": d.get("action_mastered"),
                "mastery": [d.get("mastery_before"), d.get("mastery_after")],
                "accuracy": [d.get("accuracy_before"), d.get("accuracy_after")],
                "reward": (d.get("reward") or {}).get("total"),
            },
        })

    rq: dict = {"learner_id": uid}
    if concept_id:
        rq["$or"] = [
            {"session_id": f"{uid}:{concept_id}"},
            {"concept_id": concept_id},
            {"learner_state.concept_id": concept_id},
        ]
    async for d in db.adaptive_decisions.find(rq).sort("timestep", -1).limit(limit):
        events.append({
            "kind": "rl_decision",
            "timestamp": clean(d.get("timestamp")),
            "concept_id": (d.get("concept_id")
                           or (d.get("learner_state") or {}).get("concept_id")
                           or (d.get("session_id") or "").split(":", 1)[-1]),
            "question_id": d.get("question_id"),
            "summary": {
                "timestep": d.get("timestep"),
                "action": d.get("selected_action") or d.get("action"),
                "reward": (d.get("reward") or {}).get("total") if isinstance(d.get("reward"), dict) else d.get("reward"),
                "blend_weight": d.get("blend_weight"),
            },
        })

    events.sort(key=lambda e: (e.get("timestamp") or ""), reverse=True)
    return {"count": len(events), "events": events[:limit]}


# ---------------------------------------------------------------------------
# Neo4j: live symbolic layer (optional)
# ---------------------------------------------------------------------------

async def concept_names(concept_ids: list[str | None]) -> dict[str, str]:
    ids = [c for c in concept_ids if c]
    driver = get_neo()
    if not driver or not ids:
        return {}
    try:
        async with driver.session() as s:
            r = await s.run(
                "MATCH (k:Concept) WHERE k.id IN $ids RETURN k.id AS id, k.name AS name",
                ids=ids,
            )
            return {rec["id"]: rec["name"] async for rec in r}
    except Exception:  # noqa: BLE001 — names are cosmetic
        return {}


async def kg_snapshot(user: dict, concept_id: str | None = None) -> dict:
    """Live User-KG MASTERS edges + the Process-KG state->rule->action map.

    This is the *current* symbolic state, as opposed to the historical values
    captured in each attempt's trace — useful for confirming that what the
    graph says now matches what the last decision recorded.
    """
    driver = get_neo()
    if not driver:
        return {"available": False, "reason": "Neo4j unavailable — see server log"}

    uid = str(user["_id"])
    out: dict = {"available": True, "masters": [], "process_kg": []}
    try:
        async with driver.session() as s:
            cypher = """
                MATCH (u:Learner {id:$uid})-[m:MASTERS]->(k:Concept)
                WHERE $cid IS NULL OR k.id = $cid
                RETURN k.id AS concept_id, k.name AS concept_name,
                       m.score AS mastery, m.attempts AS attempts,
                       m.tau_struggling AS tau_struggling,
                       m.tau_mastered AS tau_mastered,
                       m.tau_timestep AS tau_timestep,
                       m.last_state AS last_state,
                       m.updated_at AS updated_at
                ORDER BY concept_id
            """
            r = await s.run(cypher, uid=uid, cid=concept_id)
            out["masters"] = [dict(rec) async for rec in r]

            r = await s.run("""
                MATCH (s:CognitiveState)-[:TRIGGERS]->(r:InterventionRule)
                RETURN s.name AS state, collect({rule: r.name, action: r.action}) AS rules
                ORDER BY state
            """)
            out["process_kg"] = [dict(rec) async for rec in r]

            if not out["masters"]:
                # Distinguish "learner never assessed" from "this Neo4j has no
                # learner data at all" (e.g. NEO4J_URI now points at a fresh
                # instance while the Mongo traces below are older).
                r = await s.run("MATCH (u:Learner) RETURN count(u) AS learners")
                rec = await r.single()
                r2 = await s.run("MATCH ()-[m:MASTERS]->() RETURN count(m) AS edges")
                rec2 = await r2.single()
                learners = rec["learners"] if rec else 0
                edges = rec2["edges"] if rec2 else 0
                out["graph_totals"] = {"learners": learners, "masters_edges": edges}
                out["note"] = (
                    "This Neo4j instance holds no MASTERS edges for any learner — "
                    "the live User-KG state is empty (a fresh/reset instance). "
                    "Historical traces in MongoDB are unaffected."
                    if edges == 0 else
                    "No MASTERS edge for this learner yet — they have not been "
                    "assessed on any concept on this Neo4j instance."
                )
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return clean(out)
