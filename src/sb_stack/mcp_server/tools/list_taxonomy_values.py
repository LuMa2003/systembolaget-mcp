"""`list_taxonomy_values` — enumerate valid filter values from the daily snapshot."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.responses import TaxonomyEntry, TaxonomyResult

_DESCRIPTION = (
    "Lista giltiga värden för ett sökfilter, t.ex. alla länder i sortimentet, "
    "alla kapsyltyper, alla matsymboler. Använd innan search_products om du "
    "är osäker på exakta värden."
)


class TaxonomyInput(BaseModel):
    filter_name: str
    min_count: int = Field(default=1, ge=0)


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def list_taxonomy_values(inp: TaxonomyInput) -> TaxonomyResult:
        ctx = get_context()
        with ctx.db.reader() as conn:
            latest_row = conn.execute(
                "SELECT MAX(captured_at) FROM filter_taxonomy WHERE filter_name = ?",
                [inp.filter_name],
            ).fetchone()
            latest = latest_row[0] if latest_row else None
            if latest is None:
                return TaxonomyResult(values=[], captured_at=None)
            rows = conn.execute(
                """
                SELECT value, count
                  FROM filter_taxonomy
                 WHERE filter_name = ? AND captured_at = ? AND count >= ?
                 ORDER BY count DESC, value ASC
                """,
                [inp.filter_name, latest, inp.min_count],
            ).fetchall()
        return TaxonomyResult(
            values=[TaxonomyEntry(value=r[0], count=int(r[1])) for r in rows],
            captured_at=latest,
        )
