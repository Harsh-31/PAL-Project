"""AdaptiveDifficultyController — the public integration point for the rest
of PAL.

Wires together: LearnerState -> StatisticalPrior + QLearningPolicy ->
HybridAdaptivePolicy -> Difficulty action -> (after the learner answers)
reward computation -> state update -> Q-learning update -> persistence ->
explainability log.

The Process KG (`app.services.kg_service`) is intentionally NOT called from
here — it stays a separate, deterministic pedagogical layer. `app.services
.pal_agent` is what composes this controller's difficulty decision with the
KG's mastery/intervention decision (see module docstring there).
"""
from __future__ import annotations
import statistics
from dataclasses import dataclass, field

from .actions import ACTIONS, Difficulty, action_to_legacy_int, legacy_int_to_action
from .config import AdaptiveConfig, DEFAULT_CONFIG
from .discretization import discretize
from .hybrid_policy import HybridAdaptivePolicy
from .q_learning import QLearningPolicy
from .reward import RewardComponents, compute_reward
from .state import LearnerState
from .statistical_prior import StatisticalPrior
from . import persistence

_DEFAULT_RESPONSE_TIME_SEC = 15.0  # neutral fallback when the client sends no timing signal


def _probs_to_dict(probs: dict[Difficulty, float]) -> dict:
    return {a.value: round(v, 6) for a, v in probs.items()}


@dataclass
class DifficultyDecision:
    """Everything about one difficulty-selection decision — enough to both
    drive the UI (legacy_difficulty) and reconstruct/explain it later."""
    action: Difficulty
    legacy_difficulty: int
    state: LearnerState
    discrete_state_key: str
    p_stat: dict
    p_rl: dict
    blend_weight: float
    hybrid_policy: dict
    q_values: dict

    def to_pending(self) -> dict:
        """Snapshot stored alongside the generated question so process_outcome
        can later be told exactly which state/action/Q-row this decision used."""
        return {
            "action": self.action.value,
            "discrete_state_key": self.discrete_state_key,
            "p_stat": self.p_stat,
            "p_rl": self.p_rl,
            "blend_weight": self.blend_weight,
            "hybrid_policy": self.hybrid_policy,
            "q_values": self.q_values,
            "state_snapshot": self.state.to_dict(),
        }


