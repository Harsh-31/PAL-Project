"""Tests for the new Process-KG -> Recommendation Engine bridge functions:
recommend_challenge() (new selection strategy) and recommend_for_intervention()
(dispatcher). Neo4j-backed helpers (_fetch_all_concepts_with_embeddings,
recommend_supplementary) are monkeypatched so these run without a live DB —
recommend_supplementary's own ranking logic is pre-existing and unchanged.
"""
import asyncio
from app.services import recommender


def _run(coro):
    return asyncio.run(coro)


def _returns(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


# ---------------------------------------------------------------------------
# recommend_for_intervention — dispatch logic
# ---------------------------------------------------------------------------

def test_dispatches_remediation_to_recommend_supplementary(monkeypatch):
    calls = []

    async def fake_supp(user_id, concept_ids, struggle_threshold=None, **kw):
        calls.append((user_id, concept_ids, struggle_threshold))
        return [{"lecture_id": "x"}]

    monkeypatch.setattr(recommender, "recommend_supplementary", fake_supp)
    result = _run(recommender.recommend_for_intervention("u1", "insert_prerequisite_video", "c1", 0.3))

    assert result == [{"lecture_id": "x"}]
    assert calls[0][0] == "u1"
    assert calls[0][1] == ["c1"]
    assert calls[0][2] >= 0.6  # safety-margin threshold above the KG's own 0.55 ceiling


def test_dispatches_challenge_to_recommend_challenge(monkeypatch):
    calls = []

    async def fake_challenge(concept_id, **kw):
        calls.append(concept_id)
        return [{"lecture_id": "y", "challenge": True}]

    monkeypatch.setattr(recommender, "recommend_challenge", fake_challenge)
    result = _run(recommender.recommend_for_intervention("u1", "offer_challenge_content", "c1", 0.8))

    assert result == [{"lecture_id": "y", "challenge": True}]
    assert calls == ["c1"]


def test_returns_empty_for_actions_that_do_not_need_content():
    for action in ["continue_normal", "skip_next_similar_chunk",
                   "simplify_with_hobby_analogy", "some_unknown_action"]:
        result = _run(recommender.recommend_for_intervention("u1", action, "c1", 0.5))
        assert result == []


# ---------------------------------------------------------------------------
# recommend_challenge — selection logic (inverse of recommend_supplementary)
# ---------------------------------------------------------------------------

def _concept(cid, name, difficulty, embedding, chunks=None):
    return {
        "concept_id": cid, "concept_name": name, "difficulty": difficulty,
        "embedding": embedding, "course_id": f"course-{cid}", "course_title": f"Course {cid}",
        "chunks": chunks or [],
    }


def _lecture_chunk(lecture_id):
    return {"id": f"{lecture_id}-ch1", "start": 0, "end": 10, "summary": "s",
            "lecture_id": lecture_id, "lecture_title": f"Lecture {lecture_id}",
            "youtube_id": "yt", "duration": 100}


def test_recommend_challenge_only_returns_harder_concepts(monkeypatch):
    concepts = [
        _concept("src", "Basics", 2, [1.0, 0.0]),
        _concept("harder", "Advanced", 4, [0.95, 0.05], chunks=[_lecture_chunk("lec-harder")]),
        _concept("easier", "Trivial", 1, [0.95, 0.05], chunks=[_lecture_chunk("lec-easier")]),
        _concept("same", "Parallel", 2, [0.95, 0.05], chunks=[_lecture_chunk("lec-same")]),
    ]
    monkeypatch.setattr(recommender, "_fetch_all_concepts_with_embeddings", _returns(concepts))

    result = _run(recommender.recommend_challenge("src"))
    lecture_ids = {r["lecture_id"] for r in result}

    assert lecture_ids == {"lec-harder"}  # strictly-harder only; equal and easier excluded


def test_recommend_challenge_respects_similarity_floor(monkeypatch):
    concepts = [
        _concept("src", "Basics", 2, [1.0, 0.0]),
        _concept("dissimilar_but_harder", "Unrelated", 5, [0.0, 1.0],
                 chunks=[_lecture_chunk("lec-far")]),
    ]
    monkeypatch.setattr(recommender, "_fetch_all_concepts_with_embeddings", _returns(concepts))

    result = _run(recommender.recommend_challenge("src", min_similarity=0.55))
    assert result == []  # orthogonal embedding -> similarity 0.0, below the floor


def test_recommend_challenge_empty_when_source_concept_missing(monkeypatch):
    monkeypatch.setattr(recommender, "_fetch_all_concepts_with_embeddings", _returns([
        _concept("other", "Other", 3, [1.0, 0.0]),
    ]))
    result = _run(recommender.recommend_challenge("does-not-exist"))
    assert result == []


def test_recommend_challenge_marks_results_as_supplementary_and_challenge(monkeypatch):
    concepts = [
        _concept("src", "Basics", 1, [1.0, 0.0]),
        _concept("harder", "Advanced", 3, [0.9, 0.1], chunks=[_lecture_chunk("lec-harder")]),
    ]
    monkeypatch.setattr(recommender, "_fetch_all_concepts_with_embeddings", _returns(concepts))

    result = _run(recommender.recommend_challenge("src"))
    assert len(result) == 1
    assert result[0]["supplementary"] is True
    assert result[0]["challenge"] is True
    assert result[0]["recommendation_source"] == "semantic_challenge"
    assert result[0]["for_concept_name"] == "Basics"
