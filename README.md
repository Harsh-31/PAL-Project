# PAL (IAIRO) — Developer README

Personal Adaptive Learning (PAL) — an MVP that composes adaptive, cross-course playlists from open educational resources and personalises learning using a hybrid knowledge-graph + LLM approach.

This README focuses on developer setup, project layout, running the app locally (backend + frontend), Docker, and common troubleshooting notes.

---

**Quick start (dev)**

1. Backend (Python 3.11 recommended):

```bash
cd backend
PYENV_VERSION=3.11.14 python -m pip install -r requirements.txt
PYENV_VERSION=3.11.14 python -m uvicorn app.main:app --reload --port 7000
```

2. Frontend (Vite):

```bash
cd frontend
npm install
npm run dev
```

Open the app at http://localhost:5173. The frontend proxies `/api` to the backend; ensure the proxy target in `frontend/vite.config.js` matches the backend port (default dev backend: 7000).

---

**Repository layout**

- `backend/` — FastAPI application
  - `app/main.py` — FastAPI entrypoint; lifespan hooks connect Mongo + Neo4j and seed T-Box data
  - `app/config.py` — settings loaded from `.env`
  - `app/database/` — `mongo.py` (Motor client + indexes) and `neo4j_db.py` (Process KG state/rule seed)
  - `app/data/` — `courses.json`, `tracks.json` (T-Box seed files)
  - `app/models/` — Pydantic request/response schemas
  - `app/routes/` — `auth`, `courses` (onboarding + playlist), `quiz` (generate/submit/decisions, immediate feedback), `sidebar` (notes/chat/code-run), `dashboard`
  - `app/services/` — see below
    - `pal_agent.py` — `AdaptiveLearningOrchestrator`: thin coordinator composing Hybrid RL + Process KG + Recommendation Engine (see "Adaptive Learning Architecture" below)
    - `kg_service.py` — Process KG: mastery updates, intervention-rule lookup, state-transition detection
    - `recommender.py` — semantic goal matching, onboarding playlist construction, struggling/challenge content recommendation, remedial-recommendation lifecycle
    - `adaptive/` — Hybrid RL package (statistical prior, Q-learning, hybrid policy — see below)
    - `ollama_service.py` — LLM calls (question generation, hobby-analogy explanations); `sync_service.py` — KG/Mongo sync helpers
  - `tests/` — pytest suite (108 tests as of this writing — RL, Process KG, orchestrator, state-transition gating, remediation lifecycle, immediate feedback, onboarding playlist)
  - `requirements.txt`, `.env`, `Dockerfile`

- `frontend/` — React + Vite
  - `src/api/client.js` — fetch wrapper that sets `Authorization: Bearer <token>`
  - `src/contexts/AuthContext.jsx` — `login` / `signup` helpers used by pages
  - `src/pages/` — `Login`, `Signup`, `Onboarding`, `Dashboard`, `Learn`, `Explorer`
  - `vite.config.js` — dev proxy (update `target` to your backend port)

- `docker-compose.yml` — convenience compose file (optional)

---

Prerequisites

- `pyenv` (optional) to manage Python versions — recommended Python: `3.11.14` (some wheels are not available for newer 3.14+ at time of writing)
- Node 18+ (for frontend)
- Ollama (for local LLM inference) — https://ollama.com
- MongoDB Atlas connection (or local Mongo)
- Neo4j Aura / server (or a local Neo4j instance)

Environment variables (`backend/.env` — minimal)

```
MONGODB_URI=mongodb+srv://<user>:<pass>@cluster0.xxxxx.mongodb.net/?appName=Cluster0
MONGODB_DB=palms
NEO4J_URI=bolt://<host>:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<password>
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
JWT_SECRET=change-me-to-a-long-random-string-for-production
FRONTEND_ORIGIN=http://localhost:5173
```

Do not commit real secrets.

---

Running locally — backend notes

- Use a compatible interpreter when installing dependencies. Example with `pyenv`:

```bash
PYENV_VERSION=3.11.14 python -m pip install -r backend/requirements.txt
PYENV_VERSION=3.11.14 python -m uvicorn app.main:app --reload --port 7000
```

- Health-check: `GET /api/health` (returns OLLAMA_MODEL and status)
- Interactive docs: `http://localhost:7000/docs`

Common backend troubleshooting

- `ModuleNotFoundError: No module named 'motor'` — install `motor` in the active env:
  `python -m pip install motor` or run the `requirements.txt` installation shown above.
