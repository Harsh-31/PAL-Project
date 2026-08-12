"""Hybrid policy blending tests: pi_t = (1-w)p_stat + w*p_RL and the
confidence/progress-driven blend weight w_t."""
from app.services.adaptive.actions import ACTIONS, Difficulty
from app.services.adaptive.config import DEFAULT_CONFIG
from app.services.adaptive.hybrid_policy import HybridAdaptivePolicy


def _dist(easy, medium, hard):
    return {Difficulty.EASY: easy, Difficulty.MEDIUM: medium, Difficulty.HARD: hard}


def test_blend_with_w_zero_equals_statistical_prior():
    h = HybridAdaptivePolicy()
    p_stat = _dist(0.6, 0.3, 0.1)
    p_rl = _dist(0.1, 0.2, 0.7)
    blended = h.blend(p_stat, p_rl, 0.0)
    for a in ACTIONS:
        assert abs(blended[a] - p_stat[a]) < 1e-9


def test_blend_with_w_one_equals_rl_policy():
    h = HybridAdaptivePolicy()
    p_stat = _dist(0.6, 0.3, 0.1)
    p_rl = _dist(0.1, 0.2, 0.7)
    blended = h.blend(p_stat, p_rl, 1.0)
    for a in ACTIONS:
        assert abs(blended[a] - p_rl[a]) < 1e-9


def test_blend_intermediate_w_is_correct_convex_combination():
    h = HybridAdaptivePolicy()
    p_stat = _dist(0.6, 0.3, 0.1)
    p_rl = _dist(0.1, 0.2, 0.7)
    w = 0.4
    blended = h.blend(p_stat, p_rl, w)
    expected_easy = (1 - w) * 0.6 + w * 0.1
    expected_hard = (1 - w) * 0.1 + w * 0.7
    assert abs(blended[Difficulty.EASY] - expected_easy) < 1e-9
    assert abs(blended[Difficulty.HARD] - expected_hard) < 1e-9
    assert abs(sum(blended.values()) - 1.0) < 1e-9


def test_blend_weight_increases_with_confidence_and_progress_up_to_wmax():
    h = HybridAdaptivePolicy()
    w_low = h.blend_weight(confidence=0.0, progress=0.0)
    w_mid = h.blend_weight(confidence=0.5, progress=0.5)
    w_high = h.blend_weight(confidence=1.0, progress=1.0)
    assert w_low == DEFAULT_CONFIG.hybrid.w0
    assert w_low < w_mid < w_high
    assert w_high <= DEFAULT_CONFIG.hybrid.w_max


def test_blend_weight_capped_at_w_max():
    h = HybridAdaptivePolicy()
    w = h.blend_weight(confidence=1.0, progress=1.0)
    assert w <= DEFAULT_CONFIG.hybrid.w_max


def test_early_session_favors_statistical_prior():
    """Early on (low confidence, low progress -> low w), the hybrid policy
    should sit close to p_stat rather than p_rl."""
    h = HybridAdaptivePolicy()
    p_stat = _dist(0.7, 0.2, 0.1)
    p_rl = _dist(0.05, 0.05, 0.9)
    w = h.blend_weight(confidence=0.05, progress=0.05)
    blended = h.blend(p_stat, p_rl, w)
    assert blended[Difficulty.EASY] > blended[Difficulty.HARD]
