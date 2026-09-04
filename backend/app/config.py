"""Centralized configuration loaded from environment variables."""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_THIS_DIR = Path(__file__).resolve().parent
_ENV_CANDIDATES = [
    _THIS_DIR.parent / ".env",          # backend/.env
    _THIS_DIR.parent.parent / ".env",   # project-root/.env
]
_ENV_FILE = next((p for p in _ENV_CANDIDATES if p.exists()), ".env")


class Settings(BaseSettings):
    # MongoDB (operational store — users, quiz attempts, notes, etc.)
    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "palms"
    # PALMS course/transcript data lives in the same cluster; override if separate
    PALMS_MONGODB_DB: str = "palms"

    # Neo4j
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "password"

    # Ollama
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.2:3b"
    OLLAMA_EMBED_MODEL: str = "nomic-embed-text"    # for the recommendation engine

    # Auth
    JWT_SECRET: str = "dev-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

    # CORS
    FRONTEND_ORIGIN: str = "http://localhost:5173"

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")


settings = Settings()