- `ERROR: Failed building wheel for pydantic-core` or other rust/maturin build errors:
  - Use Python 3.11.x (pre-built wheels are available); avoid Python 3.14 for installing these deps.
  - If you must use newer Python, install Rust and ensure `maturin` can build native wheels.
- `Address already in use` on port 8000/7000:
  ```bash
  lsof -i :8000
  kill <pid>
  # if needed
  kill -9 <pid>
  ```

Running locally — frontend notes

- The dev server runs on `http://localhost:5173` by default. Vite proxies `/api` to the backend as configured in `vite.config.js`.
- If you change the backend port (e.g., use 7000), update `frontend/vite.config.js` proxy `target` to `http://127.0.0.1:7000`.

Docker / docker-compose

- There's a `docker-compose.yml` at the repo root to run backend + frontend + optional services. Use it when you want a containerised workflow:

```bash
docker-compose up --build
```

API (selected endpoints)

- POST `/api/auth/signup` — body `{name,email,password}` → returns `access_token` and `user`
- POST `/api/auth/login` — body `{email,password}` → returns `access_token` and `user`
- GET `/api/me` — requires `Authorization: Bearer <token>`
- POST `/api/quiz/generate` — generate MCQ for a chunk via Ollama; difficulty is chosen by the Hybrid RL `AdaptiveDifficultyController` (Process KG and the recommender are not consulted for this decision)
- POST `/api/quiz/submit` — submit an answer. Always returns the fixed `explanation`; for wrong answers only, also generates and returns a hobby-flavoured `analogy` (immediate feedback, independent of Process KG/RL/video state). Runs the `AdaptiveLearningOrchestrator` and additionally returns `mastery`, `intervention` (current Process KG state/rule/action), `recommendations` (only populated on fresh entry into a state that needs content), `retired_lecture_ids` (populated when the concept just reached Mastered), `next_difficulty`, and `reward`
- GET `/api/quiz/decisions/{concept_id}` — explainability trace: every RL decision (state, p_stat, p_rl, blend weight, reward, Q-values) for this learner+concept
- POST `/api/courses/onboarding` — builds the learner's starter playlist from their stated goal via semantic concept matching (see "Onboarding playlist" below)
- GET `/api/courses/playlist` — current playlist (core + supplementary/challenge lectures + mastery map)

Refer to `backend/app/routes` for the complete list of endpoints.

Troubleshooting: frontend shows HTTP 500 / auth failing

- Reproduce the API call with `curl` to isolate frontend vs backend. Example:

```bash
curl -i -X POST http://127.0.0.1:7000/api/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"name":"Test","email":"t@example.com","password":"password123"}'
```

- If the curl call succeeds but the browser shows 500, confirm the Vite proxy target port and any CORS configuration (`FRONTEND_ORIGIN` in `.env`).

Notes & design pointers

- The T-Box (courses, concepts) is seeded from JSON on startup and is idempotent. Learner progress lives in MongoDB (A-Box).
- Every quiz answer is coordinated by the `AdaptiveLearningOrchestrator` (`pal_agent.py`), which composes three independent subsystems — Hybrid RL (difficulty), the Process KG (pedagogical intervention), and the Recommendation Engine (content) — without any one overriding another. The LLM is used only to author question text, explanations, and analogies — it never makes an adaptation decision. See "Adaptive Learning Architecture" below.

---

## Adaptive Learning Architecture

PAL's per-answer adaptation is three independent subsystems composed by one thin, deterministic coordinator — no subsystem overrides another's decision:

| Subsystem | Owns | Implementation |
|---|---|---|
| Hybrid RL | "How hard should the next question be?" (Easy/Medium/Hard) — sole authority | [`backend/app/services/adaptive/`](backend/app/services/adaptive/) |
| Threshold RL | "Where should this learner's own Struggling/Mastered mastery cutoffs sit?" — learns per-learner `tau_struggling`/`tau_mastered`, consumed by the Process KG's intervention lookup | [`backend/app/services/threshold_rl/`](backend/app/services/threshold_rl/) |
| Process KG | "What pedagogical intervention fits current mastery?" (Continue/Remediate/Analogy/Challenge/Skip) | [`backend/app/services/kg_service.py`](backend/app/services/kg_service.py) |
| Recommendation Engine | "Which content resource(s) satisfy that intervention, if any?" | [`backend/app/services/recommender.py`](backend/app/services/recommender.py) |
| Orchestrator | Sequences the three above and composes one response | [`backend/app/services/pal_agent.py`](backend/app/services/pal_agent.py) |

