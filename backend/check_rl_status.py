"""Quick CLI check that the Hybrid RL engine is actually running against the
LIVE database (not the simulation) — prints counts + samples from the
RL-specific Mongo collections written by app.services.adaptive.persistence.

Run this after answering a few quiz questions in the app.

Usage:
    cd backend
    python check_rl_status.py            # everything
    python check_rl_status.py <user_id>  # scoped to one learner
"""
import asyncio
import sys
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings


async def main() -> None:
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DB]
    user_id = sys.argv[1] if len(sys.argv) > 1 else None

    n_states = await db.learner_states.count_documents({})
    n_q = await db.q_values.count_documents({})
    n_decisions = await db.adaptive_decisions.count_documents({})
    epsilon_doc = await db.rl_meta.find_one({"_id": "epsilon"})

    print("=== Hybrid RL engine status (live DB) ===")
    print(f"learner_states collection:     {n_states} doc(s)")
    print(f"q_values collection:           {n_q} discretized state(s) with learned Q-rows")
    print(f"adaptive_decisions collection: {n_decisions} logged decision(s)")
    print(f"shared epsilon:                {epsilon_doc['value'] if epsilon_doc else '(default — not yet persisted, no attempt submitted yet)'}")

    if n_states == 0:
        print("\nNo learner_states yet -> the RL engine has not run a single "
              "generate/submit cycle against this DB yet. Answer one quiz "
              "question in the app, then re-run this script.")
        client.close()
        return

    if n_q:
        print("\nSample learned Q-rows (state_key -> {EASY, MEDIUM, HARD} Q-values):")
        async for row in db.q_values.find({}).limit(8):
            print(f"  {row['_id']}: {row['q']}")
        nonzero = await db.q_values.count_documents({"$or": [
            {"q.EASY": {"$ne": 0}}, {"q.MEDIUM": {"$ne": 0}}, {"q.HARD": {"$ne": 0}},
        ]})
        print(f"  ({nonzero}/{n_q} rows have moved away from their 0.0 initialization)")

    query = {"user_id": user_id} if user_id else {}
    print(f"\nLearner state(s){' for ' + user_id if user_id else ' (first 5)'}:")
    async for s in db.learner_states.find(query).limit(5):
        print(f"  {s['_id']}: timestep={s['timestep']} skill={s['skill']:.3f} "
              f"recent_accuracy={s['recent_accuracy']:.3f} confidence={s['confidence']:.3f} "
              f"streak_momentum={s['streak_momentum']:.3f}")

    dquery = {"learner_id": user_id} if user_id else {}
    print(f"\nMost recent decisions{' for ' + user_id if user_id else ' (last 8)'}:")
    async for d in db.adaptive_decisions.find(dquery).sort("timestamp", -1).limit(8):
        print(f"  t={d['timestep']:>3} action={d['selected_action']:<6} "
              f"w={d['blend_weight']:.3f}  reward={d['reward']:.3f}  "
              f"q_before={d['q_value_before']:.4f} -> q_after={d['q_value_after']:.4f}  "
              f"({d['session_id']})")

    print("\nWhat proves RL is really deciding (not just displaying):")
    print("  1. q_before != q_after on repeated rows above -> Q-learning updates are landing.")
    print("  2. `w` (blend_weight) rising across a learner's timesteps -> hybrid policy is")
    print("     shifting from the statistical prior toward the learned Q-policy.")
    print("  3. `selected_action` isn't the same value every single row for one learner.")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
