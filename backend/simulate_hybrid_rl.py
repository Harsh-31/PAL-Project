"""Reproducible simulation/demo of the Hybrid RL PAL controller.

Runs four synthetic learner profiles (struggling, average, fast-learning,
inconsistent) through 100 simulated question interactions each, using the
exact same state/prior/Q-learning/hybrid-policy classes the real backend
uses (app.services.adaptive.*) — no mocking, no fake numbers. This is a
pure-logic simulation (no Mongo/Neo4j needed) so it demonstrates that the
Q-values genuinely change, the learner state genuinely evolves, and the
hybrid policy's RL influence genuinely grows as evidence accumulates.

Usage:
    cd backend
    python simulate_hybrid_rl.py

Writes:
    simulation_output/<profile>.json   — full per-step trace per profile
    simulation_output/summary.csv      — flattened, plot-ready CSV of all steps
"""
from __future__ import annotations
import csv
import json
import math
import random
from pathlib import Path

from app.services.adaptive.actions import ACTION_RANK, Difficulty
from app.services.adaptive.discretization import discretize
from app.services.adaptive.hybrid_policy import HybridAdaptivePolicy
from app.services.adaptive.q_learning import QLearningPolicy
from app.services.adaptive.reward import compute_reward
from app.services.adaptive.state import LearnerState
from app.services.adaptive.statistical_prior import StatisticalPrior

N_STEPS = 100
OUT_DIR = Path(__file__).parent / "simulation_output"

PROFILES = ["struggling", "average", "fast_learning", "inconsistent"]


def _profile_ability(profile: str, step: int) -> float:
    """The simulated learner's TRUE (hidden) probability of answering
    correctly at MEDIUM difficulty at this step. This drives the simulated
    learner's answers only — it is never visible to the RL/statistical
    model, exactly like a real learner's true ability is never visible."""
    if profile == "struggling":
        return min(0.55, 0.25 + 0.003 * step)
    if profile == "average":
        return min(0.80, 0.45 + 0.004 * step)
    if profile == "fast_learning":
        return min(0.95, 0.35 + 0.008 * step)
    if profile == "inconsistent":
        return max(0.15, min(0.85, 0.50 + 0.30 * math.sin(step / 4.0)))
    raise ValueError(profile)


def run_profile(profile: str, seed: int) -> dict:
    rng = random.Random(seed)
    state = LearnerState(user_id=f"sim-{profile}", concept_id="c1")
    prior = StatisticalPrior()
    hybrid = HybridAdaptivePolicy()
    q_policy = QLearningPolicy(seed=seed)

    initial_p_stat = prior.compute(state)

    log = []
    for step in range(N_STEPS):
        discrete = discretize(state)
        p_stat = prior.compute(state)
        p_rl = q_policy.action_probabilities(discrete.key)
        w = hybrid.blend_weight(state.confidence, hybrid.progress(state.timestep))
        pi = hybrid.blend(p_stat, p_rl, w)
        action = q_policy.sample(pi)

        base_p = _profile_ability(profile, step)
        p_correct = max(0.05, min(0.97, base_p - 0.15 * ACTION_RANK[action]))
        correct = rng.random() < p_correct
        response_time = max(1.0, rng.gauss(12 - 4 * base_p, 4))

        state_key_before = discrete.key
        q_before = q_policy.q(state_key_before, action)
        state.update(correct=correct, response_time_sec=response_time, action_taken=action)
        reward = compute_reward(correct=correct, speed_score=state.normalized_response_time,
                                 action_taken=action, streak_momentum_after=state.streak_momentum)
        next_discrete = discretize(state)
        _, q_after = q_policy.update(state_key_before, action, reward.total, next_discrete.key)
        q_policy.decay_epsilon()

        log.append({
            "step": step, "action": action.value, "correct": correct,
            "response_time": round(response_time, 2),
            "skill": round(state.skill, 4), "recent_accuracy": round(state.recent_accuracy, 4),
            "confidence": round(state.confidence, 4), "blend_weight": round(w, 4),
            "p_stat": {a.value: round(v, 4) for a, v in p_stat.items()},
            "p_rl": {a.value: round(v, 4) for a, v in p_rl.items()},
            "hybrid_policy": {a.value: round(v, 4) for a, v in pi.items()},
            "reward": round(reward.total, 4), "reward_components": reward.as_dict(),
            "q_before": round(q_before, 4), "q_after": round(q_after, 4),
        })

    return {
        "profile": profile,
        "initial_policy": {a.value: round(v, 4) for a, v in initial_p_stat.items()},
        "final_state": {
            "skill": round(state.skill, 4), "recent_accuracy": round(state.recent_accuracy, 4),
            "confidence": round(state.confidence, 4),
            "learning_velocity": round(state.learning_velocity, 4),
            "streak_momentum": round(state.streak_momentum, 4),
        },
        "final_q_table": q_policy.q_table,
        "final_epsilon": round(q_policy.epsilon, 4),
        "log": log,
    }


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    summary_rows = []

    print(f"Simulating {len(PROFILES)} learner profiles x {N_STEPS} questions each...\n")
    for i, profile in enumerate(PROFILES):
        result = run_profile(profile, seed=100 + i)
        out_path = OUT_DIR / f"{profile}.json"
        out_path.write_text(json.dumps(result, indent=2))

        fs = result["final_state"]
        print(f"[{profile:14s}] initial_policy={result['initial_policy']}  "
              f"final_skill={fs['skill']:.3f}  final_accuracy={fs['recent_accuracy']:.3f}  "
              f"final_confidence={fs['confidence']:.3f}  final_epsilon={result['final_epsilon']:.3f}")

        distinct_actions = {row["action"] for row in result["log"]}
        print(f"                  actions used={sorted(distinct_actions)}  "
              f"q_table_size={len(result['final_q_table'])} states\n")

        for row in result["log"]:
            summary_rows.append({
                "profile": profile,
                "step": row["step"], "action": row["action"], "correct": row["correct"],
                "response_time": row["response_time"], "skill": row["skill"],
                "recent_accuracy": row["recent_accuracy"], "confidence": row["confidence"],
                "blend_weight": row["blend_weight"], "reward": row["reward"],
                "q_before": row["q_before"], "q_after": row["q_after"],
                "p_stat": json.dumps(row["p_stat"]), "p_rl": json.dumps(row["p_rl"]),
                "hybrid_policy": json.dumps(row["hybrid_policy"]),
                "reward_components": json.dumps(row["reward_components"]),
            })

    csv_path = OUT_DIR / "summary.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"Wrote {csv_path}")
    print(f"Wrote per-profile JSON logs to {OUT_DIR}/")


if __name__ == "__main__":
    main()
