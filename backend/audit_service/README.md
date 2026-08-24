# PAL Traceability Audit Service

A **standalone, read-only** inspector for per-learner decision traceability.

It is deliberately *not* part of the learner-facing PAL system:

| | Main PAL app | This audit service |
|---|---|---|
| App | `app.main:app` | `audit_service.main:app` |
| Port | 8000 | **8001** |
| UI | React frontend (`frontend/`, :5173) | its own page at `http://127.0.0.1:8001/` |
| Auth | learner JWT — you only see *your own* data | none by default; any learner queryable by name/email/id |
| Writes | yes | **never** — reads only |
| Startup | syncs courses, seeds Process KG | connects to Mongo + Neo4j and nothing else |

The only thing it shares with the main app is `app.config.settings`, so database
credentials stay in one `.env`.

## Run it

The main API does **not** need to be running.

```bash
cd backend
source venv/bin/activate
uvicorn audit_service.main:app --reload --port 8001
```

Then open <http://127.0.0.1:8001/> for the UI, or
<http://127.0.0.1:8001/docs> for the OpenAPI console.

### Optional token

By default there is no auth, which is fine for a locally-bound admin tool.
To require one:

```bash
AUDIT_TOKEN=some-secret uvicorn audit_service.main:app --port 8001
```

Then send `X-Audit-Token: some-secret` (the UI has a field for it), or `?token=`.

## API

`{user_key}` accepts a **Mongo id, an email, or a name** (case-insensitive).
A name matching several learners returns `409` with the candidate list.

| Endpoint | What it answers |
|---|---|
| `GET /api/health` | connection status + collection counts |
| `GET /api/users?q=` | every learner with their record counts |
| `GET /api/users/{user_key}` | profile + per-concept activity summary |
| `GET /api/users/{user_key}/attempts?concept_id=&limit=&skip=` | one row per answered question |
| `GET /api/users/{user_key}/pathway/{question_id}` | **the full chain** for one answer |
| `GET /api/users/{user_key}/threshold-decisions?concept_id=` | Threshold-RL log (TRACE-02) |
| `GET /api/users/{user_key}/rl-decisions?concept_id=` | Hybrid-RL log |
| `GET /api/users/{user_key}/timeline?concept_id=` | all three logs merged chronologically |
| `GET /api/users/{user_key}/kg?concept_id=` | live Neo4j MASTERS edges + Process-KG rules |

### Example

```bash
# who has data?
curl -s localhost:8001/api/users | python3 -m json.tool

# everything about one learner
curl -s localhost:8001/api/users/aaai@gmail.com | python3 -m json.tool

# their attempts, then the full pathway for one of them
curl -s "localhost:8001/api/users/aaai@gmail.com/attempts?limit=5" | python3 -m json.tool
curl -s localhost:8001/api/users/aaai@gmail.com/pathway/<question_id> | python3 -m json.tool
```

## The pathway response

`/pathway/{question_id}` reconstructs the five layers the README's TRACE-01
claims are persisted, for a single interaction:

1. **`mastery`** — `before` → `after`, the `predicted` value, and confidence
2. **`thresholds`** — the personalised `tau_struggling` / `tau_mastered` in force,
   plus `decision_ref` and the linked Threshold-RL decision record
3. **`cognitive_state`** — `current`, `previous`, and whether it `changed`
   (the gate that decides if a recommendation may fire)
4. **`intervention`** — the Process-KG `rule` and the `action` it produced
5. **`recommendations`** — content the action generated, if any

plus `rl` (difficulty provenance: reward breakdown, Q-values, p_stat/p_rl blend),
`rl_decision_record`, and `raw_trace` for the unabridged stored trace.

### decision_ref and older records

`controller.py` writes a deterministic
`decision_ref = threshold:{user_id}:{concept_id}:{tau_timestep}` on every
threshold decision. Records written **before** that change have no `decision_ref`
field — for those, this service reconstructs the ref from
`trace.threshold_update.threshold_decision_ref` (which was a
`{user_id, concept_id, tau_timestep}` dict) and resolves the decision by that
triple instead.

`thresholds.decision_ref_resolution` in the response tells you which path was
taken:

* `decision_ref` — matched the stored ref directly (new records)
* `reconstructed:user+concept+tau_timestep` — legacy fallback
* `unresolved` — no threshold decision links to this interaction. This is
  normal: the Threshold RL only fires once every 5 interactions per concept.

## Notes on what you'll see

* **Mongo holds the history; Neo4j holds only current state.** If `NEO4J_URI`
  changes to a fresh instance, the *Live KG* tab goes empty while every
  Mongo-backed trace stays intact. The `kg` endpoint reports this explicitly via
  its `note` and `graph_totals` fields rather than silently showing nothing.
* **A learner with 0 attempts has nothing to trace.** Onboarding creates the
  user (and a `Learner` node) but no decisions — take at least one quiz as that
  learner first.
* `adaptive_decisions` documents carry no top-level `concept_id`; this service
  derives it from `learner_state.concept_id` or the `session_id`
  (`{user_id}:{concept_id}`) so per-concept filtering works anyway.
