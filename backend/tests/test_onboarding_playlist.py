"""Tests for the goal -> concept -> starter-playlist pipeline:
recommender._select_concepts_for_goal (top-k/floor + top-3 fallback) and
recommender.build_onboarding_starter_playlist (relevance ranking, the
10-lecture cap, deduplication, and order preservation).

Neo4j/Ollama are monkeypatched with deterministic fakes: `cos_sim` is
replaced with a stub that reads the intended similarity directly out of the
fake "embedding" ([similarity_value]) so each test can pin exact scores
without needing real vector math or a live embedding model.
"""
import asyncio
from app.services import recommender


def _run(coro):
    return asyncio.run(coro)


def _concept(cid, name, sim, chunks=None):
    return {
        "concept_id": cid, "concept_name": name, "difficulty": 1,
        "embedding": [sim],
        "course_id": f"course-{cid}", "course_title": f"Course {cid}",
        "chunks": chunks or [],
    }


def _fake_cos_sim(goal_vec, concept_vec):
    return concept_vec[0]


def _returns(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


def _patch_scoring(monkeypatch, concepts):
    monkeypatch.setattr(recommender.ollama, "embed", _returns([1.0]))
    monkeypatch.setattr(recommender, "cos_sim", _fake_cos_sim)
    monkeypatch.setattr(recommender, "_fetch_all_concepts_with_embeddings", _returns(concepts))


def _lecture(lid, concept_ids, title=None):
    """A composed-playlist-shaped lecture row whose chunks carry concept_id,
    same shape kg_service.get_composed_playlist actually returns."""
    return {
        "lecture_id": lid, "title": title or lid, "youtube_id": "yt", "duration_sec": 100,
        "source_course_id": "c0", "source_course_title": "Course",
        "chunks": [{"id": f"{lid}-ch", "concept_id": cid} for cid in concept_ids],
    }


def _patch_composed_playlist(monkeypatch, lectures_by_concept: dict, fixed_order: list[dict] | None = None):
    """fixed_order, if given, ignores ordered_concept_ids entirely — used to
    prove build_onboarding_starter_playlist preserves whatever order
    get_composed_playlist itself returns, rather than re-deriving its own."""
    async def fake_composed(user_id, concept_ids, ordered_concept_ids=None):
        if fixed_order is not None:
            return [lec for lec in fixed_order
                    if any(ch["concept_id"] in concept_ids for ch in lec["chunks"])]
        order = ordered_concept_ids or concept_ids
        seen = set()
        out = []
        for cid in order:
            for lec in lectures_by_concept.get(cid, []):
                if lec["lecture_id"] in seen:
                    continue
                seen.add(lec["lecture_id"])
                out.append(lec)
        return out
    monkeypatch.setattr(recommender.kg_service, "get_composed_playlist", fake_composed)


# ---------------------------------------------------------------------------
# _select_concepts_for_goal — fallback behavior
# ---------------------------------------------------------------------------

def test_A_broad_goal_activates_fallback_and_selects_top_3(monkeypatch):
    concepts = [
        _concept("c1", "Intro", 0.40), _concept("c2", "Basics", 0.35),
        _concept("c3", "Overview", 0.30), _concept("c4", "Unrelated", 0.20),
    ]
    _patch_scoring(monkeypatch, concepts)

    selected, fallback = _run(recommender._select_concepts_for_goal("very broad goal"))

    assert fallback is True
    assert [cid for cid, _ in selected] == ["c1", "c2", "c3"]  # top 3 by raw similarity


def test_B_focused_goal_does_not_activate_fallback(monkeypatch):
    concepts = [
        _concept("c1", "A", 0.90), _concept("c2", "B", 0.85),
        _concept("c3", "C", 0.80), _concept("c4", "D", 0.75),
        _concept("c5", "Unrelated", 0.40),
    ]
    _patch_scoring(monkeypatch, concepts)

    selected, fallback = _run(recommender._select_concepts_for_goal("focused goal"))

    assert fallback is False
    ids = [cid for cid, _ in selected]
    assert ids == ["c1", "c2", "c3", "c4"]
    assert "c5" not in ids  # below floor, correctly excluded


def test_fallback_boundary_exactly_3_above_floor_does_not_activate(monkeypatch):
    """Spec says "fewer than 3" — exactly 3 concepts clearing the floor must
    use the normal path, not the fallback."""
    concepts = [_concept("c1", "A", 0.90), _concept("c2", "B", 0.80), _concept("c3", "C", 0.70)]
    _patch_scoring(monkeypatch, concepts)

    selected, fallback = _run(recommender._select_concepts_for_goal("goal"))

    assert fallback is False
    assert [cid for cid, _ in selected] == ["c1", "c2", "c3"]


def test_fallback_never_changes_which_concepts_clear_the_floor(monkeypatch):
    """The fallback relaxes how MANY concepts count as enough for THIS goal —
    it must never change what similarity_floor itself means. With 2 clearing
    concepts and the floor left at its default, the fallback must select by
    raw similarity only, not silently re-admit something the floor rejected
    for a different, lower reason."""
    concepts = [_concept("c1", "A", 0.90), _concept("c2", "B", 0.66), _concept("c3", "C", 0.10)]
    _patch_scoring(monkeypatch, concepts)

    selected, fallback = _run(recommender._select_concepts_for_goal("goal"))

    assert fallback is True  # only 2 concepts (c1, c2) clear 0.65
    assert [cid for cid, _ in selected] == ["c1", "c2", "c3"]  # top 3 by raw similarity, c3 included despite low score


# ---------------------------------------------------------------------------
# build_onboarding_starter_playlist — cap, relevance ranking, dedup, ordering
# ---------------------------------------------------------------------------

def test_A2_broad_goal_starter_playlist_is_nonempty_and_within_cap(monkeypatch):
    concepts = [
        _concept("c1", "Intro", 0.40), _concept("c2", "Basics", 0.35), _concept("c3", "Overview", 0.30),
    ]
    _patch_scoring(monkeypatch, concepts)
    _patch_composed_playlist(monkeypatch, {
        "c1": [_lecture("lecA", ["c1"])],
        "c2": [_lecture("lecB", ["c2"])],
        "c3": [_lecture("lecC", ["c3"])],
    })

    result = _run(recommender.build_onboarding_starter_playlist("u1", "very broad goal"))

    assert result["fallback_activated"] is True
    assert len(result["lectures"]) > 0
    assert len(result["lectures"]) <= recommender.MAX_ONBOARDING_LECTURES


def test_C_caps_to_exactly_10_keeping_highest_relevance(monkeypatch):
    concepts = [_concept("c_hi", "High relevance", 0.95), _concept("c_lo", "Lower relevance", 0.70)]
    _patch_scoring(monkeypatch, concepts)
    hi_lectures = [_lecture(f"hi-{i}", ["c_hi"]) for i in range(6)]
    lo_lectures = [_lecture(f"lo-{i}", ["c_lo"]) for i in range(6)]
    _patch_composed_playlist(monkeypatch, {"c_hi": hi_lectures, "c_lo": lo_lectures})

    result = _run(recommender.build_onboarding_starter_playlist("u1", "goal"))

    assert result["raw_lecture_count"] == 12
    assert len(result["lectures"]) == 10
    kept_ids = {lec["lecture_id"] for lec in result["lectures"]}
    # all 6 higher-relevance lectures must survive the cut before any lower ones do
    assert all(f"hi-{i}" in kept_ids for i in range(6))
    assert sum(1 for i in range(6) if f"lo-{i}" in kept_ids) == 4


def test_D_small_playlist_is_not_padded(monkeypatch):
    concepts = [_concept("c1", "A", 0.90)]
    _patch_scoring(monkeypatch, concepts)
    _patch_composed_playlist(monkeypatch, {"c1": [_lecture("a", ["c1"]), _lecture("b", ["c1"]), _lecture("c", ["c1"])]})

    result = _run(recommender.build_onboarding_starter_playlist("u1", "goal"))

    assert result["raw_lecture_count"] == 3
    assert len(result["lectures"]) == 3  # not padded up to 10


def test_E_lecture_reachable_via_multiple_concepts_keeps_strongest_score(monkeypatch):
    concepts = [_concept("c_weak", "Weak match", 0.60), _concept("c_strong", "Strong match", 0.90)]
    _patch_scoring(monkeypatch, concepts)
    multi_concept_lecture = _lecture("multi", ["c_weak", "c_strong"])
    other_lecture = _lecture("other", ["c_strong"])
    # Simulate get_composed_playlist already having deduplicated to one row
    # for "multi" (as the real Cypher DISTINCT does), with both concept ids
    # visible across its chunks.
    _patch_composed_playlist(monkeypatch, {}, fixed_order=[other_lecture, multi_concept_lecture])

    result = _run(recommender.build_onboarding_starter_playlist("u1", "goal"))

    lecture_ids = [lec["lecture_id"] for lec in result["lectures"]]
    assert lecture_ids.count("multi") == 1  # appears exactly once, not duplicated


def test_F_selected_subset_preserves_existing_composed_playlist_order(monkeypatch):
    """c2 has higher relevance than c1, so relevance-based SELECTION would
    rank lecB above lecA — but get_composed_playlist's own (here: fixed,
    prerequisite-style) order puts lecA first. The final playlist must
    respect that existing order for the selected subset, not re-sort by
    relevance."""
    concepts = [_concept("c1", "Lower relevance, earlier in curriculum", 0.60),
                _concept("c2", "Higher relevance, later in curriculum", 0.95)]
    _patch_scoring(monkeypatch, concepts)
    lecA = _lecture("lecA", ["c1"])
    lecB = _lecture("lecB", ["c2"])
    _patch_composed_playlist(monkeypatch, {}, fixed_order=[lecA, lecB])  # fixed pre-existing order

    result = _run(recommender.build_onboarding_starter_playlist("u1", "goal"))

    assert [lec["lecture_id"] for lec in result["lectures"]] == ["lecA", "lecB"]