```
Learner answers a question
        |
        v
+-------------------------+       +---------------------------+
| Immediate Feedback      |       | Mastery Updater            |
| (routes/quiz.py — NOT   |       | record_mastery(+0.12/-0.08)|
| the orchestrator)       |       +-------------+---------------+
| - explanation (fixed)   |                     |
| - analogy, wrong only   |                     v
+-------------------------+       +---------------------------+
                                  | AdaptiveLearningOrchestrator|
                                  | - difficulty  <- Hybrid RL  |
                                  | - state       <- Process KG |
                                  | - detects state ENTRY       |
                                  | - if entry needs content ->|
                                  |   Recommendation Engine     |
                                  | - if Mastered -> retire     |
                                  |   pending remediation       |
                                  +----+-----------------+-----+
                                       |                 |
                                       v                 v
                              Hybrid RL (Q-learning   Process KG
                              + IRT prior) picks      (Struggling/OnTrack/
                              next difficulty         Mastered — per-learner
                                                       tau_struggling/tau_mastered,
                                                       see Threshold RL below)
```

`record_mastery`'s `+0.12`/`-0.08` step size is a hand-set, deliberately asymmetric constant (correct answers move mastery up ~1.5x faster than incorrect answers move it down) — not learned or paper-derived; it predates both RL subsystems and is the KG's own mastery-update rule.

### Immediate answer feedback (independent of the orchestrator)

`explanation` (fixed at question-creation time) and, for wrong answers only, a hobby-flavoured `analogy` are generated directly in `routes/quiz.py`'s `/submit` handler — never inside the orchestrator, and never gated on Process KG state or video playback. Every incorrect answer gets an analogy attempt regardless of which cognitive state it happens to land in; an LLM failure here returns `None` rather than blocking submission.

### Process KG — pedagogical states

3-state, threshold-driven on mastery score `[0,1]` (`kg_service.get_intervention`) — the KG's `CognitiveState`/`InterventionRule` nodes hold the rule/action mapping (seeded once in `neo4j_db._seed_process_kg`), but the classification thresholds themselves are **not** fixed constants: they're the per-learner `tau_struggling`/`tau_mastered` values learned by the Threshold RL and stored on that learner's `MASTERS` edge (defaulting to `0.40`/`0.85` until the Threshold RL has run at least once for them):

```
mastery < tau_struggling        -> Struggling
tau_struggling <= mastery < tau_mastered -> OnTrack
mastery >= tau_mastered         -> Mastered
```

| State | Rule(s) (priority order) | Action(s) | Triggers Recommendation Engine? |
|---|---|---|---|
| Struggling | OfferSimplerAnalogy, AddRemedialContent | `simplify_with_hobby_analogy`, `insert_prerequisite_video` | Yes, on fresh entry only |
| OnTrack | OnTrackContinue | `continue_normal` | No |
| Mastered | MasteredChallenge | `offer_challenge_content` | No — instead retires any still-active remediation for the concept |

An earlier 5-state design (`Frustrated`/`Confident` as separate states, with a `SkipRedundant`/`skip_next_similar_chunk` action on Mastered) has been retired — `_seed_process_kg` actively deletes those legacy `CognitiveState`/`InterventionRule` nodes on startup if found. `Mastered` now means "push harder content," not "skip," and `Struggling` covers what used to be split across `Frustrated`+`Struggling`.

**State-transition gating**: a recommendation only fires when the learner's cognitive state just *changed* into one that needs content (e.g. OnTrack → Struggling), never on every answer spent remaining in that state (Struggling → Struggling). The previous state is persisted on the same Neo4j `MASTERS` edge `record_mastery` already writes (`kg_service.swap_intervention_state`), so this survives separate HTTP requests.

**Remediation lifecycle**: recommendations handed out on a Struggling entry are tracked in MongoDB (`remedial_recommendations`, `status: "active"`). When the same concept later reaches Mastered, those rows are marked `"retired"` (`recommender.retire_recommendations_for_concept`) — idempotent, scoped to that one learner+concept, and never touches unrelated content.

## Hybrid Reinforcement Learning Adaptation

