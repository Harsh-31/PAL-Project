"""Standalone PAL Traceability Audit API + UI.

Run it separately from the main API (which stays on :8000):

    uvicorn audit_service.main:app --reload --port 8001

Then open http://127.0.0.1:8001/ — the audit UI is served from this app, so
nothing is added to the learner-facing React frontend.

Every route is read-only. Optionally set AUDIT_TOKEN in the environment to
require an `X-Audit-Token` header (or `?token=`) on API calls.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from . import queries as q
from .store import AUDIT_TOKEN, connect, disconnect, store

_STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Only connects to the datastores — no course sync, no Process-KG seeding.
    await connect()
    yield
    await disconnect()


app = FastAPI(
    title="PAL Traceability Audit API",
    version="1.0.0",
    description="Read-only inspector for per-learner decision traceability.",
    lifespan=lifespan,
)


async def require_token(request: Request) -> None:
    if not AUDIT_TOKEN:
        return
    supplied = request.headers.get("x-audit-token") or request.query_params.get("token")
    if supplied != AUDIT_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing audit token")


async def _user(user_key: str) -> dict:
    """Resolve a learner by id / email / name, or fail with a helpful error."""
    try:
        return await q.resolve_user(user_key)
    except q.AmbiguousUser as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ambiguous_user",
                "message": f"{len(exc.matches)} learners match '{user_key}' — "
                           f"retry with an exact email or id.",
                "matches": [
                    {"id": m["_id"], "name": m.get("name"), "email": m.get("email")}
                    for m in exc.matches
                ],
            },
        )
    except q.UserNotFound:
        raise HTTPException(status_code=404, detail=f"No learner matches '{user_key}'")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def ui():
    return FileResponse(_STATIC / "index.html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    db = store.db
    return {
        "status": "ok",
        "mongo_db": settings.MONGODB_DB,
        "neo4j": "connected" if store.neo else f"unavailable ({store.neo_error})",
        "auth": "token required" if AUDIT_TOKEN else "open (no token set)",
        "collections": {
            "users": await db.users.count_documents({}),
            "quiz_attempts": await db.quiz_attempts.count_documents({}),
            "threshold_decisions": await db.threshold_decisions.count_documents({}),
            "adaptive_decisions": await db.adaptive_decisions.count_documents({}),
            "learner_states": await db.learner_states.count_documents({}),
        },
    }


@app.get("/api/users", dependencies=[Depends(require_token)])
async def users(
    q_: str | None = Query(None, alias="q", description="name or email substring"),
    limit: int = Query(100, ge=1, le=500),
):
    """Every learner with their traceability-record counts."""
    return {"users": await q.list_users(q_, limit)}


@app.get("/api/users/{user_key}", dependencies=[Depends(require_token)])
async def overview(user_key: str):
    """Profile + per-concept activity for one learner.

    `user_key` may be a Mongo id, an email, or a name.
    """
    return await q.user_overview(await _user(user_key))


@app.get("/api/users/{user_key}/attempts", dependencies=[Depends(require_token)])
async def attempts(
    user_key: str,
    concept_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    skip: int = Query(0, ge=0),
):
    """Answered questions, newest first — one row per decision point."""
    return await q.attempts(await _user(user_key), concept_id, limit, skip)


@app.get("/api/users/{user_key}/pathway/{question_id}",
         dependencies=[Depends(require_token)])
async def pathway(user_key: str, question_id: str):
    """The full chain for one answer:
    mastery -> personalised thresholds -> cognitive state -> Process-KG rule
    -> pedagogical action -> recommendations, plus the Hybrid-RL difficulty
    provenance and the linked Threshold-RL decision record.
    """
    user = await _user(user_key)
    result = await q.pathway(user, question_id)
    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"No attempt for question '{question_id}' by {user.get('email')}",
        )
    return result


@app.get("/api/users/{user_key}/threshold-decisions",
         dependencies=[Depends(require_token)])
async def threshold_decisions(user_key: str, concept_id: str | None = None,
                              limit: int = Query(200, ge=1, le=1000)):
    """Threshold-RL log: tau_struggling/tau_mastered moves, the actions that
    caused them, rewards, and Q-values before/after."""
    return await q.threshold_decisions(await _user(user_key), concept_id, limit)


@app.get("/api/users/{user_key}/rl-decisions", dependencies=[Depends(require_token)])
async def rl_decisions(user_key: str, concept_id: str | None = None,
                       limit: int = Query(200, ge=1, le=1000)):
    """Hybrid-RL log: learner state, p_stat/p_rl blend, chosen difficulty,
    reward, Q-values."""
    return await q.rl_decisions(await _user(user_key), concept_id, limit)


@app.get("/api/users/{user_key}/timeline", dependencies=[Depends(require_token)])
async def timeline(user_key: str, concept_id: str | None = None,
                   limit: int = Query(300, ge=1, le=1000)):
    """All three decision logs merged chronologically."""
    return await q.timeline(await _user(user_key), concept_id, limit)


@app.get("/api/users/{user_key}/kg", dependencies=[Depends(require_token)])
async def kg(user_key: str, concept_id: str | None = None):
    """Live Neo4j view: the learner's MASTERS edges (mastery + learned taus)
    and the Process-KG state->rule->action map."""
    return await q.kg_snapshot(await _user(user_key), concept_id)


@app.exception_handler(q.UserNotFound)
async def _unf(request: Request, exc: q.UserNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})
