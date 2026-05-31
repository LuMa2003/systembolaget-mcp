"""`search_products` — faceted catalog search with keyword + home stock.

See docs/04_mcp_surface.md for the full parameter list. This implementation
covers the common-case filters; extending the filter set is mechanical.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from sb_stack.mcp_server import sugar
from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.product_rows import rows_to_products
from sb_stack.mcp_server.responses import Product, SearchProductsResult
from sb_stack.mcp_server.sugar import resolve_site_ids
from sb_stack.settings import Settings

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
    "m.m. Sökordet (text) söker och rangordnar på relevans i produktnamn och "
    "producent (skiftlägesokänsligt). Land, kategori och druva matchas skiftlägesokänsligt "
    "mot Systembolagets svenska benämningar. Använd denna när frågan kan "
    "uttryckas som strukturerade kriterier eller innehåller exakta namn och sökord."
)


class SearchInput(BaseModel):
    text: str | None = None
    category: str | None = None
    subcategory: str | None = None
    country: str | None = None
    grapes_any: list[str] | None = None
    vintage: str | None = None
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    abv_min: float | None = Field(default=None, ge=0)
    abv_max: float | None = Field(default=None, ge=0)
    volume_min_ml: int | None = None
    volume_max_ml: int | None = None
    taste_body_min: int | None = Field(default=None, ge=0)
    taste_body_max: int | None = Field(default=None, ge=0)
    taste_sweet_min: int | None = Field(default=None, ge=0)
    taste_sweet_max: int | None = Field(default=None, ge=0)
    taste_bitter_min: int | None = Field(default=None, ge=0)
    taste_bitter_max: int | None = Field(default=None, ge=0)
    taste_fruitacid_min: int | None = Field(default=None, ge=0)
    taste_fruitacid_max: int | None = Field(default=None, ge=0)
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

    @model_validator(mode="after")
    def _check_ranges(self) -> SearchInput:
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("lägsta pris kan inte vara högre än högsta pris")
        if self.abv_min is not None and self.abv_max is not None and self.abv_min > self.abv_max:
            raise ValueError("lägsta alkoholhalt kan inte vara högre än högsta alkoholhalt")
        return self


def _build_where(  # noqa: PLR0912, PLR0915 — one branch per filter is cohesive.
    inp: SearchInput,
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []

    # NB: free-text (inp.text) is handled separately in `register` — via the
    # FTS BM25 index when available, else the substring fallback below.
    if inp.category:
        where.append("lower(category_level_1) = lower(?)")
        params.append(inp.category)
    if inp.subcategory:
        where.append("(lower(category_level_2) = lower(?) OR lower(category_level_3) = lower(?))")
        params.extend([inp.subcategory, inp.subcategory])
    if inp.country:
        where.append("lower(country) = lower(?)")
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
        where.append("list_has_any(list_transform(taste_symbols, x -> lower(x)), ?::VARCHAR[])")
        params.append([s.lower() for s in inp.pairs_with_any])
    if inp.grapes_any:
        where.append("list_has_any(list_transform(grapes, x -> lower(x)), ?::VARCHAR[])")
        params.append([s.lower() for s in inp.grapes_any])
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


_TEXT_ILIKE = (
    "(lower(name_bold) LIKE '%' || lower(?) || '%' "
    "OR lower(name_thin) LIKE '%' || lower(?) || '%' "
    "OR lower(producer_name) LIKE '%' || lower(?) || '%')"
)


def _fts_available(conn: Any) -> bool:
    """True when the DuckDB FTS index has been built (by the sync finalize phase)."""
    row = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'fts_main_products'"
    ).fetchone()
    return row is not None


def _order_clause(inp: SearchInput) -> str:
    # name ordering uses a trimmed, name_thin-preferring expression because
    # name_bold frequently holds the producer and has leading whitespace.
    name_expr = "trim(COALESCE(name_thin, name_bold))"
    if inp.order_by == "relevance":
        # With a keyword the substring match is the relevance signal; we still
        # order deterministically by trimmed name. Without a keyword, ordering
        # by cheapest comparison price is more useful than dirty alphabetical.
        if inp.text:
            return name_expr
        return "comparison_price ASC NULLS LAST"
    return {
        "price_asc": "price_incl_vat ASC NULLS LAST",
        "price_desc": "price_incl_vat DESC NULLS LAST",
        "launch_desc": "product_launch_date DESC NULLS LAST",
        "body_asc": "taste_clock_body ASC NULLS LAST",
        "body_desc": "taste_clock_body DESC NULLS LAST",
        "comparison_price_asc": "comparison_price ASC NULLS LAST",
    }[inp.order_by]


def _run_fts_query(
    conn: Any,
    inp: SearchInput,
    where_sql: str,
    params: list[Any],
    settings: Settings,
) -> tuple[int, list[Product]]:
    """BM25-ranked text search over the FTS index, with structured filters applied.

    match_bm25 returns NULL for non-matching rows, so we wrap the filtered set and
    keep only scored rows, ordered by descending relevance.
    """
    inner = (
        "SELECT *, fts_main_products.match_bm25(product_number, ?) AS _bm25 "
        f"FROM products{where_sql}"
    )
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM ({inner}) WHERE _bm25 IS NOT NULL",
        [inp.text, *params],
    ).fetchone()
    total = int(count_row[0]) if count_row else 0
    sql = f"SELECT * FROM ({inner}) WHERE _bm25 IS NOT NULL ORDER BY _bm25 DESC LIMIT ? OFFSET ?"
    products = rows_to_products(conn, sql, [inp.text, *params, inp.limit, inp.offset], settings)
    return total, products


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def search_products(  # noqa: PLR0913 — flat params mirror the input schema (issue #12).
        text: str | None = None,
        category: str | None = None,
        subcategory: str | None = None,
        country: str | None = None,
        grapes_any: list[str] | None = None,
        vintage: str | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        abv_min: float | None = None,
        abv_max: float | None = None,
        volume_min_ml: int | None = None,
        volume_max_ml: int | None = None,
        taste_body_min: int | None = None,
        taste_body_max: int | None = None,
        taste_sweet_min: int | None = None,
        taste_sweet_max: int | None = None,
        taste_bitter_min: int | None = None,
        taste_bitter_max: int | None = None,
        taste_fruitacid_min: int | None = None,
        taste_fruitacid_max: int | None = None,
        pairs_with_any: list[str] | None = None,
        is_organic: bool | None = None,
        is_vegan: bool | None = None,
        is_gluten_free: bool | None = None,
        assortment_text: str | None = None,
        launched_since: date | None = None,
        in_stock_at: str | None = None,
        include_discontinued: bool = False,
        order_by: OrderBy = "relevance",
        limit: int = 20,
        offset: int = 0,
    ) -> SearchProductsResult:
        inp = SearchInput(
            text=text,
            category=category,
            subcategory=subcategory,
            country=country,
            grapes_any=grapes_any,
            vintage=vintage,
            price_min=price_min,
            price_max=price_max,
            abv_min=abv_min,
            abv_max=abv_max,
            volume_min_ml=volume_min_ml,
            volume_max_ml=volume_max_ml,
            taste_body_min=taste_body_min,
            taste_body_max=taste_body_max,
            taste_sweet_min=taste_sweet_min,
            taste_sweet_max=taste_sweet_max,
            taste_bitter_min=taste_bitter_min,
            taste_bitter_max=taste_bitter_max,
            taste_fruitacid_min=taste_fruitacid_min,
            taste_fruitacid_max=taste_fruitacid_max,
            pairs_with_any=pairs_with_any,
            is_organic=is_organic,
            is_vegan=is_vegan,
            is_gluten_free=is_gluten_free,
            assortment_text=assortment_text,
            launched_since=launched_since,
            in_stock_at=in_stock_at,
            include_discontinued=include_discontinued,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )
        ctx = get_context()
        where, params = _build_where(inp)

        # Optional stock filter: restrict to products stocked at the resolved
        # site_ids.
        site_ids = resolve_site_ids(inp.in_stock_at, ctx.settings)

        with ctx.db.reader() as conn:
            if site_ids:
                sugar.assert_stores_exist(conn, site_ids)
                placeholders = ", ".join(["?"] * len(site_ids))
                where.append(
                    f"product_number IN (SELECT product_number FROM stock "
                    f"WHERE site_id IN ({placeholders}) AND stock > 0)"
                )
                params.extend(site_ids)

            # Free text: BM25-rank via the FTS index when it exists; otherwise
            # fall back to a case-insensitive substring match.
            use_fts = bool(inp.text) and _fts_available(conn)
            if inp.text and not use_fts:
                where.append(_TEXT_ILIKE)
                params.extend([inp.text, inp.text, inp.text])

            where_sql = (" WHERE " + " AND ".join(where)) if where else ""

            if use_fts:
                total, products = _run_fts_query(conn, inp, where_sql, params, ctx.settings)
            else:
                count_row = conn.execute(
                    f"SELECT COUNT(*) FROM products{where_sql}", params
                ).fetchone()
                total = int(count_row[0]) if count_row else 0
                sql = (
                    f"SELECT * FROM products{where_sql} "
                    f"ORDER BY {_order_clause(inp)} LIMIT ? OFFSET ?"
                )
                products = rows_to_products(
                    conn, sql, [*params, inp.limit, inp.offset], ctx.settings
                )
        return SearchProductsResult(results=products, total_count=int(total))
