"""Async MongoDB client (Motor) — operational store for users, interactions, notes."""
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings


class MongoDB:
    client: AsyncIOMotorClient | None = None
    db = None


mongo = MongoDB()


async def connect_to_mongo() -> None:
    mongo.client = AsyncIOMotorClient(settings.MONGODB_URI)
    mongo.db = mongo.client[settings.MONGODB_DB]
    # Indexes — keep the fast path fast
    await mongo.db.users.create_index("email", unique=True)
    await mongo.db.interactions.create_index([("user_id", 1), ("timestamp", -1)])
    await mongo.db.notes.create_index([("user_id", 1), ("video_id", 1)])
    await mongo.db.quiz_attempts.create_index([("user_id", 1), ("timestamp", -1)])
    await mongo.db.chat_history.create_index([("user_id", 1), ("timestamp", 1)])
    print("[Mongo] connected & indexes ensured")


async def close_mongo_connection() -> None:
    if mongo.client:
        mongo.client.close()
        print("[Mongo] connection closed")


def get_db():
    return mongo.db