PAL's difficulty selection implements the Hybrid Reinforcement Learning algorithm from the AAAI-26 PAL paper: a 2PL IRT-style statistical prior blended with a tabular Q-learning policy, composed alongside (not replacing) the existing deterministic Process KG. Implementation lives in [`backend/app/services/adaptive/`](backend/app/services/adaptive/); orchestration lives in [`backend/app/services/pal_agent.py`](backend/app/services/pal_agent.py).

### Architecture

```
Learner interaction
        |
        v
  Learner State x_t
        |
   +----+----------------------+
   |                           |
   v                           v
Statistical Prior          RL Policy (Q-learning)
p_stat(d|x_t)               p_RL(d|x_t)
   |                           |
   +----------+----------------+
              v
        Hybrid Policy
   pi_t(d|x_t) = (1-w_t)p_stat + w_t*p_RL
              |
              v
      Difficulty Decision (Easy / Medium / Hard)
              |
        +-----+------------------------+
        |                              |
        v                              v
  Question generation (LLM)     Process KG intervention
  (difficulty-scaled prompt)    (remedial / analogy / skip)
        |                              |
        v                              |
   Learner Response  <------------------
        |
        v
  Reward r_t + State Update x_{t+1}
        |
        v
  Q-learning update: Q(s,a) <- Q(s,a) + alpha[r_t + gamma*max_a'Q(s',a') - Q(s,a)]
```

The RL controller owns **difficulty selection only**. The Process KG still owns **pedagogical interventions** (`OfferSimplerAnalogy`, `AddRemedialContent`, `OnTrackContinue`, `MasteredChallenge`) based on mastery, unchanged from before. The two are independent, composed by `AdaptiveLearningOrchestrator.process_attempt` (see "Adaptive Learning Architecture" above).

### Learner state x_t

A 6-dimensional vector, recomputed from real interaction data after every answered question (`app/services/adaptive/state.py`):

```
x_t = [skill, recent_accuracy, normalized_response_time,
       streak_momentum, learning_velocity, confidence]
```

| Component | Definition |
|---|---|
| `skill` | Online 2PL-IRT ability estimate (theta), updated Elo-style: `theta_{t+1} = theta_t + lr * (correct - sigma(a_d(theta_t - b_d)))`, using the same item parameters as the statistical prior. |
| `recent_accuracy` | Mean correctness over the last `recent_window` (default 5) answers. |
| `normalized_response_time` | z-score of the current response time against the learner's own response-time history, squashed with `sigma(-z)` into [0,1] (1 = fast, 0 = slow, relative to that learner). |
| `streak_momentum` | Signed current correct/incorrect streak length, squashed with `tanh(streak/scale)` into [-1,1]. |
| `learning_velocity` | Change in `recent_accuracy` between the current and previous rolling window — a real improvement/decline signal. |
| `confidence` | `0.5*evidence + 0.3*consistency + 0.2*time_consistency` (`state.py::update`, step 6) — (a) evidence volume (`timestep / horizon`), (b) consistency of recent correctness (1 − normalized variance), (c) consistency of recent response times (1 − coefficient of variation). No randomness. The 0.5/0.3/0.2 split is a hand-set priority ordering — "have we seen enough attempts at all" outweighs "were those attempts consistent" — not a fitted or paper-specified value. |

State persists in MongoDB per `(user_id, concept_id)` (`learner_states` collection) and survives across sessions.

### Action space

Exactly three actions, `a_t ∈ {EASY, MEDIUM, HARD}` (`app/services/adaptive/actions.py`). Mapped to the pre-existing 1-5 difficulty ladder (EASY→2, MEDIUM→3, HARD→4) only at the API/LLM-prompt boundary, so the frontend and Ollama prompts needed zero changes.

### Statistical prior (2PL IRT)

```
p_stat(d | x_t) ∝ sigma(a_d * (theta_t - b_d))
```

`theta_t` is the learner's current skill (in logit space); `a_d`/`b_d` are configurable discrimination/difficulty parameters per action. Raw sigmoid scores are normalized to sum to 1 — a real probability distribution, not a threshold rule.

On top of this, asymmetric stability thresholds nudge (never hard-select) the distribution:

- `recent_accuracy >= 0.75` → promote: shift probability mass toward the next-harder level
- `recent_accuracy <= 0.35` → demote: shift probability mass toward the next-easier level
- A `cooldown_steps`-step hold after any level change biases the distribution back toward the *current* level, preventing oscillation

### Q-learning

