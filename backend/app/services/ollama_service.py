"""Wrapper around a local Ollama model.

All prompts are engineered to return strict JSON where structure matters.
Every call goes through _chat which uses Ollama's native /api/chat endpoint
with format='json' so we don't have to parse free text.
"""
from __future__ import annotations
import json
import re
import uuid
import httpx
from app.config import settings


class OllamaService:
    def __init__(self):
        self.base = settings.OLLAMA_BASE_URL.rstrip("/")
        self.model = settings.OLLAMA_MODEL
        self.timeout = 60.0

    # ---------- low-level ----------
    async def _chat(self, system: str, user: str, json_mode: bool = True) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.3},
        }
        if json_mode:
            payload["format"] = "json"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()["message"]["content"]

    @staticmethod
    def _safe_json(raw: str, fallback: dict) -> dict:
        """Parse JSON with a couple of forgiving fallbacks."""
        try:
            return json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
        return fallback

    # ---------- public API ----------
    async def generate_mcq(
        self,
        *,
        concept_name: str,
        chunk_summary: str,
        difficulty: int,
        hobbies: list[str],
        cognitive_state: str = "OnTrack",
    ) -> dict:
        """Adaptive MCQ generation.

        Difficulty (Hybrid RL) and cognitive state (Process KG) have
        deliberately separate, non-overlapping responsibilities in this
        prompt: difficulty is the sole authority over how hard the question
        and distractors are; cognitive state only ever affects how the
        question is presented (language, scaffolding). Cognitive state must
        never be able to raise or lower the requested difficulty — see the
        explicit "DO NOT" lines in STATE_GUIDANCE below, and the authority
        statement in both the system and user prompts.
        """
        system = (
            "You are PAL, an adaptive tutor. Generate exactly ONE multiple-choice question "
            "that tests understanding of the specified concept. "
            "The requested difficulty level is authoritative: it was selected by the Hybrid RL "
            "policy and must be met exactly. Learner cognitive state may affect presentation and "
            "scaffolding, but MUST NOT override the requested difficulty. "
            "Output STRICT JSON only, no prose, no markdown, no code fences. "
            "Schema: {\"question\": str, \"options\": [str,str,str,str], "
            "\"correct_index\": int (0-3), \"explanation\": str}."
        )
        difficulty_levels = {
            1: (
                "- Direct recall / recognition.\n"
                "- Straightforward reasoning.\n"
                "- Clearly distinguishable distractors.\n"
            ),
            2: (
                "- Basic conceptual understanding.\n"
                "- One-step reasoning.\n"
                "- Moderately plausible distractors.\n"
            ),
            3: (
                "- Application of the concept.\n"
                "- Multi-step or comparative reasoning where appropriate.\n"
                "- Plausible distractors.\n"
            ),
            4: (
                "- Deeper application / analysis.\n"
                "- Subtle conceptual distinctions.\n"
                "- Strong plausible distractors.\n"
            ),
            5: (
                "- Advanced application / synthesis / edge-case reasoning.\n"
                "- High conceptual precision.\n"
                "- Challenging plausible distractors.\n"
            ),
        }
        state_guidance = {
            "Struggling": (
                "- Use simple, clear sentence structure.\n"
                "- Provide sufficient context in the question itself.\n"
                "- Avoid unnecessary linguistic complexity.\n"
                "- Make the task being asked explicit.\n"
                "- Do NOT make distractors easier or more obviously wrong because of this state.\n"
                "- Do NOT reduce conceptual difficulty below the requested level.\n"
            ),
            "OnTrack": (
                "- Use standard academic phrasing.\n"
                "- Normal level of scaffolding.\n"
            ),
            "Mastered": (
                "- Use concise, domain-appropriate academic phrasing.\n"
                "- Minimal scaffolding.\n"
                "- Do NOT increase difficulty beyond the requested level.\n"
                "- Do NOT automatically introduce cross-concept synthesis unless the requested "
                "difficulty level above already calls for it.\n"
            ),
        }
        user = (
            f"Concept: {concept_name}\n"
            f"Lecture chunk summary: {chunk_summary}\n"
            "\n"
            f"Requested difficulty: {difficulty}/5 — selected by Hybrid RL, authoritative.\n"
            f"{difficulty_levels.get(difficulty, difficulty_levels[3])}"
            "\n"
            f"Learner cognitive state: {cognitive_state} — from Process KG, presentation guidance only.\n"
            f"{state_guidance.get(cognitive_state, state_guidance['OnTrack'])}"
            "\n"
            "A learner may legitimately be e.g. difficulty=5 + Struggling, or difficulty=2 + Mastered — "
            "these are not contradictions. Difficulty describes what level of reasoning is required; "
            "cognitive state describes how that question should be presented. Never let cognitive "
            "state change what difficulty level above requires.\n"
            "\n"
            "Additional rules:\n"
            "- The question and all options must be phrased in direct, domain-appropriate language.\n"
            "- Do NOT reference sports, movies, hobbies, or everyday analogies in the question or options.\n"
            "- Exactly 4 options.\n"
            "- Exactly one clearly correct answer; the other three should be plausible distractors "
            "matching the requested difficulty level above.\n"
            "- Explanation should be one to two crisp sentences justifying the correct answer."
        )
        raw = await self._chat(system, user, json_mode=True)
        data = self._safe_json(raw, {
            "question": f"Which statement best describes {concept_name}?",
            "options": [
                f"{concept_name} is a foundational idea in this course.",
                "It is unrelated to the current lecture.",
                "It only appears in advanced graduate courses.",
                "It has been deprecated in modern practice.",
            ],
            "correct_index": 0,
            "explanation": f"{concept_name} is directly introduced in this chunk.",
        })
        # Repair loop — never send a malformed quiz to the UI
        opts = data.get("options") or []
        if not isinstance(opts, list) or len(opts) != 4:
            data["options"] = (opts + ["Option A", "Option B", "Option C", "Option D"])[:4]
        try:
            data["correct_index"] = int(data.get("correct_index", 0)) % 4
        except Exception:
            data["correct_index"] = 0
        # The model occasionally returns valid JSON with an empty (not missing)
        # "explanation" — setdefault alone won't catch that, and a blank
        # explanation means the learner sees nothing after a wrong answer.
        if not (data.get("explanation") or "").strip():
            data["explanation"] = f"The correct answer is: {data['options'][data['correct_index']]}"
        data["id"] = str(uuid.uuid4())
        return data

    async def summarize_with_hobby(
        self, *, concept_name: str, chunk_summary: str, hobbies: list[str]
    ) -> dict:
        """Post-lecture personalised summary using hobby analogies (per PRD §2 Remediation)."""
        system = (
            "You are PAL, an adaptive tutor. Produce a short personalised summary. "
            "Output STRICT JSON: {\"summary\": str, \"analogy\": str, \"next_focus\": str}."
        )
        user = (
            f"Concept: {concept_name}\n"
            f"Chunk summary: {chunk_summary}\n"
            f"Learner hobbies: {', '.join(hobbies) if hobbies else 'none provided'}\n"
            "Keep 'summary' 2 sentences, 'analogy' one vivid sentence tied to a hobby, "
            "'next_focus' one sentence on what to study next."
        )
        raw = await self._chat(system, user, json_mode=True)
        return self._safe_json(raw, {
            "summary": chunk_summary,
            "analogy": "",
            "next_focus": f"Practice a small problem on {concept_name}.",
        })

    async def chat_doubt(
        self, *, message: str, context_summary: str = "", hobbies: list[str] | None = None
    ) -> str:
        """Free-form Q&A from the sidebar. Returns plain text."""
        system = (
            "You are PAL, a patient tutor. Answer clearly and concisely. "
            "Use the provided lecture context when relevant. "
            "If the learner names a hobby, feel free to use a short analogy."
        )
        user = (
            f"Lecture context: {context_summary or '(none)'}\n"
            f"Learner hobbies: {', '.join(hobbies or []) or '(none)'}\n"
            f"Question: {message}"
        )
        return await self._chat(system, user, json_mode=False)

    async def embed(self, text: str) -> list[float] | None:
        """Call Ollama's embedding endpoint. Returns the vector, or None on failure.

        Used by the recommendation engine to score cross-course concept similarity.
        Failure returns None so callers can gracefully fall back to topic matching.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{settings.OLLAMA_BASE_URL}/api/embeddings",
                    json={"model": settings.OLLAMA_EMBED_MODEL, "prompt": text},
                )
                r.raise_for_status()
                data = r.json()
                vec = data.get("embedding")
                if isinstance(vec, list) and vec:
                    return [float(x) for x in vec]
                return None
        except Exception as e:
            print(f"[Ollama] embed failed: {e}")
            return None


ollama = OllamaService()