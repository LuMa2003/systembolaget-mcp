"""`search_products` — faceted catalog search with FTS + home stock.

See docs/04_mcp_surface.md for the full parameter list. This implementation
covers the common-case filters; extending the filter set is mechanical.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.product_rows import rows_to_products
from sb_stack.mcp_server.responses import SearchProductsResult
from sb_stack.mcp_server.sugar import resolve_site_ids

OrderBy = Literal[
    "relevance",
    "price_asc",
    "price_desc",
    "launch_desc",
    "body_asc",
    "body_desc",
    "comparison_price_asc",
]

_DESCRIPTION = (
    "Sök i Systembolagets sortiment med filter: kategori, land, pris, "
    "alkoholhalt, smakklockor, matsymboler, förpackning, certifieringar, "
    "m.m. Använd denna när frågan kan uttryckas som strukturerade kriterier "
    "eller innehåller exakta namn och sökord."
)


class SearchInput(BaseModel):
    text: str | None = None
    category: str | None = None
    subcategory: str | None = None
    country: str | None = None
    grapes_any: list[str] | None = None
    vintage: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    abv_min: float | None = None
    abv_max: float | None = None
    volume_min_ml: int | None = None
    volume_max_ml: int | None = None
    taste_body_min: int | None = None
    taste_body_max: int | None = None
    taste_sweet_min: int | None = None
    taste_sweet_max: int | None = None
    taste_bitter_min: int | None = None
    taste_bitter_max: int | None = None
    taste_fruitacid_min: int | None = None
    taste_fruitacid_max: int | None = None
    pairs_with_any: list[str] | None = None
    is_organic: bool | None = None
    is_vegan: bool | None = None
    is_gluten_free: bool | None = None
    assortment_text: str | None = None
    launched_since: date | None = None
    in_stock_at: str | None = None
    include_discontinued: bool = False
    order_by: OrderBy = "relevance"
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def _build_where(  # noqa: PLR0912, PLR0915 — one branch per filter is cohesive.
    inp: SearchInput,
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []

    if inp.category:
        where.append("category_level_1 = ?")
        params.append(inp.category)
    if inp.subcategory:
        where.append("(category_level_2 = ? OR category_level_3 = ?)")
        params.extend([inp.subcategory, inp.subcategory])
    if inp.country:
        where.append("country = ?")
        params.append(inp.country)
    if inp.vintage:
        where.append("vintage = ?")
        params.append(inp.vintage)
    if inp.price_min is not None:
        where.append("price_incl_vat >= ?")
        params.append(inp.price_min)
    if inp.price_max is not None:
        where.append("price_incl_vat <= ?")
        params.append(inp.price_max)
    if inp.abv_min is not None:
        where.append("alcohol_percentage >= ?")
        params.append(inp.abv_min)
    if inp.abv_max is not None:
        where.append("alcohol_percentage <= ?")
        params.append(inp.abv_max)
    if inp.volume_min_ml is not None:
        where.append("volume_ml >= ?")
        params.append(inp.volume_min_ml)
    if inp.volume_max_ml is not None:
        where.append("volume_ml <= ?")
        params.append(inp.volume_max_ml)
    for fld, lo, hi in (
        ("taste_clock_body", inp.taste_body_min, inp.taste_body_max),
        ("taste_clock_sweetness", inp.taste_sweet_min, inp.taste_sweet_max),
        ("taste_clock_bitter", inp.taste_bitter_min, inp.taste_bitter_max),
        ("taste_clock_fruitacid", inp.taste_fruitacid_min, inp.taste_fruitacid_max),
    ):
        if lo is not None:
            where.append(f"{fld} >= ?")
            params.append(lo)
        if hi is not None:
            where.append(f"{fld} <= ?")
            params.append(hi)
    if inp.pairs_with_any:
        where.append("list_has_any(taste_symbols, ?::VARCHAR[])")
        params.append(inp.pairs_with_any)
    if inp.grapes_any:
        where.append("list_has_any(grapes, ?::VARCHAR[])")
        params.append(inp.grapes_any)
    if inp.is_organic is not None:
        where.append("is_organic = ?")
        params.append(inp.is_organic)
    if inp.is_vegan is not None:
        where.append("is_vegan_friendly = ?")
        params.append(inp.is_vegan)
    if inp.is_gluten_free is not None:
        where.append("is_gluten_free = ?")
        params.append(inp.is_gluten_free)
    if inp.assortment_text:
        where.append("assortment_text = ?")
        params.append(inp.assortment_text)
    if inp.launched_since is not None:
        where.append("product_launch_date >= ?")
        params.append(inp.launched_since)
    if not inp.include_discontinued:
        where.append("(is_discontinued IS NULL OR is_discontinued = FALSE)")
    return where, params


def _order_clause(order_by: OrderBy) -> str:
    return {
        "relevance": "name_bold",
        "price_asc": "price_incl_vat ASC NULLS LAST",
        "price_desc": "price_incl_vat DESC NULLS LAST",
        "launch_desc": "product_launch_date DESC NULLS LAST",
        "body_asc": "taste_clock_body ASC NULLS LAST",
        "body_desc": "taste_clock_body DESC NULLS LAST",
        "comparison_price_asc": "comparison_price ASC NULLS LAST",
    }[order_by]


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def search_products(inp: SearchInput) -> SearchProductsResult:
        ctx = get_context()
        where, params = _build_where(inp)

        # Optional stock filter: restrict to products stocked at the resolved
        # site_ids.
        site_ids = resolve_site_ids(inp.in_stock_at, ctx.settings)
        if site_ids:
            placeholders = ", ".join(["?"] * len(site_ids))
            where.append(
                f"product_number IN (SELECT product_number FROM stock "
                f"WHERE site_id IN ({placeholders}) AND stock > 0)"
            )
            params.extend(site_ids)

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        # Total count
        with ctx.db.reader() as conn:
            count_row = conn.execute(
                f"SELECT COUNT(*) FROM products{where_sql}", params
            ).fetchone()
            total = int(count_row[0]) if count_row else 0
            # Results page
            sql = (
                f"SELECT * FROM products{where_sql} "
                f"ORDER BY {_order_clause(inp.order_by)} "
                f"LIMIT ? OFFSET ?"
            )
            products = rows_to_products(conn, sql, [*params, inp.limit, inp.offset], ctx.settings)
        return SearchProductsResult(results=products, total_count=int(total))
