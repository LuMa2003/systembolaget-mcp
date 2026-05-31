"""`list_taxonomy_values` — enumerate valid filter values for search_products."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sb_stack.errors import InvalidInputError
from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.responses import TaxonomyEntry, TaxonomyResult

_DESCRIPTION = (
    "Lista giltiga värden för ett sökfilter, t.ex. alla länder i sortimentet, "
    "alla druvor, alla matsymboler. Använd innan search_products om du är osäker "
    "på exakta värden. Giltiga filter (svenska alias accepteras): land, druva, "
    "matsymboler, kategori, förpackning, försegling, sortiment, årgång."
)

# Canonical API taxonomy keys we expose, plus Swedish aliases. Lookup is
# case-insensitive: the user-facing vocabulary is Swedish, the storage key is
# the raw Systembolaget API key.
_ALIASES: dict[str, str] = {
    "matsymboler": "TasteSymbols",
    "matsymbol": "TasteSymbols",
    "tastesymbols": "TasteSymbols",
    "land": "Country",
    "länder": "Country",
    "country": "Country",
    "druva": "Grapes",
    "druvor": "Grapes",
    "grapes": "Grapes",
    "kategori": "CategoryLevel1",
    "categorylevel1": "CategoryLevel1",
    "förpackning": "PackagingLevel1",
    "packaginglevel1": "PackagingLevel1",
    "försegling": "Seal",
    "märkning": "Seal",
    "seal": "Seal",
    "sortiment": "AssortmentText",
    "assortmenttext": "AssortmentText",
    "årgång": "Vintage",
    "vintage": "Vintage",
}

# Swedish filter names advertised to the user, in a stable order.
_VALID_NAMES = (
    "land",
    "druva",
    "matsymboler",
    "kategori",
    "förpackning",
    "försegling",
    "sortiment",
    "årgång",
)

# Facets that map to a product column: counts are computed from the synced
# products table so taxonomy agrees with what search_products can return.
# value: SQL expression yielding (value, count) rows, ordered/filtered by caller.
_PRODUCT_COLUMN_FACETS: dict[str, str] = {
    "Country": "SELECT country AS value, COUNT(*) AS cnt FROM products "
    "WHERE country IS NOT NULL GROUP BY country",
    "CategoryLevel1": "SELECT category_level_1 AS value, COUNT(*) AS cnt FROM products "
    "WHERE category_level_1 IS NOT NULL GROUP BY category_level_1",
    "Grapes": "SELECT g AS value, COUNT(*) AS cnt FROM "
    "(SELECT unnest(grapes) AS g FROM products) WHERE g IS NOT NULL GROUP BY g",
    "TasteSymbols": "SELECT s AS value, COUNT(*) AS cnt FROM "
    "(SELECT unnest(taste_symbols) AS s FROM products) WHERE s IS NOT NULL GROUP BY s",
}


class TaxonomyInput(BaseModel):
    filter_name: str
    min_count: int = Field(default=1, ge=0)


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def list_taxonomy_values(filter_name: str, min_count: int = 1) -> TaxonomyResult:
        inp = TaxonomyInput(filter_name=filter_name, min_count=min_count)

        canonical = _ALIASES.get(inp.filter_name.strip().lower())
        if canonical is None:
            valid = ", ".join(_VALID_NAMES)
            raise InvalidInputError(
                f"okänt filter '{inp.filter_name}'. Giltiga filter är: {valid}."
            )

        ctx = get_context()
        with ctx.db.reader() as conn:
            if canonical in _PRODUCT_COLUMN_FACETS:
                # Counts straight from the synced catalog so they match search_products.
                rows = conn.execute(
                    f"SELECT value, cnt FROM ({_PRODUCT_COLUMN_FACETS[canonical]}) "
                    "WHERE cnt >= ? ORDER BY cnt DESC, value ASC",
                    [inp.min_count],
                ).fetchall()
                return TaxonomyResult(
                    values=[TaxonomyEntry(value=r[0], count=int(r[1])) for r in rows],
                    captured_at=None,
                )

            # Facets without a clean product column fall back to the API snapshot.
            latest_row = conn.execute(
                "SELECT MAX(captured_at) FROM filter_taxonomy WHERE filter_name = ?",
                [canonical],
            ).fetchone()
            latest = latest_row[0] if latest_row else None
            if latest is None:
                return TaxonomyResult(values=[], captured_at=None)
            rows = conn.execute(
                """
                SELECT value, count
                  FROM filter_taxonomy
                 WHERE filter_name = ? AND captured_at = ? AND count >= ?
                       AND value <> '*-*'
                 ORDER BY count DESC, value ASC
                """,
                [canonical, latest, inp.min_count],
            ).fetchall()
        return TaxonomyResult(
            values=[TaxonomyEntry(value=r[0], count=int(r[1])) for r in rows],
            captured_at=latest,
        )
