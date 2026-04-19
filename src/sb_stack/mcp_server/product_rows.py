"""Shared helpers for loading product rows + home stock lookup.

Every product-returning tool funnels through these so the output shape,
image_url derivation, and stock join logic stay in one place.
"""

from __future__ import annotations

from typing import Any

import duckdb

from sb_stack.mcp_server.responses import Product, StockAtStore, TasteClocks
from sb_stack.settings import Settings

# Columns selected for the Product summary response. Keep in sync with
# `Product` in responses.py.
PRODUCT_SUMMARY_COLS: tuple[str, ...] = (
    "product_number",
    "name_bold",
    "name_thin",
    "producer_name",
    "category_level_1",
    "category_level_2",
    "category_level_3",
    "country",
    "origin_level_1",
    "volume_ml",
    "alcohol_percentage",
    "price_incl_vat",
    "comparison_price",
    "taste_clock_body",
    "taste_clock_bitter",
    "taste_clock_sweetness",
    "taste_clock_fruitacid",
    "taste_clock_roughness",
    "taste_clock_smokiness",
    "taste_clock_casque",
    "taste_symbols",
    "grapes",
    "is_organic",
    "is_vegan_friendly",
    "is_discontinued",
    "image_url",
)


def row_to_product(row: dict[str, Any]) -> Product:
    """Turn a raw DuckDB row dict into a Product pydantic model."""
    return Product(
        product_number=row["product_number"],
        name_bold=row.get("name_bold") or "",
        name_thin=row.get("name_thin"),
        producer_name=row.get("producer_name"),
        category_level_1=row.get("category_level_1"),
        category_level_2=row.get("category_level_2"),
        category_level_3=row.get("category_level_3"),
        country=row.get("country"),
        origin_level_1=row.get("origin_level_1"),
        volume_ml=row.get("volume_ml"),
        alcohol_percentage=_safe_float(row.get("alcohol_percentage")),
        price_incl_vat=_safe_float(row.get("price_incl_vat")),
        comparison_price=_safe_float(row.get("comparison_price")),
        taste_clocks=TasteClocks(
            body=row.get("taste_clock_body"),
            bitter=row.get("taste_clock_bitter"),
            sweetness=row.get("taste_clock_sweetness"),
            fruitacid=row.get("taste_clock_fruitacid"),
            roughness=row.get("taste_clock_roughness"),
            smokiness=row.get("taste_clock_smokiness"),
            casque=row.get("taste_clock_casque"),
        ),
        taste_symbols=list(row.get("taste_symbols") or []),
        grapes=list(row.get("grapes") or []),
        is_organic=row.get("is_organic"),
        is_vegan_friendly=row.get("is_vegan_friendly"),
        is_discontinued=row.get("is_discontinued"),
        image_url=row.get("image_url"),
    )


def rows_to_products(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any],
    settings: Settings,
) -> list[Product]:
    """Run `sql` (which must SELECT * or the summary columns), return Products."""
    results = conn.execute(sql, params).fetchall()
    cols = [d[0] for d in conn.description]
    products: list[Product] = []
    pn_to_product: dict[str, Product] = {}
    for row in results:
        row_dict = dict(zip(cols, row, strict=True))
        p = row_to_product(row_dict)
        products.append(p)
        pn_to_product[p.product_number] = p

    if products and settings.store_subset:
        _attach_home_stock(conn, pn_to_product, settings)
    return products


def _attach_home_stock(
    conn: duckdb.DuckDBPyConnection,
    pn_to_product: dict[str, Product],
    settings: Settings,
) -> None:
    placeholders = ", ".join(["?"] * len(pn_to_product))
    sub_placeholders = ", ".join(["?"] * len(settings.store_subset))
    rows = conn.execute(
        f"""
        SELECT product_number, site_id, stock, shelf, is_in_assortment, observed_at
          FROM stock
         WHERE product_number IN ({placeholders})
           AND site_id IN ({sub_placeholders})
        """,
        [*pn_to_product.keys(), *settings.store_subset],
    ).fetchall()
    for pn, site_id, stock, shelf, in_asm, observed in rows:
        p = pn_to_product.get(pn)
        if p is None:
            continue
        p.home_stock[site_id] = StockAtStore(
            stock=int(stock or 0),
            shelf=shelf,
            is_in_assortment=in_asm,
            observed_at=observed,
        )


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
