# PALMS — Personal Adaptive Learning Management System (MVP)

> _"Learn in your own way with PAL."_

PALMS is a neuro-symbolic adaptive learning platform. Your AI copilot **PAL** builds a custom
video playlist from open-source courses (MIT OCW, NPTEL), pauses videos to ask **adaptive
MCQs generated in real-time by a local Ollama model**, and adjusts difficulty, playlist
order, and analogies based on your mastery — all driven by a **three-layer Knowledge Graph**
in Neo4j (T-Box / A-Box / Process KG).

This is the **single-user MVP** — no RabbitMQ, no Celery. It runs end-to-end on your machine
against free-tier MongoDB Atlas and Neo4j Sandbox.

---

## What's built (mapped to the architecture diagram)

| Diagram block | Where in code |
|---|---|
| **Ingestion Pipeline (Stages 1 / 1.5 / 2)** | `backend/app/data/courses.json` (seed) → `kg_service.seed_courses_if_empty` writes T-Box to Neo4j |
| **MongoDB (Operational Store)** — Courses, Questions, User History, Interactions | `backend/app/database/mongo.py` |
| **Neo4j (KG Storage)** — Course KG (T-Box), User KG (A-Box), Process KG | `backend/app/database/neo4j_db.py` + `services/kg_service.py` |
| **FastAPI Backend (Microservices layer)** — User, Playlist, Video, Session, Analytics, KG, Sidebar | `backend/app/routes/*` |
| **PAL-Agent Decision Engine** — Observe → Beliefs → Predict → Assess → Counterfactual → Select Action → Reflect → Memory | `backend/app/services/pal_agent.py` |
| **Reasoning Runtime — Model Router / Inference** | `backend/app/services/ollama_service.py` |
| **React Frontend** — Personalised Playlist, Video Player, Quiz, Notes, Dashboard, KG Explorer | `frontend/src/pages` + `frontend/src/components` |
| **Auth & Access — API Gateway, JWT** | `backend/app/routes/auth.py` + `utils/security.py` + `utils/deps.py` |
| **Micro-loop (per learning step)** | Runs on every quiz submission — see `pal_agent.decide_after_attempt` |
| **Macro-loop (over a session)** | Playlist ordering + mastery snapshot on dashboard |

Deferred for post-MVP (as you requested): RabbitMQ / Celery workers, KG Synchronizer service, per-frame OCR (Tesseract), fine-tuning pipeline, RBAC, audit logs.

---

## The 4 seeded courses

1. **MIT 6.0001** — Introduction to CS & Programming in Python
2. **MIT 18.06** — Linear Algebra (Gilbert Strang)
3. **NPTEL — Programming, Data Structures & Algorithms in Python** (Prof. Madhavan Mukund)
4. **NPTEL — Introduction to Machine Learning** (Prof. Balaraman Ravindran)

Each course has real YouTube lecture IDs, concept nodes with prerequisite edges, and
lecture chunks with summaries — so the Ollama model has real context to generate MCQs from.

---

## Prerequisites

- **Python 3.11+**
- **Node.js 20+**
- **Ollama** installed locally — https://ollama.com
- A **MongoDB Atlas free-tier cluster** — connection string
- A **Neo4j Sandbox** or Aura Free instance — bolt URI + credentials

Pull a local model, e.g.:
```bash
ollama pull llama3.2:3b        # fast, small, good for MVP
# or
ollama pull mistral:7b-instruct
```

---

## Setup

### 1. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env with your Mongo/Neo4j creds and Ollama model name

uvicorn app.main:app --reload --port 8000
```

On first boot the backend will:
- Connect to Mongo, ensure indexes.
- Connect to Neo4j, create uniqueness constraints.
- Seed the **Process KG** rules (idempotent).
- Seed the **4 courses** (idempotent — safe to restart).

Verify: `curl http://localhost:8000/api/health`

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. The Vite dev server proxies `/api/*` to `http://localhost:8000`.

### 3. Ollama

Make sure Ollama is running on the default port (`11434`). The backend hits `/api/chat`
with `format=json` so MCQs come back as structured JSON — no fragile parsing.

---

## End-to-end user flow (MVP)

1. **Sign up** → password stored bcrypt-hashed in Mongo, JWT issued.
2. **Onboarding wizard** — pick a course, set baseline (beginner/intermediate/advanced), state a goal, add hobbies (e.g. _Marvel, cricket, cooking_). This upserts a `Learner` node in Neo4j and an `ENROLLED_IN` edge to the `Course`.
3. **Learn page**:
   - Left: playlist from Neo4j, ordered by concept difficulty.
   - Center: YouTube player (via `react-youtube`).
   - Right: tabbed sidebar — **Notes** (Mongo), **Chat** (Ollama Q&A with lecture context), **Code** (Python subprocess, 5s timeout).
   - Every 1s the frontend polls the player time. When you reach the end of a lecture chunk it pauses the video and pops the **QuizOverlay**.
