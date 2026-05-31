"""`semantic_search` — vector search over product_embeddings."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from sb_stack.errors import InvalidInputError
from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.product_rows import rows_to_products
from sb_stack.mcp_server.responses import (
    SemanticSearchItem,
    SemanticSearchResult,
)
from sb_stack.mcp_server.sugar import resolve_site_ids

_DESCRIPTION = (
    "Hitta drycker genom fritext där beskrivningen är stämningsfull eller "
    "parafraserande, t.ex. 'en rökig whisky som passar en höstkväll' eller "
    "'nåt lättdrucket och fräscht'. Använd när search_products inte räcker. "
    "Skriv sökfrågan på svenska för bästa träffar. Alkoholfria drycker "
    "utesluts om du inte uttryckligen ber om dem (eller sätter "
    "include_alcohol_free=true)."
)

# Swedish words that signal a strong smoke/peat intent in the query. When
# present we apply a structured re-rank on taste_clock_smokiness so peated
# Islays surface even before the templates are re-embedded with clock tokens.
_SMOKE_TERMS = ("rök", "torv", "peat")

# Words that mean the user actually wants alcohol-free drinks.
_ALCOHOL_FREE_TERMS = ("alkoholfri", "utan alkohol")


class SemanticInput(BaseModel):
    query: str
    category: str | None = None
    in_stock_at: str | None = None
    include_alcohol_free: bool = False
    min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("sökfrågan får inte vara tom")
        return stripped


def _candidate_rows(
    ctx: Any, q_vec: list[float], where_sql: str, params: list[Any], limit: int
) -> list[dict[str, Any]]:
    with ctx.db.reader() as conn:
        sql = f"""
            SELECT p.product_number,
                   p.taste_clock_smokiness AS smokiness,
                   array_cosine_distance(
                       e.embedding, ?::FLOAT[{ctx.settings.embed_dim}]
                   ) AS distance
              FROM products p
              JOIN product_embeddings e USING (product_number)
             {where_sql}
             ORDER BY distance ASC
             LIMIT ?
        """
        rows = conn.execute(sql, [q_vec, *params, limit]).fetchall()
        cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    async def semantic_search(
        query: str,
        category: str | None = None,
        in_stock_at: str | None = None,
        include_alcohol_free: bool = False,
        min_similarity: float | None = None,
        limit: int = 10,
    ) -> SemanticSearchResult:
        inp = SemanticInput(
            query=query,
            category=category,
            in_stock_at=in_stock_at,
            include_alcohol_free=include_alcohol_free,
            min_similarity=min_similarity,
            limit=limit,
        )
        ctx = get_context()
        if ctx.embed_client is None:
            raise InvalidInputError("Semantisk sökning är inte tillgänglig just nu.")

        vectors = await ctx.embed_client.embed([inp.query])
        if not vectors:
            return SemanticSearchResult(results=[])
        q_vec = vectors[0]

        q_lower = inp.query.lower()
        smoke_intent = any(term in q_lower for term in _SMOKE_TERMS)
        wants_alcohol_free = any(term in q_lower for term in _ALCOHOL_FREE_TERMS)

        where: list[str] = ["(p.is_discontinued IS NULL OR p.is_discontinued = FALSE)"]
        params: list[Any] = []
        if inp.category:
            where.append("p.category_level_1 = ?")
            params.append(inp.category)
        # Exclude alcohol-free unless the caller opted in or the query asks for it;
        # otherwise marketing language ("lätt/fräsch/fruktig") over-ranks it.
        if not inp.include_alcohol_free and not wants_alcohol_free:
            where.append("(p.category_level_1 IS NULL OR p.category_level_1 != 'Alkoholfritt')")
        site_ids = resolve_site_ids(inp.in_stock_at, ctx.settings)
        if site_ids:
            placeholders = ", ".join(["?"] * len(site_ids))
            where.append(
                "p.product_number IN (SELECT product_number FROM stock "
                f"WHERE site_id IN ({placeholders}) AND stock > 0)"
            )
            params.extend(site_ids)
        where_sql = " WHERE " + " AND ".join(where)

        # Pull a larger candidate set when re-ranking so the smoke boost has
        # material to reorder; otherwise the top-N is already final.
        candidate_limit = max(inp.limit * 5, 50) if smoke_intent else inp.limit
        parsed = _candidate_rows(ctx, q_vec, where_sql, params, candidate_limit)

        scored: list[tuple[str, float]] = []
        for r in parsed:
            similarity = max(0.0, 1.0 - float(r["distance"]))
            if smoke_intent and r.get("smokiness") is not None:
                # Clocks run ~1–11; scale to [0, 1] and blend a modest boost so a
                # genuinely peated dram outranks an empty-taste-text near-neighbour
                # without overwhelming the semantic signal.
                smoke_norm = min(1.0, float(r["smokiness"]) / 11.0)
                similarity = min(1.0, similarity + 0.15 * smoke_norm)
            scored.append((r["product_number"], round(similarity, 4)))

        if smoke_intent:
            scored.sort(key=lambda x: x[1], reverse=True)

        if inp.min_similarity is not None:
            scored = [s for s in scored if s[1] >= inp.min_similarity]
        scored = scored[: inp.limit]

        if not scored:
            return SemanticSearchResult(results=[])

        pns = [pn for pn, _ in scored]
        with ctx.db.reader() as conn:
            placeholders = ", ".join(["?"] * len(pns))
            products = rows_to_products(
                conn,
                f"SELECT * FROM products WHERE product_number IN ({placeholders})",
                pns,
                ctx.settings,
            )

        prod_by_pn = {p.product_number: p for p in products}
        items: list[SemanticSearchItem] = []
        for pn, similarity in scored:
            p = prod_by_pn.get(pn)
            if p is None:
                continue
            items.append(
                SemanticSearchItem(
                    **p.model_dump(),
                    similarity=similarity,
                )
            )
        return SemanticSearchResult(results=items)