class AdaptiveDifficultyController:
    """Behind-the-scenes abstraction: composes StatisticalPrior + QLearningPolicy
    into a HybridAdaptivePolicy and exposes a simple select/observe API."""

    def __init__(self, config: AdaptiveConfig = DEFAULT_CONFIG):
        self.config = config
        self.stat_prior = StatisticalPrior(config)
        self.hybrid = HybridAdaptivePolicy(config)

    async def select_difficulty(self, user_id: str, concept_id: str,
                                 baseline_skill: float | None = None) -> DifficultyDecision:
        state = await persistence.load_learner_state(user_id, concept_id)
        if state.timestep == 0 and baseline_skill is not None:
            # Cold start: blend the fresh-state skill prior with the onboarding
            # baseline / Process-KG mastery BEFORE computing any distribution,
            # so the very first question isn't a blind 0.5 guess.
            state.skill = (state.skill + baseline_skill) / 2.0

        q_table = await persistence.load_q_table()
        epsilon = await persistence.load_epsilon(self.config)
        # Seed keyed by timestep so repeated calls against the SAME persisted
        # state/table (e.g. a preview call right after an update) are
        # reproducible rather than re-rolling the dice.
        q_policy = QLearningPolicy(self.config, q_table=q_table, epsilon=epsilon,
                                    seed=self.config.q.seed + state.timestep)

        discrete = discretize(state, self.config)
        p_stat = self.stat_prior.compute(state)
        p_rl = q_policy.action_probabilities(discrete.key)
        w = self.hybrid.blend_weight(state.confidence, self.hybrid.progress(state.timestep))
        pi = self.hybrid.blend(p_stat, p_rl, w)
        action = q_policy.sample(pi)

        return DifficultyDecision(
            action=action,
            legacy_difficulty=action_to_legacy_int(action),
            state=state,
            discrete_state_key=discrete.key,
            p_stat=_probs_to_dict(p_stat),
            p_rl=_probs_to_dict(p_rl),
            blend_weight=w,
            hybrid_policy=_probs_to_dict(pi),
            q_values={a.value: q_policy.q(discrete.key, a) for a in ACTIONS},
        )

    async def process_outcome(self, *, user_id: str, concept_id: str, question_id: str,
                               correct: bool, response_time_sec: float | None,
                               pending: dict | None, current_difficulty: int | None = None) -> dict:
        """`pending` is the DifficultyDecision.to_pending() snapshot captured at
        select_difficulty time — it tells us which state/action/Q-row the
        outcome belongs to. If missing (e.g. a question generated before this
        engine existed), we reconstruct a best-effort pending record from the
        currently persisted state so the loop still runs correctly."""
        if not pending or "state_snapshot" not in pending:
            pending = await self._fallback_pending(user_id, concept_id, current_difficulty)

        action = Difficulty(pending["action"])
        state = LearnerState.from_dict(pending["state_snapshot"])
        pre_state_key = pending["discrete_state_key"]
        q_before_snapshot = pending["q_values"].get(action.value, 0.0)

        if response_time_sec is None:
            response_time_sec = (
                statistics.mean(state.response_time_history)
                if state.response_time_history else _DEFAULT_RESPONSE_TIME_SEC
            )

        state.update(correct=correct, response_time_sec=response_time_sec,
                     action_taken=action, config=self.config)

        reward = compute_reward(
            correct=correct,
            speed_score=state.normalized_response_time,
            action_taken=action,
            streak_momentum_after=state.streak_momentum,
            config=self.config.reward,
        )

        q_table = await persistence.load_q_table()
        epsilon = await persistence.load_epsilon(self.config)
        q_policy = QLearningPolicy(self.config, q_table=q_table, epsilon=epsilon)
        next_discrete = discretize(state, self.config)
        q_before, q_after = q_policy.update(pre_state_key, action, reward.total, next_discrete.key)
        q_policy.decay_epsilon()

        await persistence.save_learner_state(state)
        await persistence.save_q_row(pre_state_key, q_policy.q_table[pre_state_key])
        await persistence.save_epsilon(q_policy.epsilon)

        session_id = f"{user_id}:{concept_id}"
        log_entry = {
            "learner_id": user_id,
            "session_id": session_id,
            "timestep": state.timestep,
            "question_id": question_id,
            "learner_state": state.to_dict(),
            "discretized_state": pre_state_key,
            "p_stat": pending.get("p_stat"),
            "p_rl": pending.get("p_rl"),
            "blend_weight": pending.get("blend_weight"),
            "hybrid_policy": pending.get("hybrid_policy"),
            "selected_action": action.value,
            "reward": reward.total,
            "reward_components": reward.as_dict(),
            "q_value_before": q_before,
            "q_value_after": q_after,
        }
        await persistence.log_decision(log_entry)

        return {
            "state": state,
            "reward": reward,
            "q_value_before": q_before,
            "q_value_after": q_after,
            "next_discrete_state": next_discrete.key,
            "log_entry": log_entry,
        }

    async def _fallback_pending(self, user_id: str, concept_id: str,
                                 current_difficulty: int | None) -> dict:
        state = await persistence.load_learner_state(user_id, concept_id)
        discrete = discretize(state, self.config)
        action = legacy_int_to_action(current_difficulty) if current_difficulty else Difficulty.MEDIUM
        return {
            "action": action.value,
            "discrete_state_key": discrete.key,
            "p_stat": {}, "p_rl": {}, "blend_weight": 0.0, "hybrid_policy": {},
            "q_values": {}, "state_snapshot": state.to_dict(),
        }