Tabular Q-learning over a discretized state (`app/services/adaptive/q_learning.py`):

```
Q_{t+1}(a_t) = Q_t(a_t) + alpha [ R_t + gamma * max_a' Q_t(a') - Q_t(a_t) ]
```

- `alpha` (learning rate), `gamma` (discount), `epsilon` (exploration), `epsilon_decay`, `epsilon_min` — all configurable (`config.py`)
- `p_RL(d|x_t)` is the epsilon-greedy policy expressed as a probability distribution (argmax gets `1-epsilon`, the rest share `epsilon`) — a real distribution, used directly in the hybrid blend
- The Q-table is **shared across learners** (keyed by discretized state, not by user) since the discretized state already encodes the learner's own skill/accuracy/etc — this lets the policy learn faster than a per-learner table would with MVP traffic
- Seeded RNG (`config.q.seed`) for reproducible action sampling/exploration in tests and simulation

### Discretization

The continuous 6-d state is split into 3 buckets per dimension — `LOW`/`MID`/`HIGH` — via configurable boundaries (`app/services/adaptive/discretization.py`), giving a Q-table with at most 3^6 = 729 states x 3 actions, and a state key any student can read directly, e.g. `sk2-ac1-rt0-st1-vl1-cf2`.

| Dimension | LOW | MID | HIGH |
|---|---|---|---|
| skill | < 0.40 | 0.40–0.60 | > 0.60 |
| recent_accuracy | < 0.40 | 0.40–0.70 | > 0.70 |
| normalized_response_time | < 0.35 | 0.35–0.65 | > 0.65 |
| streak_momentum | < −0.30 | −0.30–0.30 | > 0.30 |
| learning_velocity | < −0.05 | −0.05–0.05 | > 0.05 |
| confidence | < 0.40 | 0.40–0.70 | > 0.70 |

### Reward

```
r_t = r_acc + r_time + r_prog + r_mom
```

| Component | Range | Definition |
|---|---|---|
| `r_acc` | {+1, −0.5} | +1 correct, −0.5 incorrect |
| `r_time` | [0, 0.3] | `time_reward_max * speed_score`, where `speed_score` is the same normalized-response-time signal from `x_t` |
| `r_prog` | [0, 0.2] | `progress_reward_max * (difficulty_rank / 2)` on correct answers only — rewards succeeding at harder difficulty |
| `r_mom` | [0, 0.1] | `momentum_reward_max * max(0, streak_momentum)` — rewards positive streaks, never penalizes negative ones |

Every component is independently testable (`app/services/adaptive/reward.py`) and logged per-decision.

### Hybrid policy

```
pi_t(d | x_t) = (1 - w_t) * p_stat(d | x_t) + w_t * p_RL(d | x_t)
w_t = min(w_max, w0 + kappa * confidence_t * progress_t)
```

Defaults: `w_max = 0.8`, `w0 = 0.15`, `kappa = 0.7`. Early in an adaptation stream (low confidence, low progress), `w_t ≈ w0` and the statistical prior dominates. As evidence accumulates, `w_t → w_max` and the learned Q-policy gets more say. The final action is *sampled* from `pi_t` (not argmax), so the hybrid policy's own exploration behavior carries through end to end.

### Configuration

All parameters live in `app/services/adaptive/config.py` as typed dataclasses (`RewardConfig`, `IRTConfig`, `QLearningConfig`, `HybridConfig`, `BucketConfig`, `StateConfig`) — no magic numbers scattered through the algorithm code.

### Explainability

Every decision is logged to the `adaptive_decisions` MongoDB collection with the full trace: learner state, discretized state, `p_stat`, `p_rl`, blend weight, hybrid policy, selected action, reward + components, and Q-value before/after. `GET /api/quiz/decisions/{concept_id}` returns this trace so "why did PAL choose HARD here?" is always answerable.

### A note on the numeric constants

Every weight, threshold, and bound above lives in a typed dataclass in `config.py` — none are derived in this repo from calibration, grid search, or fitting against usage data (there's no such script; `simulate_hybrid_rl.py` runs demo profiles for illustration, it doesn't tune anything). Two patterns account for essentially all of them:

