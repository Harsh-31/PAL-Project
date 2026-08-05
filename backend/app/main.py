"""PALMS backend — FastAPI entry point."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database.mongo import connect_to_mongo, close_mongo_connection
from app.database.neo4j_db import connect_to_neo4j, close_neo4j_connection
from app.services import kg_service, recommender
from app.routes import auth, courses, quiz, sidebar, dashboard


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_mongo()
    await connect_to_neo4j()
    await kg_service.seed_courses_if_empty()
    # Recommender: embed any concepts that don't yet have vectors.
    # No-op after the first run; safe to fail (recommender falls back to topic-match).
    report = await recommender.ensure_concept_embeddings()
    print(f"[Recommender] embeddings: {report}")
    yield
    await close_mongo_connection()
    await close_neo4j_connection()


app = FastAPI(title="PALMS API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(courses.router)
app.include_router(quiz.router)
app.include_router(sidebar.router)
app.include_router(dashboard.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "model": settings.OLLAMA_MODEL}