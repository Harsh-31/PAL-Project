"""Sidebar: notes, chat, code playground.

Notes are stored in MongoDB, chat calls Ollama and logs the full transcript,
and the code playground runs Python in a subprocess with a hard timeout.
Suitable for the MVP; production would sandbox this properly.
"""
import subprocess
import sys
import tempfile
import os
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from app.database.mongo import get_db
from app.models.schemas import NoteIn, ChatIn, CodeRunIn
from app.services import kg_service
from app.services.ollama_service import ollama
from app.utils.deps import current_user

router = APIRouter(prefix="/api/sidebar", tags=["sidebar"])


# ---------- Notes ----------
@router.get("/notes/{video_id}")
async def list_notes(video_id: str, user=Depends(current_user)):
    db = get_db()
    cursor = db.notes.find({"user_id": user["id"], "video_id": video_id}).sort("timestamp_sec", 1)
    out = []
    async for n in cursor:
        n["id"] = str(n["_id"]); n.pop("_id")
        n["created_at"] = n["created_at"].isoformat() if n.get("created_at") else None
        out.append(n)
    return out


@router.post("/notes")
async def create_note(payload: NoteIn, user=Depends(current_user)):
    db = get_db()
    doc = payload.model_dump()
    doc["user_id"] = user["id"]
    doc["created_at"] = datetime.now(timezone.utc)
    res = await db.notes.insert_one(doc)
    return {"id": str(res.inserted_id), "ok": True}


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str, user=Depends(current_user)):
    db = get_db()
    try:
        r = await db.notes.delete_one({"_id": ObjectId(note_id), "user_id": user["id"]})
    except Exception:
        raise HTTPException(status_code=400, detail="Bad id")
    return {"deleted": r.deleted_count}


# ---------- Chat ----------
@router.post("/chat")
async def chat(payload: ChatIn, user=Depends(current_user)):
    db = get_db()
    context_summary = ""
    if payload.chunk_id:
        chunk = await kg_service.get_chunk(payload.chunk_id)
        if chunk:
            context_summary = f"{chunk['concept']['name']}: {chunk['summary']}"

    udoc = await db.users.find_one({"_id": ObjectId(user["id"])})
    hobbies = udoc.get("hobbies", []) if udoc else []

    reply = await ollama.chat_doubt(
        message=payload.message,
        context_summary=context_summary,
        hobbies=hobbies,
    )
    ts = datetime.now(timezone.utc)
    await db.chat_history.insert_many([
        {"user_id": user["id"], "role": "user", "content": payload.message,
         "lecture_id": payload.lecture_id, "chunk_id": payload.chunk_id, "timestamp": ts},
        {"user_id": user["id"], "role": "assistant", "content": reply,
         "lecture_id": payload.lecture_id, "chunk_id": payload.chunk_id, "timestamp": ts},
    ])
    return {"reply": reply}


@router.get("/chat/history")
async def chat_history(user=Depends(current_user), limit: int = 50):
    db = get_db()
    cursor = db.chat_history.find({"user_id": user["id"]}).sort("timestamp", -1).limit(limit)
    msgs = []
    async for m in cursor:
        m["id"] = str(m["_id"]); m.pop("_id")
        m["timestamp"] = m["timestamp"].isoformat()
        msgs.append(m)
    msgs.reverse()
    return msgs


# ---------- Code playground ----------
@router.post("/code/run")
async def run_code(payload: CodeRunIn, user=Depends(current_user)):
    if payload.language != "python":
        raise HTTPException(status_code=400, detail="Only python supported in MVP")
    if len(payload.code) > 5000:
        raise HTTPException(status_code=400, detail="Code too large")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(payload.code)
        path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=5,
        )
        return {"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:],
                "returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Execution timed out (5s limit)", "returncode": -1}
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