- **Priority ordering by magnitude**: where a reward or blend has multiple components, the *primary* signal is always sized to dominate the *secondary/shaping* ones — e.g. `r_acc`'s `{+1, -0.5}` can't be outweighed by `r_time + r_prog + r_mom`'s `[0, 0.6]` ceiling; the confidence blend's evidence term (`0.5`) outweighs consistency (`0.3`) and timing (`0.2`). The exact numbers within that ordering are hand-picked, not derived.
- **Continuity with a pre-existing rule**: where the RL layer replaces or augments a value that was previously a hardcoded rule, its default is set to match that rule so the learned system starts exactly where the deterministic one left off — e.g. Threshold RL's `tau_struggling_default = 0.40` / `tau_mastered_default = 0.85` reproduce the Process KG's original static Frustrated/Struggling and Confident/Mastered cutoffs.

Where a docstring attributes a value to "the paper" (AAAI-26 PAL), that attribution is the code's own claim — this repo does not contain the paper itself, so those specific numbers (e.g. IRT stability thresholds `0.75`/`0.35`, hybrid weights `w_max=0.8`/`w0=0.15`/`kappa=0.7`) can't be independently checked against it here. The `### Deviations from the paper` section below lists everything the paper leaves unspecified, where the value shown is instead this codebase's own MVP choice.

### Threshold RL — learnable cognitive-state boundaries

Alongside the Hybrid difficulty RL, a second, independent RL system learns **per-learner** the mastery cutoffs the Process KG uses to decide Struggling vs. Mastered — instead of the fixed `0.40`/`0.85` constants every learner shared before. Implementation: [`backend/app/services/threshold_rl/`](backend/app/services/threshold_rl/); wired into the orchestrator at `pal_agent.py`'s `_threshold_rl.record_interaction(...)` call, which runs every answered question, *before* the Process KG's intervention lookup so thresholds are current when a state is decided.

**Cadence**: interactions are buffered per `(user_id, concept_id)`; a Q-update + threshold adjustment only runs every `update_every_n = 5` interactions (`ThresholdUpdateConfig`), not per-answer like the difficulty RL — this is a delayed-reward design, since "did moving this threshold help?" only becomes visible after several interactions, and updating on every single answer would oscillate the boundary.

**Action space**: two independent 3-action Q-policies (`DECREASE` / `KEEP` / `INCREASE`), one for `tau_struggling`, one for `tau_mastered`, sharing the same discretized 6-d state key as the difficulty RL (mirrored onto the Neo4j `MASTERS` edge by the difficulty RL, then read back here — see `controller.py::discretize_from_rl_state`). Each `INCREASE`/`DECREASE` moves its threshold by a fixed `step_delta = 0.03` (`ThresholdQLearningConfig`) — small relative to the ~0.4-wide operating bands below, so a boundary can only creep, not jump, per 5-interaction cycle.

**Bounds** (`ThresholdBoundsConfig`, enforced in `controller.py::_clamp_thresholds`):

| Bound | Min | Max | Default |
|---|---|---|---|
| `tau_struggling` | 0.20 | 0.60 | 0.40 |
| `tau_mastered` | 0.60 | 0.95 | 0.85 |

Plus a hard `min_gap = 0.15`: if a move would let `tau_mastered - tau_struggling` collapse below that gap, both are re-centered outward to preserve it. The defaults (`0.40`, `0.85`) intentionally reproduce the Process KG's original static Frustrated/Struggling and Confident/Mastered cutoffs, so the learned system starts from the same operating point as the hand-designed rule table and only *nudges* away from it within a bounded, pedagogically plausible range — the min/max bounds exist to stop the learned value from drifting somewhere nonsensical (e.g. "mastered" below 0.6) rather than to reflect any measured optimum.

**Reward** (`ThresholdRewardConfig` / `reward.py`), computed once per 5-interaction window, over the *whole* window rather than one answer:

```
R_t = w1·ΔMastery + w2·ΔAccuracy − w3·InterventionCost − w4·UnnecessaryRemediation
```

| Weight | Value | Term it scales |
|---|---|---|
| `w_mastery_delta` | 0.4 | `mastery_after - mastery_before` over the window — the actual target outcome |
| `w_accuracy_delta` | 0.3 | `accuracy_after - accuracy_before` — a correlated, faster-moving leading indicator of the same thing |
| `w_intervention_cost` | 0.2 | `interventions_triggered / window_size` — a regularizer penalizing thresholds that fire remediation too often |
| `w_unnecessary_remediation` | 0.1 | `interventions_without_improvement / max(interventions_triggered, 1)` — penalizes remediation that didn't help, weighted lowest since it's a ratio over an already-small count |