4. **Quiz** — `POST /api/quiz/generate` calls Ollama with the chunk summary, concept name, current difficulty, and your hobbies. You answer. `POST /api/quiz/submit` runs the **PAL-Agent micro-loop**:
   - Update mastery in Neo4j (Learner-[:MASTERS]->Concept).
   - Predict trajectory, assess confidence, generate counterfactuals.
   - Consult the Process KG for the intervention rule (`Frustrated / Struggling / OnTrack / Confident / Mastered`).
   - Return the new mastery, next difficulty, and the intervention text.
5. **Post-chunk personalised summary** — after each quiz PAL delivers a summary with an analogy from one of your hobbies (`GET /api/quiz/summary/:chunk_id`).
6. **Dashboard** — accuracy, mastery by concept, recent activity with the interventions the Process KG fired.
7. **KG Explorer** — snapshot of your Learner node's edges and a table of the deterministic Process KG rules.

---

## API surface (selected)

```
POST   /api/auth/signup            {name, email, password}
POST   /api/auth/login             {email, password}
GET    /api/me
GET    /api/courses
POST   /api/onboarding             {course_id, baseline, goal, evaluation_frequency, hobbies[]}
GET    /api/playlist/:course_id
POST   /api/quiz/generate          {lecture_id, chunk_id}
POST   /api/quiz/submit            {question_id, selected_index, ...}
GET    /api/quiz/summary/:chunk_id
GET    /api/sidebar/notes/:video_id
POST   /api/sidebar/notes          {video_id, content, timestamp_sec}
DELETE /api/sidebar/notes/:id
POST   /api/sidebar/chat           {message, lecture_id?, chunk_id?}
GET    /api/sidebar/chat/history
POST   /api/sidebar/code/run       {language, code}
GET    /api/dashboard/summary
GET    /api/dashboard/kg
```

All routes except `/api/auth/*` and `/api/health` require `Authorization: Bearer <jwt>`.

---

## Scaling to multi-user later

Points already in place that make the extension smooth:
- All Mongo indexes are prefixed with `user_id`.
- All Neo4j reads/writes are scoped by `Learner {id: user_id}`.
- JWT-based auth means the same routes work for N users concurrently.

What you'd add:
- Move `ollama_service` calls onto a task queue (RabbitMQ + Celery or arq) so generation
  can be batched / de-duplicated.
- Add the `Sidebar Service` websocket for live PAL streaming.
- Turn `/api/sidebar/code/run` into a proper sandboxed executor.
- Add the async workers block from the architecture diagram (embedding jobs, reflection
  jobs, report generation).

---

## Project structure

```
palms-mvp/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI entry, lifespan hooks
│   │   ├── config.py               # pydantic-settings
│   │   ├── database/               # Mongo + Neo4j drivers
│   │   ├── models/schemas.py       # Pydantic API contracts
│   │   ├── routes/                 # auth, courses, quiz, sidebar, dashboard
│   │   ├── services/
│   │   │   ├── ollama_service.py   # LLM cognitive engine
│   │   │   ├── kg_service.py       # Neo4j read/write for KG
│   │   │   └── pal_agent.py        # PAL-Agent micro-loop
│   │   ├── utils/security.py       # bcrypt + JWT
│   │   ├── utils/deps.py           # current_user dependency
│   │   └── data/courses.json       # 4 seeded courses
│   ├── requirements.txt
│   ├── .env.example
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── main.jsx, App.jsx
│   │   ├── contexts/AuthContext.jsx
│   │   ├── api/client.js
│   │   ├── pages/                  # Login, Signup, Onboarding, Dashboard, Learn, Explorer
│   │   ├── components/             # Layout, QuizOverlay, NotesTab, ChatTab, CodeTab
│   │   └── styles/global.css
│   ├── vite.config.js
│   ├── package.json
│   └── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Troubleshooting

- **Ollama call is slow / times out** — try a smaller model (`llama3.2:3b` or `phi3:mini`). MCQ generation is set to `temperature: 0.3` for consistency.
- **Neo4j connection fails** — Sandbox instances expire after a few days. Check the URI includes `neo4j+s://` for a hosted DB.
- **`bcrypt`-related errors on Windows** — the requirements pin `bcrypt==4.0.1` for compatibility with passlib 1.7.4.
- **YouTube video won't play** — some MIT/NPTEL videos are region-restricted. Swap the `youtube_id` in `courses.json` for an alternative and restart (the seed is idempotent per-course; you may need to drop the Course node in Neo4j to re-seed).
