"""Tests for the remedial-recommendation lifecycle:
recommender.record_active_recommendations / retire_recommendations_for_concept.

Minimal in-memory fake of the specific Motor query patterns these two
functions use (compound-filter update_one with $setOnInsert, update_many,
async-iterable find) — intentionally separate from the existing
_FakeCollection in test_adaptive_persistence_integration.py, which is keyed
by _id and shouldn't be stretched to cover a different query shape.
"""
import asyncio
from app.services import recommender


def _run(coro):
    return asyncio.run(coro)


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        self._iter = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeRemediationCollection:
    def __init__(self):
        self.docs: list[dict] = []

    @staticmethod
    def _match(doc, query):
        return all(doc.get(k) == v for k, v in query.items())

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if self._match(doc, query):
                if "$set" in update:
                    doc.update(update["$set"])
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)

    async def update_many(self, query, update):
        for doc in self.docs:
            if self._match(doc, query) and "$set" in update:
                doc.update(update["$set"])

    def find(self, query):
        return _AsyncIter([d for d in self.docs if self._match(d, query)])


class _FakeDB:
    def __init__(self):
        self.remedial_recommendations = _FakeRemediationCollection()


def _patch_db(monkeypatch):
    fake_db = _FakeDB()
    monkeypatch.setattr(recommender, "get_db", lambda: fake_db)
    return fake_db


# ---------------------------------------------------------------------------
# record_active_recommendations
# ---------------------------------------------------------------------------

def test_record_active_recommendations_creates_active_rows(monkeypatch):
    fake_db = _patch_db(monkeypatch)
    lectures = [{"lecture_id": "lec-1"}, {"lecture_id": "lec-2"}]

    _run(recommender.record_active_recommendations("u1", "c1", lectures))

    rows = fake_db.remedial_recommendations.docs
    assert len(rows) == 2
    assert all(r["status"] == "active" for r in rows)
    assert {r["lecture_id"] for r in rows} == {"lec-1", "lec-2"}
    assert all(r["user_id"] == "u1" and r["concept_id"] == "c1" for r in rows)


def test_record_active_recommendations_is_idempotent(monkeypatch):
    fake_db = _patch_db(monkeypatch)
    lectures = [{"lecture_id": "lec-1"}]

    _run(recommender.record_active_recommendations("u1", "c1", lectures))
    _run(recommender.record_active_recommendations("u1", "c1", lectures))  # same lecture again

    assert len(fake_db.remedial_recommendations.docs) == 1


def test_record_active_recommendations_noop_on_empty_list(monkeypatch):
    fake_db = _patch_db(monkeypatch)
    _run(recommender.record_active_recommendations("u1", "c1", []))
    assert fake_db.remedial_recommendations.docs == []


# ---------------------------------------------------------------------------
# retire_recommendations_for_concept
# ---------------------------------------------------------------------------

def test_retire_marks_active_rows_retired_and_returns_lecture_ids(monkeypatch):
    fake_db = _patch_db(monkeypatch)
    _run(recommender.record_active_recommendations(
        "u1", "c1", [{"lecture_id": "lec-1"}, {"lecture_id": "lec-2"}],
    ))

    retired_ids = _run(recommender.retire_recommendations_for_concept("u1", "c1"))

    assert set(retired_ids) == {"lec-1", "lec-2"}
    assert all(r["status"] == "retired" for r in fake_db.remedial_recommendations.docs)
    assert all("retired_at" in r for r in fake_db.remedial_recommendations.docs)


def test_retire_is_a_safe_noop_when_nothing_active(monkeypatch):
    _patch_db(monkeypatch)
    retired_ids = _run(recommender.retire_recommendations_for_concept("u1", "c1"))
    assert retired_ids == []


def test_retire_only_touches_the_specified_concept(monkeypatch):
    """Preserve unrelated content: retiring c1's remediation must not affect c2's."""
    fake_db = _patch_db(monkeypatch)
    _run(recommender.record_active_recommendations("u1", "c1", [{"lecture_id": "lec-c1"}]))
    _run(recommender.record_active_recommendations("u1", "c2", [{"lecture_id": "lec-c2"}]))

    retired_ids = _run(recommender.retire_recommendations_for_concept("u1", "c1"))

    assert retired_ids == ["lec-c1"]
    c2_row = next(r for r in fake_db.remedial_recommendations.docs if r["concept_id"] == "c2")
    assert c2_row["status"] == "active"  # untouched


def test_retire_only_touches_the_specified_learner(monkeypatch):
    fake_db = _patch_db(monkeypatch)
    _run(recommender.record_active_recommendations("u1", "c1", [{"lecture_id": "lec-1"}]))
    _run(recommender.record_active_recommendations("u2", "c1", [{"lecture_id": "lec-1"}]))

    retired_ids = _run(recommender.retire_recommendations_for_concept("u1", "c1"))

    assert retired_ids == ["lec-1"]
    u2_row = next(r for r in fake_db.remedial_recommendations.docs if r["user_id"] == "u2")
    assert u2_row["status"] == "active"  # untouched — a different learner's record


def test_retire_is_idempotent_second_call_finds_nothing_new(monkeypatch):
    """Repeated Mastered interactions must not error or re-retire anything."""
    _patch_db(monkeypatch)
    _run(recommender.record_active_recommendations("u1", "c1", [{"lecture_id": "lec-1"}]))

    first = _run(recommender.retire_recommendations_for_concept("u1", "c1"))
    second = _run(recommender.retire_recommendations_for_concept("u1", "c1"))

    assert first == ["lec-1"]
    assert second == []  # already retired — nothing active left to find
