"""Semantic-retrieval pairing engine.

Flow:
  1. Embed the dish text.
  2. Rank all (non-discontinued, optional in-stock) products by cosine
     distance from the query in `product_embeddings`.
  3. Apply a light taste_symbols bias if the caller hints at food
     categories (`dominant_component_hint` or explicit `taste_symbols`).
  4. Pick a diverse top-N (one per category_level_2 at most).
  5. Compute a confidence signal from the top-result cosine similarity
     and the gap between top and #5.

This is the single-signal MVP called out in DISH_PAIRING_DESIGN.md §"Phased
rollout". Promoting to the full 8-axis scorer is a drop-in expansion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sb_stack.db import DB
from sb_stack.embed import EmbeddingClient
from sb_stack.errors import EmbeddingError
from sb_stack.settings import Settings

if TYPE_CHECKING:
    from sb_stack.mcp_server.responses import Product

Confidence = Literal["low", "medium", "high"]


@dataclass
class Recommendation:
    product: Product
    similarity: float
    why: str  # short Swedish one-liner — LLM can rewrite if needed


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
    ) -> tuple[list[Recommendation], Confidence]:
        if not dish.strip():
            return [], "low"

        q_text = self._build_query_text(dish, meal_context, taste_symbols_hint)
        try:
            vectors = await self.embed_client.embed([q_text])
        except EmbeddingError:
            return [], "low"
        if not vectors:
            return [], "low"
        q_vec = vectors[0]

        candidates = self._rank_candidates(
            q_vec=q_vec,
            taste_symbols_hint=taste_symbols_hint,
            site_ids_in_stock=site_ids_in_stock,
            budget_max=budget_max,
            fetch_n=limit * 5,
        )
        if not candidates:
            return [], "low"

        diverse = _diversify(candidates, limit=limit)
        confidence = _confidence_for(candidates)
        return [
            Recommendation(
                product=p,
                similarity=sim,
                why=_format_why(p, sim),
            )
            for (p, sim) in diverse
        ], confidence

    # ── Internals ───────────────────────────────────────────────────────

    @staticmethod
    def _build_query_text(dish: str, meal_context: str | None, hints: list[str] | None) -> str:
        parts: list[str] = [dish.strip()]
        if meal_context:
            parts.append(f"Sammanhang: {meal_context}")
        if hints:
            parts.append(f"Matsymboler: {', '.join(hints)}")
        return " | ".join(parts)

    def _rank_candidates(
        self,
        *,
        q_vec: list[float],
        taste_symbols_hint: list[str] | None,
        site_ids_in_stock: list[str] | None,
        budget_max: float | None,
        fetch_n: int,
    ) -> list[tuple[Product, float]]:
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

        with self.db.reader() as conn:
            sql = f"""
                SELECT p.product_number,
                       array_cosine_distance(
                           e.embedding, ?::FLOAT[{self.settings.embed_dim}]
                       ) AS distance
                  FROM products p
                  JOIN product_embeddings e USING (product_number)
                 {where_sql}
                 ORDER BY distance ASC
                 LIMIT ?
            """
            ranked = conn.execute(sql, [q_vec, *params, fetch_n]).fetchall()
            pns = [r[0] for r in ranked]
            distances = {r[0]: float(r[1]) for r in ranked}
            if not pns:
                return []
            placeholders = ", ".join(["?"] * len(pns))
            products = rows_to_products(
                conn,
                f"SELECT * FROM products WHERE product_number IN ({placeholders})",
                pns,
                self.settings,
            )
        prod_by_pn = {p.product_number: p for p in products}

        out: list[tuple[Product, float]] = []
        for pn in pns:
            p = prod_by_pn.get(pn)
            if p is None:
                continue
            similarity = max(0.0, 1.0 - distances[pn])
            out.append((p, similarity))
        return out


def _diversify(
    candidates: list[tuple[Product, float]], *, limit: int
) -> list[tuple[Product, float]]:
    """Light-weight diversification: at most one per category_level_2."""
    picked: list[tuple[Product, float]] = []
    seen: set[str] = set()
    for p, sim in candidates:
        bucket = (p.category_level_2 or p.category_level_1 or "other").lower()
        if bucket in seen:
            continue
        picked.append((p, sim))
        seen.add(bucket)
        if len(picked) >= limit:
            break
    if len(picked) < limit:
        # Not enough buckets; top up with the remaining highest-similarity picks.
        for p, sim in candidates:
            if (p, sim) in picked:
                continue
            picked.append((p, sim))
            if len(picked) >= limit:
                break
    return picked


def _confidence_for(candidates: list[tuple[Product, float]]) -> Confidence:
    """High if top match is strong AND well-separated from the tail."""
    if not candidates:
        return "low"
    top_sim = candidates[0][1]
    tail_sim = candidates[min(4, len(candidates) - 1)][1]
    gap = top_sim - tail_sim
    if top_sim >= 0.55 and gap >= 0.05:
        return "high"
    if top_sim >= 0.4:
        return "medium"
    return "low"


def _format_why(product: Product, similarity: float) -> str:
    symbols = ", ".join(product.taste_symbols) if product.taste_symbols else None
    parts = [f"semantisk träff ({similarity:.2f})"]
    if symbols:
        parts.append(f"passar till {symbols}")
    if product.country:
        parts.append(product.country)
    return "; ".join(parts)
