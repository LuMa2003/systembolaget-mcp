"""`compare_products` — 2–5 products, side by side."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sb_stack.errors import InvalidInputError, ProductNotFoundError
from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.product_rows import rows_to_products
from sb_stack.mcp_server.responses import CompareResult, CompareRow

_DESCRIPTION = (
    "Jämför 2–5 produkter sida vid sida (pris, smakprofil, ursprung, hållbarhet, lagerstatus)."
)
_COMPARE_FIELDS: tuple[str, ...] = (
    "name_bold",
    "producer_name",
    "country",
    "vintage",
    "price_incl_vat",
    "comparison_price",
    "alcohol_percentage",
    "volume_ml",
    "taste_clock_body",
    "taste_clock_sweetness",
    "taste_clock_bitter",
    "taste_clock_fruitacid",
    "taste_clock_roughness",
    "taste_clock_smokiness",
    "taste_clock_casque",
    "is_organic",
    "assortment_text",
)


class CompareInput(BaseModel):
    product_numbers: list[str] = Field(..., min_length=2, max_length=5)


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def compare_products(inp: CompareInput) -> CompareResult:
        ctx = get_context()
        pns = list(dict.fromkeys(inp.product_numbers))  # preserve order, dedupe
        if len(pns) < 2:
            raise InvalidInputError("at least 2 distinct product_numbers required")

        placeholders = ", ".join(["?"] * len(pns))
        with ctx.db.reader() as conn:
            rows = conn.execute(
                f"SELECT * FROM products WHERE product_number IN ({placeholders})",
                pns,
            ).fetchall()
            cols = [d[0] for d in conn.description]
            by_pn: dict[str, dict[str, Any]] = {}
            for r in rows:
                d = dict(zip(cols, r, strict=True))
                by_pn[d["product_number"]] = d
            for pn in pns:
                if pn not in by_pn:
                    raise ProductNotFoundError(pn)

            products = rows_to_products(
                conn,
                f"SELECT * FROM products WHERE product_number IN ({placeholders})",
                pns,
                ctx.settings,
            )

        # Preserve caller's order.
        prod_by_pn = {p.product_number: p for p in products}
        ordered_products = [prod_by_pn[pn] for pn in pns]
        table_rows = [
            CompareRow(field=f, values=[by_pn[pn].get(f) for pn in pns]) for f in _COMPARE_FIELDS
        ]
        return CompareResult(rows=table_rows, products=ordered_products)