As with the difficulty-RL reward, the ordering (mastery > accuracy > cost penalties) is a deliberate hand-set priority — mastery gain is the real objective, accuracy is a proxy for it, and the two penalty terms exist to discourage the policy from spamming interventions to farm mastery gain, not to compete with it in magnitude. There's no paper citation for this module — the Threshold RL is a repo-native extension beyond what the AAAI-26 paper covers (see Deviations below).

**Q-learning hyperparameters**: identical values to the difficulty RL's `QLearningConfig` (`alpha=0.15`, `gamma=0.90`, `epsilon=0.25`, `epsilon_decay=0.97`, `epsilon_min=0.05`) — `gamma=0.9` weights the delayed, windowed reward heavily; `alpha=0.15` is a conservative update step to avoid table thrashing across two independent Q-tables (struggling/mastered); `epsilon` decays from a 25% exploration rate to a 5% floor so exploration never fully stops. Seeded (`seed=137`, offset by the learner's `tau_timestep` per update) for reproducibility.

**Persistence & explainability**: both Q-tables and `epsilon` are stored in MongoDB (`threshold_q_values`, `threshold_meta`), shared globally across learners for the same fast-convergence reason as the difficulty RL's Q-table. Every update is logged to `threshold_decisions` with the full before/after trace (thresholds, actions, reward components, Q-values).

### Onboarding playlist

`recommender.build_onboarding_starter_playlist` turns a learner's free-text goal into a starter playlist via semantic (embedding cosine-similarity) concept matching:

- `top_k=20` concepts considered, `similarity_floor=0.65` — a concept only qualifies if it clears this bar
- Fallback: if fewer than `fallback_min_concepts=3` concepts clear the floor, the top 3 by raw similarity are used instead, so legitimately broad goals still get a playlist
- `MAX_ONBOARDING_LECTURES=10` cap, relevance-ranked, deduplicated, prerequisite-aware ordering — so a broad goal produces a focused playlist, not an entire course

### Testing & simulation

- `backend/tests/test_adaptive_*.py` — unit tests for state, statistical prior, Q-learning, reward, hybrid policy, discretization, and a pure-logic end-to-end simulation (80 steps)
- `backend/tests/test_orchestrator.py`, `test_state_transition.py` — `AdaptiveLearningOrchestrator` composition, state-entry gating, RL/KG independence
- `backend/tests/test_remediation_lifecycle.py` — active/retired remedial-recommendation lifecycle
- `backend/tests/test_immediate_feedback.py` — wrong-answer analogy generation, independent of Process KG/RL/correctness by construction
- `backend/tests/test_onboarding_playlist.py`, `test_recommender_challenge.py` — goal-matching, fallback, capping, and challenge-content recommendation
- `backend/simulate_hybrid_rl.py` — reproducible demo across 4 learner profiles (struggling / average / fast-learning / inconsistent), 100 questions each; writes `backend/simulation_output/*.json` and `summary.csv` for plotting

Run with:

```bash
cd backend
python -m pytest -q   # 108 tests as of this writing
python simulate_hybrid_rl.py
```

### Deviations from the paper (and why)

- **Session/state scoping**: the current schema has no explicit "session" entity, so the learner-state stream is keyed by `(user_id, concept_id)` rather than a session id — this matches how quizzes are already organized (per concept/chunk) and persists naturally across sessions.
- **`progress_t`**: the paper does not pin down its exact functional form; implemented as `min(timestep / progress_horizon, 1.0)`, a monotonic proxy for "evidence accumulated in this adaptation stream."
- **`p_RL(d|x_t)`**: represented as the epsilon-greedy policy's probability simplex (argmax gets `1-epsilon`) rather than a softmax-over-Q-values, to avoid introducing an unspecified temperature hyperparameter and to keep exploration and the RL distribution the same mechanism.
- **Q-table scope**: shared across learners (keyed by discretized state only) rather than per-learner, for faster convergence at MVP traffic volumes — the discretized state already carries learner-specific signal.
- **Threshold RL**: the per-learner `tau_struggling`/`tau_mastered` boundary-learning system (`app/services/threshold_rl/`, see above) is not part of the AAAI-26 PAL paper's described algorithm — it's a repo-native extension applying the same tabular Q-learning pattern to a second decision (where the Process KG's mastery cutoffs should sit) rather than a paper-specified requirement.

Contributing

- Fork, create a feature branch, run tests (when present), and open a PR describing the change.
