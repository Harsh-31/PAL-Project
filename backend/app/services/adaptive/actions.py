"""The three-level difficulty action space: a_t in {Easy, Medium, Hard}."""
from __future__ import annotations
from enum import Enum


class Difficulty(str, Enum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


ACTIONS: list[Difficulty] = [Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD]
ACTION_RANK: dict[Difficulty, int] = {Difficulty.EASY: 0, Difficulty.MEDIUM: 1, Difficulty.HARD: 2}

# The pre-existing app (Ollama prompts, the frontend "difficulty X/5" widget,
# the baseline->difficulty mapping) speaks a legacy 1-5 int ladder. We keep
# that wire format for backward compatibility and translate to/from the
# 3-action RL space only at this boundary — everything inside `adaptive/`
# works exclusively with Difficulty.
_ACTION_TO_LEGACY_INT: dict[Difficulty, int] = {
    Difficulty.EASY: 2, Difficulty.MEDIUM: 3, Difficulty.HARD: 4,
}
_LEGACY_INT_BOUNDARIES: tuple[tuple[int, Difficulty], ...] = (
    (2, Difficulty.EASY), (3, Difficulty.MEDIUM), (5, Difficulty.HARD),
)


def action_to_legacy_int(action: Difficulty) -> int:
    return _ACTION_TO_LEGACY_INT[action]


def legacy_int_to_action(value: int) -> Difficulty:
    for upper, action in _LEGACY_INT_BOUNDARIES:
        if value <= upper:
            return action
    return Difficulty.HARD


def next_harder(action: Difficulty) -> Difficulty:
    idx = min(ACTION_RANK[action] + 1, len(ACTIONS) - 1)
    return ACTIONS[idx]


def next_easier(action: Difficulty) -> Difficulty:
    idx = max(ACTION_RANK[action] - 1, 0)
    return ACTIONS[idx]
