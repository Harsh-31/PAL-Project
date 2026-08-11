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
  - `app/database/` — `mongo.py` (Motor client + indexes) and `neo4j_db.py`
  - `app/data/` — `courses.json`, `tracks.json` (T-Box seed files)
  - `app/models/` — Pydantic request/response schemas
  - `app/routes/` — `auth`, `courses`, `quiz`, `sidebar`, `dashboard`
  - `app/services/` — integration with Ollama, recommender, PAL agent, KG seeding
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
- POST `/api/quiz/generate` — generate MCQ for a chunk via Ollama
- POST `/api/quiz/submit` — submit an answer; runs PAL-Agent and returns intervention

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
- The PAL-Agent is deterministic in policy selection (Process KG) and uses the LLM only to author questions and summaries.

Contributing

- Fork, create a feature branch, run tests (when present), and open a PR describing the change.

---

If you'd like, I can:

- set the project Python version via a `.python-version` file to `3.11.14` (pyenv)
- update `frontend/vite.config.js` to proxy to `http://127.0.0.1:7000` (dev default)
- add a small `scripts/dev.sh` wrapper that launches frontend + backend with the right env selectors

Tell me which of the above you'd like me to apply next.
