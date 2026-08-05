"""PAL-Agent — the neuro-symbolic decision engine.

Implements the micro-loop from the architecture diagram:
  Observe -> Update Beliefs -> Predict -> Assess -> Counterfactual
    -> Select Action -> Reflect -> Update Memory

The output of `decide_after_attempt` is the concrete action the API layer
takes (e.g. raise difficulty, insert remedial video, offer analogy).
"""
from datetime import datetime, timezone
from app.services import kg_service


# Difficulty ladder: 1 (very easy) .. 5 (very hard)
_DIFF_MIN, _DIFF_MAX = 1, 5


def _next_difficulty(current: int, correct: bool, mastery: float) -> int:
    """Adaptive rule — bounded, symmetric-ish."""
    if correct and mastery > 0.8:
        return min(current + 1, _DIFF_MAX)
    if not correct and mastery < 0.4:
        return max(current - 1, _DIFF_MIN)
    return current


async def decide_initial_difficulty(user_id: str, concept_id: str, baseline: str) -> int:
    """Cold-start difficulty from baseline; warms up as attempts land."""
    mastery = await kg_service.get_mastery(user_id, concept_id)
    base_map = {"beginner": 2, "intermediate": 3, "advanced": 4}
    base = base_map.get(baseline, 3)
    # Nudge toward observed mastery
    if mastery > 0.75:
        base = min(base + 1, _DIFF_MAX)
    elif mastery < 0.35:
        base = max(base - 1, _DIFF_MIN)
    return base


async def decide_after_attempt(
    user_id: str,
    concept_id: str,
    correct: bool,
    current_difficulty: int,
) -> dict:
    """Full micro-loop after a quiz attempt.

    Returns a structured trace (Process KG friendly) plus the next action.
    """
    # 1. Observe (implicit — inputs)
    # 2. Update Beliefs — write to KG
    delta = 0.12 if correct else -0.08
    new_mastery = await kg_service.record_mastery(user_id, concept_id, delta)

    # 3. Predict — projected mastery if learner continues at this rate
    predicted = min(1.0, new_mastery + (0.05 if correct else -0.03))

    # 4. Assess reliability — few attempts = lower confidence
    attempts = await kg_service.get_attempts(user_id, concept_id)
    confidence = min(1.0, attempts / 5.0)

    # 5. Counterfactual — what if we'd asked an easier question?
    counterfactual = {
        "if_easier": "learner likely correct; reveals less signal",
        "if_harder": "learner likely wrong; risks frustration",
    }

    # 6. Select Action — consult Process KG (deterministic anchor)
    rule = await kg_service.get_intervention(new_mastery)
    next_diff = _next_difficulty(current_difficulty, correct, new_mastery)

    # 7. Reflect — compare predicted vs actual
    reflection = (
        "Prediction matched outcome." if correct == (predicted >= 0.5)
        else "Prediction diverged from outcome; recalibrating."
    )

    # 8. Memory update happens implicitly via record_mastery + interaction log
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "beliefs": {"mastery": new_mastery, "predicted": predicted},
        "confidence": confidence,
        "counterfactual": counterfactual,
        "intervention": rule or {"state": "OnTrack", "rule": "ContinueBaseline",
                                 "action": "continue_normal"},
        "next_difficulty": next_diff,
        "reflection": reflection,
    }
