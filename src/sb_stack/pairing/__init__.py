"""Pairing engine — match a dish description to drink recommendations.

This scaffold is the semantic-retrieval core from DISH_PAIRING_DESIGN.md:
embed the dish text, rank candidates by vector similarity to the Swedish
`usage` / `taste` / `aroma` fields, optionally bias by taste_symbols,
and return a diverse top-N. The 8-axis scorer + cultural-pairings
layer from §7 of the design doc are deliberately left as follow-up
work — the tool contract is wired and unit-tested, so those layers can
slot in without MCP-surface changes.
"""

from sb_stack.pairing.engine import PairingEngine, Recommendation

__all__ = ["PairingEngine", "Recommendation"]
