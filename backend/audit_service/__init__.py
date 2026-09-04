"""PAL Traceability Audit Service — a standalone, read-only inspector.

Deliberately NOT part of the main PALMS app (`app/`):
  * separate FastAPI app, separate port (default 8001)
  * its own Mongo/Neo4j connections — no shared lifespan, no sync, no seeding
  * read-only: it never writes to either store
  * serves its own minimal UI, so the learner-facing React frontend is untouched

It reuses only `app.config.settings` so credentials live in one place.
"""
