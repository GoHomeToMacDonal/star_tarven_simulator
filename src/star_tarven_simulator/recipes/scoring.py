"""Deterministic structural scoring for terminal recipe candidates."""

from __future__ import annotations

from .templates import ScoreBreakdown


def score_recipe(*, energy: int, cost: int, slots_used: int, has_artanis: bool,
                 teleport_feedback: bool, unmet: int) -> ScoreBreakdown:
    """Score graph facts, not economy or combat simulation.

    The values are deliberately interpretable and bounded enough for a later
    positive GFlowNet terminal reward (`exp(total / temperature)`).
    """
    tiers = min(energy // cost, 2) if cost else 0
    threshold_completion = min(1.0, energy / cost) if cost else 1.0
    payoff = min(1.0, (tiers + int(has_artanis) * 0.5) / 2.0)
    slot_efficiency = max(0.0, 1.0 - (slots_used - 1) / 7.0)
    stability = 0.9 if energy >= cost else 0.25
    if teleport_feedback:
        stability = min(1.0, stability + 0.1)
    conflict_penalty = min(0.5, 0.25 * unmet)
    total = max(0.0, 0.45 * payoff + 0.25 * threshold_completion + 0.15 * slot_efficiency + 0.15 * stability - conflict_penalty)
    return ScoreBreakdown(
        total=round(total, 6), payoff=round(payoff, 6), threshold_completion=round(threshold_completion, 6),
        slot_efficiency=round(slot_efficiency, 6), stability=round(stability, 6), conflict_penalty=round(conflict_penalty, 6),
    )


__all__ = ["score_recipe"]
