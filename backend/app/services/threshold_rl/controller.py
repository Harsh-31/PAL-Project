"""ThresholdController — the public API for the Threshold RL.

Called by the orchestrator (pal_agent.py) every interaction.  Internally
it buffers interactions and only runs the Q-learning update + threshold
adjustment every ``update_every_n`` interactions (default 5) to prevent
oscillation.

The 6-d learner state used for discretization is read from the MASTERS
edge (synced there by the orchestrator after the Hybrid RL update).
When the mirrored state is not yet available (cold start), falls back to
a neutral bucket.
"""
from __future__ import annotations
from app.services.adaptive.config import DEFAULT_CONFIG as ADAPTIVE_CONFIG
from app.services.adaptive.discretization import DiscreteState, _bucket
from .actions import ThresholdAction
from .config import ThresholdRLConfig, DEFAULT_THRESHOLD_CONFIG
from .q_learning import ThresholdQLearning
from .reward import compute_threshold_reward
from . import persistence


def discretize_from_rl_state(rl_state: dict | None) -> str:
    """Build a discrete state key from the RL state mirrored on the MASTERS edge."""
    if not rl_state or rl_state.get("rl_skill") is None:
        return "sk1-ac1-rt1-st1-vl1-cf1"
    b = ADAPTIVE_CONFIG.buckets
    ds = DiscreteState(
        skill=_bucket(rl_state.get("rl_skill", 0.5), b.skill),
        accuracy=_bucket(rl_state.get("rl_recent_accuracy", 0.5), b.accuracy),
        response_time=_bucket(rl_state.get("rl_normalized_response_time", 0.5), b.response_time),
        streak=_bucket(rl_state.get("rl_streak_momentum", 0.0), b.streak),
        velocity=_bucket(rl_state.get("rl_learning_velocity", 0.0), b.velocity),
        confidence=_bucket(rl_state.get("rl_confidence", 0.5), b.confidence),
    )
    return ds.key


def _clamp_thresholds(
    tau_s: float, tau_m: float, cfg: ThresholdRLConfig
) -> tuple[float, float]:
    """Enforce hard constraints on threshold values."""
    bounds = cfg.bounds
    tau_s = max(bounds.tau_struggling_min, min(bounds.tau_struggling_max, tau_s))
    tau_m = max(bounds.tau_mastered_min, min(bounds.tau_mastered_max, tau_m))
    if tau_m - tau_s < bounds.min_gap:
        mid = (tau_s + tau_m) / 2.0
        tau_s = mid - bounds.min_gap / 2.0
        tau_m = mid + bounds.min_gap / 2.0
        tau_s = max(bounds.tau_struggling_min, min(bounds.tau_struggling_max, tau_s))
        tau_m = max(bounds.tau_mastered_min, min(bounds.tau_mastered_max, tau_m))
    return round(tau_s, 4), round(tau_m, 4)


