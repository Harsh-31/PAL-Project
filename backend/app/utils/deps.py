"""Shared FastAPI dependencies."""
from __future__ import annotations
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from bson import ObjectId
from app.utils.security import decode_token
from app.database.mongo import get_db

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


async def current_user(token: str | None = Depends(oauth2)) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    db = get_db()
    try:
        user = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        raise HTTPException(status_code=401, detail="Malformed subject")
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    user["id"] = str(user["_id"])
    user.pop("_id", None)
    user.pop("password", None)
    return user
