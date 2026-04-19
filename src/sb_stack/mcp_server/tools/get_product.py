"""`get_product` — full product detail + variants + home-stock + image URLs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

from sb_stack.errors import InvalidInputError, ProductNotFoundError
from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.responses import (
    GetProductResult,
    HomeStockRow,
    ImageSize,
    Variant,
)

_DESCRIPTION = (
    "Hämta fullständig information om en specifik produkt (alla ~170 fält "
    "plus varianter i andra storlekar och lagerstatus i hemmabutikerna)."
)
_IMAGE_SIZES = (100, 200, 400, 800)
_IMAGE_URL_TEMPLATE = "https://product-cdn.systembolaget.se/productimages/{pid}/{pid}_{size}.webp"


class GetProductInput(BaseModel):
    product_number: str | None = None
    query: str | None = None

    @model_validator(mode="after")
    def _require_one(self) -> GetProductInput:
        if not self.product_number and not self.query:
            raise InvalidInputError("provide product_number or query")
        return self


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def get_product(inp: GetProductInput) -> GetProductResult:
        ctx = get_context()
        with ctx.db.reader() as conn:
            pn = inp.product_number
            if pn is None:
                # Simple fallback fuzzy lookup: case-insensitive LIKE on
                # name_bold. FTS would be richer; can swap in later.
                row = conn.execute(
                    """
                    SELECT product_number FROM products
                     WHERE lower(name_bold) LIKE lower(?)
                     ORDER BY is_discontinued ASC NULLS FIRST
                     LIMIT 1
                    """,
                    [f"%{inp.query}%"],
                ).fetchone()
                if row is None:
                    raise ProductNotFoundError(inp.query or "")
                pn = row[0]

            row = conn.execute("SELECT * FROM products WHERE product_number = ?", [pn]).fetchone()
            if row is None:
                raise ProductNotFoundError(pn)
            cols = [d[0] for d in conn.description]
            product_dict: dict[str, Any] = dict(zip(cols, row, strict=True))

            variants_rows = conn.execute(
                """
                SELECT variant_product_number, variant_volume_ml, variant_bottle_text
                  FROM product_variants
                 WHERE product_number = ?
                """,
                [pn],
            ).fetchall()
            variants = [
                Variant(
                    variant_product_number=r[0],
                    variant_volume_ml=r[1],
                    variant_bottle_text=r[2],
                )
                for r in variants_rows
            ]

            stock_rows = conn.execute(
                """
                SELECT s.site_id, st.alias, s.stock, s.shelf, s.is_in_assortment,
                       s.observed_at
                  FROM stock s
                  LEFT JOIN stores st USING (site_id)
                 WHERE s.product_number = ?
                """,
                [pn],
            ).fetchall()
            home_stock = [
                HomeStockRow(
                    site_id=r[0],
                    alias=r[1],
                    stock=r[2],
                    shelf=r[3],
                    is_in_assortment=r[4],
                    observed_at=r[5],
                )
                for r in stock_rows
            ]

        pid = product_dict.get("product_id")
        image_urls = (
            [
                ImageSize(size=sz, url=_IMAGE_URL_TEMPLATE.format(pid=pid, size=sz))
                for sz in _IMAGE_SIZES
            ]
            if pid
            else []
        )

        return GetProductResult(
            product=product_dict,
            variants=variants,
            home_stock=home_stock,
            image_urls=image_urls,
        )
