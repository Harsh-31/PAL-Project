"""Tests for immediate answer feedback — routes.quiz._generate_incorrect_answer_analogy.

This is deliberately NOT part of AdaptiveLearningOrchestrator (see
pal_agent.py's docstring: "orchestrator MUST NOT generate explanations
itself"). It lives directly in routes/quiz.py and is called only when
`correct == false` (routes/quiz.py's /submit handler) — its own signature,
`(chunk_id, hobbies)`, has no Process-KG state, RL, or correctness parameter
at all, which is itself evidence that it structurally cannot depend on any
of those (Test I: this same function is the ONLY analogy-generation path
regardless of which state — Frustrated/Struggling/OnTrack/Confident/
Mastered — an incorrect answer happens to land in).

The route-level "only called when correct == false" wiring itself
(routes/quiz.py: `if not correct: analogy = await
_generate_incorrect_answer_analogy(...)`) is verified by direct code
inspection in the final report rather than a FastAPI TestClient integration
test — this project has no route-level test infrastructure (auth/Mongo/Neo4j
dependency overrides), and introducing one for a single call site would be
disproportionate scope for this task.
"""
import asyncio
from app.routes import quiz as quiz_route
from app.services import kg_service
from app.services.ollama_service import ollama


def _run(coro):
    return asyncio.run(coro)


def _returns(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


def _raises(exc):
    async def _fn(*args, **kwargs):
        raise exc
    return _fn


CHUNK = {
    "id": "chunk-1",
    "summary": "Recursion is when a function calls itself.",
    "concept": {"id": "concept-1", "name": "Recursion"},
}


def test_A_generates_analogy_when_chunk_and_llm_succeed(monkeypatch):
    monkeypatch.setattr(kg_service, "get_chunk", _returns(CHUNK))
    monkeypatch.setattr(
        ollama, "summarize_with_hobby",
        _returns({"summary": "A recap.", "analogy": "Like a set of Russian nesting dolls.",
                  "next_focus": "Practice base cases."}),
    )

    result = _run(quiz_route._generate_incorrect_answer_analogy("chunk-1", ["cricket"]))

    assert result == "Like a set of Russian nesting dolls."


def test_A_independent_of_process_kg_or_correctness_by_construction(monkeypatch):
    """Test I: the function's own signature (chunk_id, hobbies) has no state/
    correctness parameter — it cannot special-case Frustrated/Struggling/
    OnTrack/Confident/Mastered even if it wanted to. This is the same
    function regardless of which state an incorrect answer lands in."""
    import inspect
    params = list(inspect.signature(quiz_route._generate_incorrect_answer_analogy).parameters)
    assert params == ["chunk_id", "hobbies"]


def test_falls_back_to_summary_when_analogy_field_is_empty(monkeypatch):
    monkeypatch.setattr(kg_service, "get_chunk", _returns(CHUNK))
    monkeypatch.setattr(
        ollama, "summarize_with_hobby",
        _returns({"summary": "A recap sentence.", "analogy": "", "next_focus": "..."}),
    )

    result = _run(quiz_route._generate_incorrect_answer_analogy("chunk-1", []))

    assert result == "A recap sentence."  # simplified explanation still provided


def test_returns_none_when_chunk_not_found(monkeypatch):
    monkeypatch.setattr(kg_service, "get_chunk", _returns(None))

    result = _run(quiz_route._generate_incorrect_answer_analogy("missing-chunk", []))

    assert result is None


def test_returns_none_and_does_not_raise_when_llm_fails(monkeypatch):
    monkeypatch.setattr(kg_service, "get_chunk", _returns(CHUNK))
    monkeypatch.setattr(ollama, "summarize_with_hobby", _raises(RuntimeError("ollama down")))

    result = _run(quiz_route._generate_incorrect_answer_analogy("chunk-1", []))

    assert result is None  # never raises — must not block quiz submission


def test_works_with_no_hobbies(monkeypatch):
    calls = []

    async def fake_summarize(*, concept_name, chunk_summary, hobbies):
        calls.append(hobbies)
        return {"summary": "s", "analogy": "a", "next_focus": "n"}

    monkeypatch.setattr(kg_service, "get_chunk", _returns(CHUNK))
    monkeypatch.setattr(ollama, "summarize_with_hobby", fake_summarize)

    result = _run(quiz_route._generate_incorrect_answer_analogy("chunk-1", []))

    assert result == "a"
    assert calls == [[]]