class ThresholdController:
    def __init__(self, config: ThresholdRLConfig = DEFAULT_THRESHOLD_CONFIG):
        self.config = config

    async def record_interaction(
        self,
        *,
        user_id: str,
        concept_id: str,
        correct: bool,
        mastery: float,
        accuracy: float,
        cognitive_state: str,
        state_changed: bool,
    ) -> dict | None:
        """Called every interaction.  Buffers outcomes and returns a threshold
        update dict (with new tau values and reward info) every N interactions,
        or None if no update is due yet.
        """
        buf = await persistence.load_interaction_buffer(user_id, concept_id) or {
            "interactions": [],
            "mastery_start": mastery,
            "accuracy_start": accuracy,
            "interventions_triggered": 0,
            "interventions_without_improvement": 0,
        }

        buf["interactions"].append({
            "correct": correct,
            "mastery": mastery,
            "accuracy": accuracy,
            "cognitive_state": cognitive_state,
            "state_changed": state_changed,
        })

        if state_changed and cognitive_state == "Struggling":
            buf["interventions_triggered"] = buf.get("interventions_triggered", 0) + 1

        n = self.config.update.update_every_n
        if len(buf["interactions"]) < n:
            await persistence.save_interaction_buffer(user_id, concept_id, buf)
            return None

        result = await self._run_update(user_id, concept_id, buf)

        # Reset buffer.
        await persistence.save_interaction_buffer(user_id, concept_id, {
            "interactions": [],
            "mastery_start": mastery,
            "accuracy_start": accuracy,
            "interventions_triggered": 0,
            "interventions_without_improvement": 0,
        })
        return result

    async def _run_update(self, user_id: str, concept_id: str, buf: dict) -> dict:
        from app.services import kg_service

        interactions = buf["interactions"]
        mastery_before = buf["mastery_start"]
        accuracy_before = buf["accuracy_start"]
        mastery_after = interactions[-1]["mastery"]
        accuracy_after = interactions[-1]["accuracy"]

        improvements_after_intervention = 0
        last_intervention_idx = None
        for i, ix in enumerate(interactions):
            if ix.get("state_changed") and ix["cognitive_state"] == "Struggling":
                last_intervention_idx = i
            elif last_intervention_idx is not None and ix["correct"]:
                improvements_after_intervention += 1
                last_intervention_idx = None

        interventions_triggered = buf.get("interventions_triggered", 0)
        interventions_without_improvement = max(
            0, interventions_triggered - improvements_after_intervention
        )

        reward = compute_threshold_reward(
            mastery_before=mastery_before,
            mastery_after=mastery_after,
            accuracy_before=accuracy_before,
            accuracy_after=accuracy_after,
            interventions_triggered=interventions_triggered,
            interventions_without_improvement=interventions_without_improvement,
            window_size=len(interactions),
            config=self.config.reward,
        )

        thresholds = await kg_service.get_thresholds(user_id, concept_id)
        tau_s = thresholds.get("tau_struggling") or self.config.bounds.tau_struggling_default
        tau_m = thresholds.get("tau_mastered") or self.config.bounds.tau_mastered_default
        tau_timestep = thresholds.get("tau_timestep", 0)

        rl_state = await kg_service.get_rl_state_from_edge(user_id, concept_id)
        state_key = discretize_from_rl_state(rl_state)

        q_struggling, q_mastered = await persistence.load_q_tables()
        epsilon = await persistence.load_epsilon(self.config)
        q_policy = ThresholdQLearning(
            self.config,
            q_table_struggling=q_struggling,
            q_table_mastered=q_mastered,
            epsilon=epsilon,
            seed=self.config.q.seed + tau_timestep,
        )

        action_s = q_policy.select_epsilon_greedy("struggling", state_key)
        action_m = q_policy.select_epsilon_greedy("mastered", state_key)

        delta = self.config.q.step_delta
        new_tau_s = tau_s + {
            ThresholdAction.DECREASE: -delta,
            ThresholdAction.KEEP: 0.0,
            ThresholdAction.INCREASE: delta,
        }[action_s]
        new_tau_m = tau_m + {
            ThresholdAction.DECREASE: -delta,
            ThresholdAction.KEEP: 0.0,
            ThresholdAction.INCREASE: delta,
        }[action_m]

        new_tau_s, new_tau_m = _clamp_thresholds(new_tau_s, new_tau_m, self.config)

        next_state_key = state_key
        q_s_before, q_s_after = q_policy.update(
            "struggling", state_key, action_s, reward.total, next_state_key
        )
        q_m_before, q_m_after = q_policy.update(
            "mastered", state_key, action_m, reward.total, next_state_key
        )
        q_policy.decay_epsilon()

        await persistence.save_q_tables(q_policy.q_struggling, q_policy.q_mastered)
        await persistence.save_epsilon(q_policy.epsilon)

        await kg_service.update_thresholds(
            user_id, concept_id, new_tau_s, new_tau_m, tau_timestep + 1
        )

        # Deterministic decision_ref links every quiz interaction back to
        # this exact threshold decision — the key for end-to-end traceability.
        decision_ref = f"threshold:{user_id}:{concept_id}:{tau_timestep + 1}"

        log_entry = {
            "user_id": user_id,
            "concept_id": concept_id,
            "tau_timestep": tau_timestep + 1,
            "decision_ref": decision_ref,
            "state_key": state_key,
            "mastery_before": mastery_before,
            "mastery_after": mastery_after,
            "accuracy_before": accuracy_before,
            "accuracy_after": accuracy_after,
            "tau_struggling_before": tau_s,
            "tau_struggling_after": new_tau_s,
            "action_struggling": action_s.value,
            "tau_mastered_before": tau_m,
            "tau_mastered_after": new_tau_m,
            "action_mastered": action_m.value,
            "reward": reward.as_dict(),
            "q_struggling": {"before": q_s_before, "after": q_s_after},
            "q_mastered": {"before": q_m_before, "after": q_m_after},
        }
        await persistence.log_decision(log_entry, decision_ref=decision_ref)

        return {
            "tau_struggling": new_tau_s,
            "tau_mastered": new_tau_m,
            "tau_timestep": tau_timestep + 1,
            "decision_ref": decision_ref,
            "reward": reward,
            "actions": {"struggling": action_s.value, "mastered": action_m.value},
        }
