"""`semantic_search` — vector search over product_embeddings."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

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
    "'nåt lättdrucket och fräscht'. Använd när search_products inte räcker."
)


class SemanticInput(BaseModel):
    query: str
    category: str | None = None
    in_stock_at: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    async def semantic_search(inp: SemanticInput) -> SemanticSearchResult:
        ctx = get_context()
        if ctx.embed_client is None:
            raise RuntimeError("semantic_search unavailable: embed client not configured")

        vectors = await ctx.embed_client.embed([inp.query])
        if not vectors:
            return SemanticSearchResult(results=[])
        q_vec = vectors[0]

        where: list[str] = ["(p.is_discontinued IS NULL OR p.is_discontinued = FALSE)"]
        params: list[Any] = []
        if inp.category:
            where.append("p.category_level_1 = ?")
            params.append(inp.category)
        site_ids = resolve_site_ids(inp.in_stock_at, ctx.settings)
        if site_ids:
            placeholders = ", ".join(["?"] * len(site_ids))
            where.append(
                "p.product_number IN (SELECT product_number FROM stock "
                f"WHERE site_id IN ({placeholders}) AND stock > 0)"
            )
            params.extend(site_ids)
        where_sql = " WHERE " + " AND ".join(where)

        with ctx.db.reader() as conn:
            sql = f"""
                SELECT p.*,
                       array_cosine_distance(
                           e.embedding, ?::FLOAT[{ctx.settings.embed_dim}]
                       ) AS distance
                  FROM products p
                  JOIN product_embeddings e USING (product_number)
                 {where_sql}
                 ORDER BY distance ASC
                 LIMIT ?
            """
            rows = conn.execute(sql, [q_vec, *params, inp.limit]).fetchall()
            cols = [d[0] for d in conn.description]
        parsed = [dict(zip(cols, r, strict=True)) for r in rows]

        # Reuse rows_to_products by loading Products once more — simplest path.
        # For this scaffold the re-query is acceptable; a later tune can merge.
        with ctx.db.reader() as conn:
            pns = [r["product_number"] for r in parsed]
            if not pns:
                return SemanticSearchResult(results=[])
            placeholders = ", ".join(["?"] * len(pns))
            products = rows_to_products(
                conn,
                f"SELECT * FROM products WHERE product_number IN ({placeholders})",
                pns,
                ctx.settings,
            )

        prod_by_pn = {p.product_number: p for p in products}
        items: list[SemanticSearchItem] = []
        for r in parsed:
            p = prod_by_pn.get(r["product_number"])
            if p is None:
                continue
            similarity = max(0.0, 1.0 - float(r["distance"]))
            items.append(
                SemanticSearchItem(
                    **p.model_dump(),
                    similarity=round(similarity, 4),
                )
            )
        return SemanticSearchResult(results=items)
