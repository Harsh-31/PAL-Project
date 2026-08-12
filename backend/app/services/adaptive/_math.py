"""Tiny numerically-stable math helpers shared across the adaptive package."""
from __future__ import annotations
import math


def sigmoid(x: float) -> float:
    """Numerically stable logistic sigmoid."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p: float, eps: float = 1e-4) -> float:
    """Inverse sigmoid, clamped away from 0/1 to stay finite."""
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
