"""Request / response schemas — the API contract."""
from typing import Optional
from pydantic import BaseModel, EmailStr, Field


# ---------- Auth ----------
class SignupIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


# ---------- Onboarding ----------
class OnboardingIn(BaseModel):
    # PRD: the intake gathers baseline, goals, evaluation frequency, hobbies.
    # Learners pick one or more LEARNING TRACKS (curated groupings of concepts
    # spanning multiple courses) rather than a single course, so the playlist
    # can be composed across the OER library as described in "Curation".
    track_ids: list[str] = Field(default_factory=list)
    baseline: str          # "beginner" | "intermediate" | "advanced"
    goal: str              # free-text — the learner's stated objective
    evaluation_frequency: str = "per_chunk"  # per_chunk | per_video | per_session
    hobbies: list[str] = Field(default_factory=list)


# ---------- Quiz ----------
class QuizRequest(BaseModel):
    lecture_id: str
    chunk_id: str


class QuizAttemptIn(BaseModel):
    question_id: str
    lecture_id: str
    chunk_id: str
    concept_id: str
    selected_index: int
    correct_index: int
    time_taken_sec: Optional[float] = None


# ---------- Sidebar ----------
class NoteIn(BaseModel):
    video_id: str
    content: str
    timestamp_sec: Optional[float] = None


class ChatIn(BaseModel):
    lecture_id: Optional[str] = None
    chunk_id: Optional[str] = None
    message: str


class CodeRunIn(BaseModel):
    language: str = "python"
    code: str