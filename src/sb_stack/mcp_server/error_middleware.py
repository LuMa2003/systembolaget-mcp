"""FastMCP middleware that makes every user-facing tool error Swedish.

Two jobs:
  1. Translate pydantic input-validation failures (English, with `inp.` prefixes
     and an errors.pydantic.dev URL) into a clean Swedish message.
  2. Surface our domain `MCPError`s (already Swedish) as a bare ToolError without
     FastMCP's English "Error calling tool 'X':" wrapper.

Project rule (CLAUDE.md): all end-user text is Swedish; code/logs stay English.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import types as mt
from pydantic import ValidationError

from sb_stack.errors import MCPError

_FIELD_LABELS = {
    "limit": "limit",
    "offset": "offset",
    "days_ahead": "antal dagar",
    "product_numbers": "produktnummer",
    "query": "sökfråga",
    "dish": "maträtt",
    "filter_name": "filternamn",
    "min_count": "minsta antal",
}


def _field_name(loc: tuple[Any, ...]) -> str:
    """Last path segment, minus the FastMCP `inp` wrapper, in Swedish if known."""
    parts = [str(p) for p in loc if p != "inp"]
    raw = parts[-1] if parts else "fältet"
    return _FIELD_LABELS.get(raw, raw)


def _reason(err: dict[str, Any]) -> str:
    t = err.get("type", "")
    ctx = err.get("ctx", {}) or {}
    match t:
        case "missing" | "missing_argument":
            return "är obligatoriskt"
        case "greater_than_equal":
            return f"måste vara minst {ctx.get('ge')}"
        case "less_than_equal":
            return f"får vara högst {ctx.get('le')}"
        case "greater_than":
            return f"måste vara större än {ctx.get('gt')}"
        case "less_than":
            return f"måste vara mindre än {ctx.get('lt')}"
        case "too_short":
            return f"kräver minst {ctx.get('min_length', ctx.get('actual_length'))} värden"
        case "too_long":
            return f"får ha högst {ctx.get('max_length')} värden"
        case "string_too_short":
            return "är för kort"
        case "unexpected_keyword_argument":
            return "är okänt"
        case "value_error":
            # Our own field_validators raise Swedish ValueErrors; reuse the text.
            msg = err.get("msg", "")
            return msg.replace("Value error, ", "").strip() or "är ogiltigt"
        case _:
            return "är ogiltigt"


def _to_swedish(exc: ValidationError) -> str:
    parts = [f"{_field_name(e['loc'])} {_reason(e)}" for e in exc.errors()]
    # de-dup while preserving order
    seen: set[str] = set()
    uniq = [p for p in parts if not (p in seen or seen.add(p))]
    return "Ogiltig inmatning: " + "; ".join(uniq) + "."


class SwedishErrorMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        try:
            return await call_next(context)
        except ValidationError as exc:
            raise ToolError(_to_swedish(exc)) from exc
        except MCPError as exc:
            # Already-Swedish domain message; drop the English wrapper.
            raise ToolError(str(exc)) from exc
