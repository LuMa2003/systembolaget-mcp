"""Pairing engine — match a dish description to drink recommendations.

The engine cannot re-embed in this build, so it re-ranks the embedding
candidate set with a structured read of the dish: inferred taste_symbols + a
body target steer scoring over the sommelier `usage` field, a category prior
suppresses nonsensical picks, and Swedish rationale is grounded in the
product's own `usage` text. See `DISH_PAIRING_DESIGN.md` §7/§10 for the
scoring + confidence model this approximates without re-embedding.
"""

from sb_stack.pairing.engine import (
    Interpretation,
    PairingEngine,
    PairingResult,
    Recommendation,
    ScoreBreakdown,
)

__all__ = [
    "Interpretation",
    "PairingEngine",
    "PairingResult",
    "Recommendation",
    "ScoreBreakdown",
]
