"""Signup / login routes — email + password, JWT bearer."""
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from app.database.mongo import get_db
from app.models.schemas import SignupIn, LoginIn, TokenOut
from app.utils.security import hash_password, verify_password, create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/signup", response_model=TokenOut)
async def signup(payload: SignupIn):
    db = get_db()
    if await db.users.find_one({"email": payload.email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    doc = {
        "name": payload.name,
        "email": payload.email,
        "password": hash_password(payload.password),
        "onboarded": False,
        "created_at": datetime.now(timezone.utc),
    }
    res = await db.users.insert_one(doc)
    uid = str(res.inserted_id)
    token = create_access_token(uid)
    return TokenOut(
        access_token=token,
        user={"id": uid, "name": payload.name, "email": payload.email, "onboarded": False},
    )


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn):
    db = get_db()
    user = await db.users.find_one({"email": payload.email})
    if not user or not verify_password(payload.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    uid = str(user["_id"])
    token = create_access_token(uid)
    return TokenOut(
        access_token=token,
        user={
            "id": uid,
            "name": user["name"],
            "email": user["email"],
            "onboarded": user.get("onboarded", False),
            "current_course_id": None,  # legacy, removed
            "track_ids": user.get("track_ids", []),
            "target_concept_ids": user.get("target_concept_ids", []),
        },
    )