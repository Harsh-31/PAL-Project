"""Integration test for the Mongo-backed persistence + controller wiring.

Uses a minimal in-memory fake of the Motor collection API (find_one,
insert_one, update_one, replace_one, find().sort().limit()) so the full
select_difficulty -> process_outcome -> select_difficulty loop can be
exercised without a real MongoDB instance, proving the plumbing between
the pure algorithm classes and app.services.adaptive.persistence /
controller actually works end to end.
"""
import asyncio

from app.services.adaptive import controller as controller_module
from app.services.adaptive import persistence as persistence_module
from app.services.adaptive.controller import AdaptiveDifficultyController


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=1):
        self._docs.sort(key=lambda d: d.get(field), reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeCollection:
    def __init__(self):
        self.docs: dict = {}
        self._auto_id = 0

    async def find_one(self, query):
        if "_id" in query:
            return self.docs.get(query["_id"])
        for d in self.docs.values():
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None

    async def insert_one(self, doc):
        doc = dict(doc)
        if "_id" not in doc:
            self._auto_id += 1
            doc["_id"] = self._auto_id
        self.docs[doc["_id"]] = doc
        return doc

    async def update_one(self, query, update, upsert=False):
        _id = query["_id"]
        doc = self.docs.get(_id)
        if doc is None:
            if not upsert:
                return
            doc = {"_id": _id}
            self.docs[_id] = doc
        if "$set" in update:
            doc.update(update["$set"])

    async def replace_one(self, query, doc, upsert=False):
        self.docs[query["_id"]] = dict(doc)

    def find(self, query):
        matched = [d for d in self.docs.values() if all(d.get(k) == v for k, v in query.items())]
        return _Cursor(matched)


class _FakeDB:
    def __init__(self):
        self._collections: dict = {}

    def __getattr__(self, name):
        if name not in self._collections:
            self._collections[name] = _FakeCollection()
        return self._collections[name]


def test_full_select_process_select_loop_persists_across_calls(monkeypatch):
    fake_db = _FakeDB()
    monkeypatch.setattr(persistence_module, "get_db", lambda: fake_db)

    controller = AdaptiveDifficultyController()

    async def _run():
        user_id, concept_id = "user-1", "concept-1"

        decision1 = await controller.select_difficulty(user_id, concept_id, baseline_skill=0.6)
        assert decision1.state.timestep == 0
        pending = decision1.to_pending()

        outcome = await controller.process_outcome(
            user_id=user_id, concept_id=concept_id, question_id="q1",
            correct=True, response_time_sec=5.0, pending=pending,
        )
        assert outcome["state"].timestep == 1
        assert outcome["q_value_after"] != 0.0 or outcome["reward"].total == 0.0

        # A fresh load must reflect the persisted, updated state.
        reloaded = await persistence_module.load_learner_state(user_id, concept_id)
        assert reloaded.timestep == 1
        assert reloaded.skill != 0.5

        # The decision log recorded exactly one entry.
        trace = await persistence_module.get_decision_trace(user_id, concept_id)
        assert len(trace) == 1
        assert trace[0]["selected_action"] == pending["action"]
        assert trace[0]["timestep"] == 1

        # A second select_difficulty call sees the evolved state (not a fresh one).
        decision2 = await controller.select_difficulty(user_id, concept_id)
        assert decision2.state.timestep == 1
        assert decision2.state.skill == reloaded.skill

        return decision1, outcome, decision2

    decision1, outcome, decision2 = asyncio.run(_run())
    assert decision1.action in list(controller_module.ACTIONS)
    assert decision2.action in list(controller_module.ACTIONS)


def test_process_outcome_falls_back_gracefully_without_pending(monkeypatch):
    """A question generated before this engine existed (no rl_pending on the
    doc) must still be processable — the fallback reconstructs a pending
    record from the currently persisted state instead of crashing."""
    fake_db = _FakeDB()
    monkeypatch.setattr(persistence_module, "get_db", lambda: fake_db)
    controller = AdaptiveDifficultyController()

    async def _run():
        outcome = await controller.process_outcome(
            user_id="user-2", concept_id="concept-2", question_id="q-legacy",
            correct=False, response_time_sec=None, pending=None, current_difficulty=3,
        )
        assert outcome["state"].timestep == 1
        return outcome

    outcome = asyncio.run(_run())
    assert outcome["reward"].r_acc == -0.5
