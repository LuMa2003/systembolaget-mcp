"""`compare_products` — 2–5 products, side by side."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

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
# Taste-clock rows are calibrated per category, so they aren't comparable
# across e.g. wine vs. beer vs. spirits (#16).
_TASTE_CLOCK_FIELDS = frozenset(f for f in _COMPARE_FIELDS if f.startswith("taste_clock_"))
_CROSS_CATEGORY_NOTE = (
    "Smakklockor är kalibrerade per kategori och bör inte jämföras rakt av "
    "mellan vin, öl och sprit."
)


class CompareInput(BaseModel):
    product_numbers: list[str]

    @model_validator(mode="after")
    def _check_count(self) -> CompareInput:
        distinct = len(dict.fromkeys(self.product_numbers))
        if not 2 <= distinct <= 5:
            raise InvalidInputError("jämför mellan 2 och 5 produkter")
        return self


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def compare_products(product_numbers: list[str]) -> CompareResult:
        inp = CompareInput(product_numbers=product_numbers)
        ctx = get_context()
        pns = list(dict.fromkeys(inp.product_numbers))  # preserve order, dedupe

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

        categories = {by_pn[pn].get("category_level_1") for pn in pns}
        cross_category = len(categories) > 1

        table_rows: list[CompareRow] = []
        for f in _COMPARE_FIELDS:
            values = [by_pn[pn].get(f) for pn in pns]
            # Across categories, taste-clock rows mix calibrations; drop any
            # that carry no signal at all rather than show all-null rows.
            if cross_category and f in _TASTE_CLOCK_FIELDS and all(v is None for v in values):
                continue
            table_rows.append(CompareRow(field=f, values=values))

        notes = _CROSS_CATEGORY_NOTE if cross_category else None
        return CompareResult(rows=table_rows, products=ordered_products, notes=notes)
