"""Hybrid Reinforcement Learning adaptive-difficulty engine (AAAI-26 PAL paper).

Composes a 2PL IRT-style statistical prior with a tabular Q-learning policy
into a single hybrid policy pi_t(d|x_t) that selects the difficulty of the
next question. See ``controller.AdaptiveDifficultyController`` for the public
entry point, and the README "Hybrid Reinforcement Learning Adaptation"
section for the full architecture and equations.

This package is intentionally free of any Process-KG / LLM concerns — those
remain in ``app.services.kg_service`` and ``app.services.ollama_service``.
``app.services.pal_agent`` is what composes all three together.
"""
from .actions import ACTIONS, Difficulty
from .config import AdaptiveConfig, DEFAULT_CONFIG
from .controller import AdaptiveDifficultyController, DifficultyDecision

__all__ = [
    "ACTIONS",
    "Difficulty",
    "AdaptiveConfig",
    "DEFAULT_CONFIG",
    "AdaptiveDifficultyController",
    "DifficultyDecision",
]
