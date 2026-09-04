"""Threshold RL — learns per-learner cognitive-state boundaries.

Separate from the Hybrid RL difficulty engine (app.services.adaptive).
Same Q-learning pattern, different action space: adjusts tau_struggling
and tau_mastered on the MASTERS edge every 5 interactions.
"""
