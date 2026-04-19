"""`pair_with_dish` — stub until the pairing engine (Step 7) lands.

The tool is registered so the MCP surface is complete; calls return a
gentle Swedish message pointing the LLM at the current alternatives.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sb_stack.mcp_server.responses import PairWithDishResult

_DESCRIPTION = (
    "Föreslår drycker som passar till en beskriven maträtt. Tolkar maten "
    "i tre lager: (1) semantisk matchning mot Systembolagets sommeliertexter "
    "om varje produkt, (2) smakklocke- och matsymbolanalys, "
    "(3) regional/traditionell affinitet. "
    "(OBS: pairing-motorn är under utveckling; använd semantic_search eller "
    "search_products med pairs_with_any under tiden.)"
)


class PairInput(BaseModel):
    dish: str
    meal_context: str | None = None
    style_preference: str | None = None
    cultural_tag: str | None = None
    budget_max: float | None = None
    in_stock_at: str | None = None


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def pair_with_dish(inp: PairInput) -> PairWithDishResult:  # noqa: ARG001
        return PairWithDishResult()
