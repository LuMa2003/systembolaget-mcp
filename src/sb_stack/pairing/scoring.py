"""Structured re-rank over embedding candidates.

The embedding query alone rewards products whose *name* echoes the dish
("oxfilé" → a lager literally named after oxfilé; "Fishshot" for anything
fishy). Since we cannot re-embed, we pull a larger candidate set and re-score
each one on signals that actually reflect pairing fitness:

  * usage_similarity  — the raw cosine (the sommelier `usage` text is part of
                        the embedded blob, so this still carries real signal)
  * symbol_match      — overlap with the dish's inferred taste_symbols
  * taste_clock_fit   — closeness of the product body to the dish body target
  * category prior    — penalise spirits / gin / liqueur / alcohol-free for
                        ordinary food unless the dish calls for them
  * name-overlap guard — a product may not win purely because its name
                        contains a dish word

The combined `total` drives ranking; the cosine float is never surfaced to
the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sb_stack.pairing.profile import DishProfile, _normalize

if TYPE_CHECKING:
    from sb_stack.mcp_server.responses import Product

# Categories that are usually wrong for ordinary food unless the dish asks.
_PENALIZED_CAT1: frozenset[str] = frozenset({"Sprit", "Alkoholfritt"})
_PENALIZED_CAT2: frozenset[str] = frozenset(
    {"Gin & Genever", "Likör", "Smaksatt sprit", "Bitter", "Aperitif & Bitter"}
)

# Score weights. usage cosine and structured symbol fit dominate; the category
# prior and clock fit shape the tail.
_W_USAGE = 0.45
_W_SYMBOL = 0.30
_W_CLOCK = 0.15
_W_CATEGORY = 0.10

# A usage cosine at/above this counts as a "strong" match for confidence.
STRONG_USAGE = 0.45


@dataclass
class Candidate:
    product: Product
    usage_text: str | None
    similarity: float  # raw cosine, 0..1
    # Filled by scoring:
    symbol_match: float = 0.0
    taste_clock_fit: float = 0.0
    category_prior: float = 0.0
    name_overlap: bool = False
    total: float = 0.0


def _symbol_match(product: Product, profile: DishProfile) -> float:
    if not profile.symbols:
        return 0.0
    have = set(product.taste_symbols)
    hits = sum(1 for s in profile.symbols if s in have)
    return hits / len(profile.symbols)


def _clock_fit(product: Product, profile: DishProfile) -> float:
    if profile.body_target is None:
        return 0.5  # neutral: dish gives no body steer
    body = product.taste_clocks.body
    if body is None:
        return 0.4  # mild penalty for missing data
    # 0..12 scale; full credit within 1 step, linear falloff to 0 at 6 steps.
    dist = abs(body - profile.body_target)
    return max(0.0, 1.0 - dist / 6.0)


def _category_prior(product: Product, profile: DishProfile) -> float:
    cat1 = product.category_level_1 or ""
    cat2 = product.category_level_2 or ""
    if cat1 in _PENALIZED_CAT1 or cat2 in _PENALIZED_CAT2:
        # Dessert / aperitif contexts legitimately want spirits & liqueurs.
        if profile.spirit_friendly:
            return 0.6
        return 0.0
    return 1.0


def _name_contains_dish_word(product: Product, profile: DishProfile) -> bool:
    """True if the product NAME echoes a dish word — the lexical trap."""
    if not profile.dish_words:
        return False
    name = _normalize(f"{product.name_bold} {product.name_thin or ''}")
    name_words = set(name.split())
    return bool(profile.dish_words & name_words)


def score_candidates(candidates: list[Candidate], profile: DishProfile) -> list[Candidate]:
    """Score and sort candidates in place; returns the same list, ranked."""
    for c in candidates:
        c.symbol_match = _symbol_match(c.product, profile)
        c.taste_clock_fit = _clock_fit(c.product, profile)
        c.category_prior = _category_prior(c.product, profile)
        c.name_overlap = _name_contains_dish_word(c.product, profile)

        total = (
            _W_USAGE * c.similarity
            + _W_SYMBOL * c.symbol_match
            + _W_CLOCK * c.taste_clock_fit
            + _W_CATEGORY * c.category_prior
        )
        # Name-overlap guard: a name echo with no structured pairing support
        # (no symbol agreement) must not let the product win on lexical luck.
        if c.name_overlap and c.symbol_match == 0.0:
            total *= 0.5
        c.total = total

    candidates.sort(key=lambda c: c.total, reverse=True)
    return candidates


def diversify(
    candidates: list[Candidate], *, limit: int, relevance_floor: float
) -> list[Candidate]:
    """At most one per category_level_2, but only above a relevance floor.

    The floor stops a cheap off-category lager from being injected ahead of
    relevant wines purely to vary the category mix.
    """
    relevant = [c for c in candidates if c.total >= relevance_floor]
    pool = relevant or candidates  # never return empty if we have anything

    picked: list[Candidate] = []
    seen: set[str] = set()
    for c in pool:
        bucket = (c.product.category_level_2 or c.product.category_level_1 or "other").lower()
        if bucket in seen:
            continue
        picked.append(c)
        seen.add(bucket)
        if len(picked) >= limit:
            break
    if len(picked) < limit:
        for c in pool:
            if c in picked:
                continue
            picked.append(c)
            if len(picked) >= limit:
                break
    return picked


def compute_confidence(scored: list[Candidate], profile: DishProfile) -> str:
    """Confidence from real signals, not raw cosine magnitude.

    Counts candidates that both clear the usage-similarity bar AND match an
    inferred taste_symbol. No inferred signal (nonsense dish) ⇒ low.
    """
    if not profile.has_signal:
        return "low"

    strong = [c for c in scored if c.similarity >= STRONG_USAGE and c.symbol_match > 0.0]
    if profile.symbols and len(strong) >= 5:
        return "high"
    if strong or (profile.symbols and any(c.symbol_match > 0.0 for c in scored)):
        return "medium"
    return "low"
