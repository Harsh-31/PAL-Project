"""Read-only connections for the audit service.

Separate clients from `app.database.*` on purpose: this service must be
startable with the main API down, and must never participate in the main
app's lifespan (which syncs courses and seeds the Process KG).
"""
from __future__ import annotations

import os
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase, AsyncDriver

from app.config import settings


class _Store:
    mongo: Optional[AsyncIOMotorClient] = None
    db: Any = None
    neo: Optional[AsyncDriver] = None
    neo_error: Optional[str] = None


store = _Store()

# Optional shared-secret guard. Unset (the default) => no auth, which is fine
# for a locally-bound admin tool. Set AUDIT_TOKEN to require it.
AUDIT_TOKEN: str | None = os.environ.get("AUDIT_TOKEN") or None


async def connect() -> None:
    store.mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    store.db = store.mongo[settings.MONGODB_DB]
    # Touch the connection so a bad URI fails loudly at startup, not per-request.
    await store.db.command("ping")
    print(f"[Audit] Mongo connected → db={settings.MONGODB_DB}")

    # Neo4j is optional: it only enriches the symbolic layer (live MASTERS edge
    # + Process-KG rules). The Mongo-backed traces work without it.
    try:
        store.neo = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        await store.neo.verify_connectivity()
        print(f"[Audit] Neo4j connected → {settings.NEO4J_URI}")
    except Exception as exc:  # noqa: BLE001 — degrade, don't crash the tool
        store.neo_error = f"{type(exc).__name__}: {exc}"
        if store.neo is not None:
            try:
                await store.neo.close()
            except Exception:  # noqa: BLE001
                pass
        store.neo = None
        print(f"[Audit] Neo4j unavailable (KG layer disabled): {store.neo_error}")


async def disconnect() -> None:
    if store.mongo:
        store.mongo.close()
    if store.neo:
        await store.neo.close()


def get_db():
    if store.db is None:
        raise RuntimeError("Audit service Mongo connection not initialised")
    return store.db


def get_neo() -> AsyncDriver | None:
    return store.neo
