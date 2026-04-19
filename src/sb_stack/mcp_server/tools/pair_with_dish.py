"""`pair_with_dish` — hands off to `sb_stack.pairing.engine.PairingEngine`."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.responses import (
    PairingRecommendation,
    PairWithDishResult,
)
from sb_stack.mcp_server.sugar import resolve_site_ids
from sb_stack.pairing import PairingEngine

_DESCRIPTION = (
    "Föreslår drycker som passar till en beskriven maträtt. Använder "
    "semantisk matchning mot Systembolagets sommeliertexter (fältet usage) "
    "och biaserar valfritt på matsymboler (Fisk, Kött, Skaldjur, …)."
)


class PairInput(BaseModel):
    dish: str
    meal_context: str | None = None
    taste_symbols_hint: list[str] | None = None
    budget_max: float | None = None
    in_stock_at: str | None = None
    limit: int = 5


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    async def pair_with_dish(inp: PairInput) -> PairWithDishResult:
        ctx = get_context()
        if ctx.embed_client is None:
            return PairWithDishResult(
                dish=inp.dish,
                notes=(
                    "Embedding-tjänsten är inte konfigurerad — pairing kräver semantisk retrieval."
                ),
            )

        engine = PairingEngine(
            settings=ctx.settings,
            db=ctx.db,
            embed_client=ctx.embed_client,
        )
        site_ids = resolve_site_ids(inp.in_stock_at, ctx.settings) or None
        recs, confidence = await engine.pair(
            dish=inp.dish,
            meal_context=inp.meal_context,
            taste_symbols_hint=inp.taste_symbols_hint,
            site_ids_in_stock=site_ids,
            budget_max=inp.budget_max,
            limit=inp.limit,
        )
        return PairWithDishResult(
            dish=inp.dish,
            confidence=confidence,
            recommendations=[
                PairingRecommendation(
                    product=r.product,
                    similarity=round(r.similarity, 4),
                    why=r.why,
                )
                for r in recs
            ],
        )
