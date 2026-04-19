"""`find_similar_products` — vector-similarity lookup vs a given product."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sb_stack.errors import ProductNotFoundError
from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.product_rows import rows_to_products
from sb_stack.mcp_server.responses import (
    SemanticSearchItem,
    SimilarProductsResult,
)
from sb_stack.mcp_server.sugar import resolve_site_ids

_DESCRIPTION = (
    "Hitta drycker som liknar en given produkt. Matchas på smakklockor, "
    "kategori och semantisk beskrivning."
)


class SimilarInput(BaseModel):
    product_number: str
    limit: int = Field(default=10, ge=1, le=50)
    same_category_only: bool = True
    max_price: float | None = None
    in_stock_at: str | None = None


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def find_similar_products(inp: SimilarInput) -> SimilarProductsResult:
        ctx = get_context()
        with ctx.db.reader() as conn:
            src_row = conn.execute(
                """
                SELECT p.*, e.embedding
                  FROM products p
                  LEFT JOIN product_embeddings e USING (product_number)
                 WHERE p.product_number = ?
                """,
                [inp.product_number],
            ).fetchone()
            if src_row is None:
                raise ProductNotFoundError(inp.product_number)
            cols = [d[0] for d in conn.description]
            src = dict(zip(cols, src_row, strict=True))
            if src.get("embedding") is None:
                # No embedding yet — return an empty list rather than crashing.
                products = rows_to_products(
                    conn,
                    "SELECT * FROM products WHERE product_number = ?",
                    [inp.product_number],
                    ctx.settings,
                )
                return SimilarProductsResult(source=products[0], similar=[])

            where: list[str] = [
                "p.product_number != ?",
                "(p.is_discontinued IS NULL OR p.is_discontinued = FALSE)",
            ]
            params: list[Any] = [inp.product_number]
            if inp.same_category_only and src.get("category_level_1"):
                where.append("p.category_level_1 = ?")
                params.append(src["category_level_1"])
            if inp.max_price is not None:
                where.append("p.price_incl_vat <= ?")
                params.append(inp.max_price)
            site_ids = resolve_site_ids(inp.in_stock_at, ctx.settings)
            if site_ids:
                placeholders = ", ".join(["?"] * len(site_ids))
                where.append(
                    "p.product_number IN (SELECT product_number FROM stock "
                    f"WHERE site_id IN ({placeholders}) AND stock > 0)"
                )
                params.extend(site_ids)

            where_sql = " WHERE " + " AND ".join(where)
            sql = f"""
                SELECT p.product_number,
                       array_cosine_distance(
                           e.embedding, ?::FLOAT[{ctx.settings.embed_dim}]
                       ) AS distance
                  FROM products p
                  JOIN product_embeddings e USING (product_number)
                 {where_sql}
                 ORDER BY distance ASC
                 LIMIT ?
            """
            ranked = conn.execute(sql, [src["embedding"], *params, inp.limit]).fetchall()
            pns = [r[0] for r in ranked]
            distance_by_pn = {r[0]: float(r[1]) for r in ranked}

            similar_products = []
            if pns:
                placeholders = ", ".join(["?"] * len(pns))
                similar_products = rows_to_products(
                    conn,
                    f"SELECT * FROM products WHERE product_number IN ({placeholders})",
                    pns,
                    ctx.settings,
                )
            source_products = rows_to_products(
                conn,
                "SELECT * FROM products WHERE product_number = ?",
                [inp.product_number],
                ctx.settings,
            )
        source = source_products[0]
        pn_to_p = {p.product_number: p for p in similar_products}
        ordered: list[SemanticSearchItem] = []
        for pn in pns:
            p = pn_to_p.get(pn)
            if p is None:
                continue
            similarity = max(0.0, 1.0 - distance_by_pn[pn])
            ordered.append(SemanticSearchItem(**p.model_dump(), similarity=round(similarity, 4)))
        return SimilarProductsResult(source=source, similar=ordered)
