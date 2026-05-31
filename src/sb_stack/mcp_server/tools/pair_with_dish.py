"""`pair_with_dish` — hands off to `sb_stack.pairing.engine.PairingEngine`."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.responses import (
    PairingInterpretation,
    PairingRecommendation,
    PairWithDishResult,
    ScoreBreakdown,
)
from sb_stack.mcp_server.sugar import resolve_site_ids
from sb_stack.pairing import PairingEngine

_DESCRIPTION = (
    "Föreslår drycker som passar till en beskriven maträtt. Tolkar rätten "
    "till matsymboler (Fisk, Fläsk, Nöt, Ost, …) och en kroppsprofil, och "
    "rangordnar mot Systembolagets sommeliertexter (fältet usage). "
    "Använd meal_context för t.ex. «något kraftigt» eller «lätt och fräscht»."
)


class PairInput(BaseModel):
    dish: str = Field(min_length=1)
    meal_context: str | None = None
    taste_symbols_hint: list[str] | None = None
    budget_max: float | None = Field(default=None, ge=0)
    in_stock_at: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    async def pair_with_dish(
        dish: str,
        meal_context: str | None = None,
        taste_symbols_hint: list[str] | None = None,
        budget_max: float | None = None,
        in_stock_at: str | None = None,
        limit: int = 5,
    ) -> PairWithDishResult:
        inp = PairInput(
            dish=dish,
            meal_context=meal_context,
            taste_symbols_hint=taste_symbols_hint,
            budget_max=budget_max,
            in_stock_at=in_stock_at,
            limit=limit,
        )
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
        result = await engine.pair(
            dish=inp.dish,
            meal_context=inp.meal_context,
            taste_symbols_hint=inp.taste_symbols_hint,
            site_ids_in_stock=site_ids,
            budget_max=inp.budget_max,
            limit=inp.limit,
        )

        interpretation = None
        if result.interpretation is not None:
            i = result.interpretation
            interpretation = PairingInterpretation(
                dish_summary=i.dish_summary,
                inferred_taste_symbols=i.inferred_taste_symbols,
                inferred_profile=i.inferred_profile,
                sommelier_reasoning=i.sommelier_reasoning,
            )

        return PairWithDishResult(
            dish=inp.dish,
            confidence=result.confidence,
            recommendations=[
                PairingRecommendation(
                    product=r.product,
                    similarity=round(r.similarity, 4),
                    why=r.why,
                    score_breakdown=ScoreBreakdown(
                        usage_similarity=r.breakdown.usage_similarity,
                        taste_clock_fit=r.breakdown.taste_clock_fit,
                        symbol_match=r.breakdown.symbol_match,
                        total=r.breakdown.total,
                    ),
                )
                for r in result.recommendations
            ],
            interpretation=interpretation,
        )
