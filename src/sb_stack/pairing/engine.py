"""Pairing engine — structured re-rank over embedding candidates.

We cannot re-embed the catalog (no embed-server in this build), so the engine
takes the embedding query result as a *candidate set* and re-ranks it with a
structured read of the dish (see `profile.py` + `scoring.py`):

  1. Infer the dish's taste_symbols + body target (Swedish keyword map).
  2. Pull a large candidate set (top ~50) by cosine over the embedded text,
     carrying each product's `usage` sentence along.
  3. Score every candidate on usage cosine, symbol match, taste-clock fit and
     a category-sensibility prior; guard against pure name-overlap wins.
  4. Diversify above a relevance floor; compute confidence from real signals.
  5. Build Swedish, user-facing rationale grounded in the `usage` text.

`taste_symbols_hint` and `budget_max` stay hard SQL filters. `meal_context`
is used for inference only — it is NOT concatenated into the embedding query
(which previously injected stray words like "vänner" into ranking).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sb_stack.db import DB
from sb_stack.embed import EmbeddingClient
from sb_stack.errors import EmbeddingError
from sb_stack.pairing.profile import DishProfile, infer_profile
from sb_stack.pairing.scoring import (
    Candidate,
    compute_confidence,
    diversify,
    score_candidates,
)
from sb_stack.pairing.voicing import (
    build_dish_summary,
    build_inferred_profile,
    build_sommelier_reasoning,
    build_why,
)
from sb_stack.settings import Settings

if TYPE_CHECKING:
    from sb_stack.mcp_server.responses import Product

# Pull this many candidates from the embedding query before re-ranking, so the
# structured score can promote relevant picks the raw cosine buried.
_CANDIDATE_POOL = 50
# Additionally inject up to this many closest products carrying an inferred
# taste_symbol — the raw cosine pool often misses the dish's tagged drinks.
_SYMBOL_INJECT_N = 40
# Minimum total score for a candidate to be eligible during diversification.
_RELEVANCE_FLOOR = 0.30


@dataclass
class ScoreBreakdown:
    usage_similarity: float
    taste_clock_fit: float
    symbol_match: float
    total: float


@dataclass
class Recommendation:
    product: Product
    similarity: float
    why: str
    breakdown: ScoreBreakdown


@dataclass
class Interpretation:
    dish_summary: str
    inferred_taste_symbols: list[str]
    inferred_profile: str
    sommelier_reasoning: str


@dataclass
class PairingResult:
    recommendations: list[Recommendation] = field(default_factory=list)
    confidence: str = "low"
    interpretation: Interpretation | None = None


class PairingEngine:
    """Take a dish, return structured drink recommendations."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: DB,
        embed_client: EmbeddingClient,
    ) -> None:
        self.settings = settings
        self.db = db
        self.embed_client = embed_client

    async def pair(
        self,
        *,
        dish: str,
        meal_context: str | None = None,
        taste_symbols_hint: list[str] | None = None,
        site_ids_in_stock: list[str] | None = None,
        budget_max: float | None = None,
        limit: int = 5,
    ) -> PairingResult:
        if not dish.strip():
            return PairingResult()

        profile = infer_profile(dish, meal_context, taste_symbols_hint)

        # Embed the dish only (not meal_context prose — that feeds inference).
        try:
            vectors = await self.embed_client.embed([dish.strip()])
        except EmbeddingError:
            return PairingResult(interpretation=self._interpretation(profile, "low"))
        if not vectors:
            return PairingResult(interpretation=self._interpretation(profile, "low"))
        q_vec = vectors[0]

        candidates = self._fetch_candidates(
            q_vec=q_vec,
            taste_symbols_hint=taste_symbols_hint,
            inferred_symbols=list(profile.symbols),
            site_ids_in_stock=site_ids_in_stock,
            budget_max=budget_max,
            fetch_n=max(_CANDIDATE_POOL, limit * 5),
        )
        if not candidates:
            return PairingResult(interpretation=self._interpretation(profile, "low"))

        scored = score_candidates(candidates, profile)
        confidence = compute_confidence(scored, profile)
        picked = diversify(scored, limit=limit, relevance_floor=_RELEVANCE_FLOOR)

        recs = [
            Recommendation(
                product=c.product,
                similarity=c.similarity,
                why=build_why(c, profile),
                breakdown=ScoreBreakdown(
                    usage_similarity=round(c.similarity, 4),
                    taste_clock_fit=round(c.taste_clock_fit, 4),
                    symbol_match=round(c.symbol_match, 4),
                    total=round(c.total, 4),
                ),
            )
            for c in picked
        ]
        return PairingResult(
            recommendations=recs,
            confidence=confidence,
            interpretation=self._interpretation(profile, confidence),
        )

    # ── Internals ───────────────────────────────────────────────────────

    @staticmethod
    def _interpretation(profile: DishProfile, confidence: str) -> Interpretation:
        return Interpretation(
            dish_summary=build_dish_summary(profile),
            inferred_taste_symbols=list(profile.symbols),
            inferred_profile=build_inferred_profile(profile),
            sommelier_reasoning=build_sommelier_reasoning(profile, confidence),
        )

    def _fetch_candidates(
        self,
        *,
        q_vec: list[float],
        taste_symbols_hint: list[str] | None,
        inferred_symbols: list[str],
        site_ids_in_stock: list[str] | None,
        budget_max: float | None,
        fetch_n: int,
    ) -> list[Candidate]:
        where: list[str] = [
            "(p.is_discontinued IS NULL OR p.is_discontinued = FALSE)",
        ]
        params: list[Any] = []
        if taste_symbols_hint:
            where.append("list_has_any(p.taste_symbols, ?::VARCHAR[])")
            params.append(taste_symbols_hint)
        if budget_max is not None:
            where.append("p.price_incl_vat <= ?")
            params.append(budget_max)
        if site_ids_in_stock:
            placeholders = ", ".join(["?"] * len(site_ids_in_stock))
            where.append(
                "p.product_number IN (SELECT product_number FROM stock "
                f"WHERE site_id IN ({placeholders}) AND stock > 0)"
            )
            params.extend(site_ids_in_stock)
        where_sql = " WHERE " + " AND ".join(where)

        # Deferred import: sb_stack.mcp_server imports from sb_stack.pairing
        # via the pair_with_dish tool, so we can't import product_rows at
        # module level (circular).
        from sb_stack.mcp_server.product_rows import rows_to_products  # noqa: PLC0415

        select_sql = f"""
            SELECT p.product_number,
                   array_cosine_distance(
                       e.embedding, ?::FLOAT[{self.settings.embed_dim}]
                   ) AS distance,
                   p.usage AS usage
              FROM products p
              JOIN product_embeddings e USING (product_number)
        """

        with self.db.reader() as conn:
            ranked = conn.execute(
                f"{select_sql} {where_sql} ORDER BY distance ASC LIMIT ?",
                [q_vec, *params, fetch_n],
            ).fetchall()

            # The raw cosine pool is polluted by name overlap and often misses
            # the products actually tagged for this dish. Inject the closest
            # products carrying an inferred taste_symbol so the re-rank has the
            # right material to promote (same cosine distance, real signal).
            if inferred_symbols:
                ranked += conn.execute(
                    f"{select_sql} {where_sql} "
                    "AND list_has_any(p.taste_symbols, ?::VARCHAR[]) "
                    "ORDER BY distance ASC LIMIT ?",
                    [q_vec, *params, inferred_symbols, _SYMBOL_INJECT_N],
                ).fetchall()

            if not ranked:
                return []
            distances: dict[str, float] = {}
            usage_by_pn: dict[str, Any] = {}
            for pn, distance, usage in ranked:
                if pn not in distances:  # keep first (cosine-ordered) occurrence
                    distances[pn] = float(distance)
                    usage_by_pn[pn] = usage
            pns = list(distances.keys())
            placeholders = ", ".join(["?"] * len(pns))
            products = rows_to_products(
                conn,
                f"SELECT * FROM products WHERE product_number IN ({placeholders})",
                pns,
                self.settings,
            )
        prod_by_pn = {p.product_number: p for p in products}

        out: list[Candidate] = []
        for pn in pns:
            p = prod_by_pn.get(pn)
            if p is None:
                continue
            similarity = max(0.0, 1.0 - distances[pn])
            out.append(
                Candidate(
                    product=p,
                    usage_text=usage_by_pn.get(pn),
                    similarity=similarity,
                )
            )
        return out
