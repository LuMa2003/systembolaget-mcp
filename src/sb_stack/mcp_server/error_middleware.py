"""FastMCP middleware that makes every user-facing tool error Swedish.

Two jobs:
  1. Translate pydantic input-validation failures (English, with `inp.` prefixes
     and an errors.pydantic.dev URL) into a clean Swedish message.
  2. Surface our domain `MCPError`s (already Swedish) as a bare ToolError without
     FastMCP's English "Error calling tool 'X':" wrapper.

Project rule (CLAUDE.md): all end-user text is Swedish; code/logs stay English.
"""

from __future__ import annotations

import re
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import types as mt
from pydantic import ValidationError

from sb_stack.errors import MCPError

_PREFIX_RE = re.compile(r"^Error calling tool '[^']*':\s*")

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
    fixed = {
        "missing": "är obligatoriskt",
        "missing_argument": "är obligatoriskt",
        "string_too_short": "är för kort",
        "unexpected_keyword_argument": "är okänt",
    }
    if t in fixed:
        return fixed[t]
    templated = {
        "greater_than_equal": f"måste vara minst {ctx.get('ge')}",
        "less_than_equal": f"får vara högst {ctx.get('le')}",
        "greater_than": f"måste vara större än {ctx.get('gt')}",
        "less_than": f"måste vara mindre än {ctx.get('lt')}",
        "too_short": f"kräver minst {ctx.get('min_length', ctx.get('actual_length'))} värden",
        "too_long": f"får ha högst {ctx.get('max_length')} värden",
    }
    if t in templated:
        return templated[t]
    if t == "value_error":
        # Our own field_validators raise Swedish ValueErrors; reuse the text.
        msg = str(err.get("msg", ""))
        return msg.replace("Value error, ", "").strip() or "är ogiltigt"
    return "är ogiltigt"


def _to_swedish(exc: ValidationError) -> str:
    uniq: list[str] = []
    for e in exc.errors():
        reason = _reason(dict(e))
        # value_error messages (our own field/model validators) are already a
        # complete Swedish sentence — don't prefix them with the field label.
        part = reason if e.get("type") == "value_error" else f"{_field_name(e['loc'])} {reason}"
        if part not in uniq:
            uniq.append(part)
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
        except ToolError as exc:
            # FastMCP wraps tool-raised exceptions (incl. our Swedish MCPError)
            # as "Error calling tool 'X': <msg>". Strip that English prefix so
            # the user sees only the Swedish message.
            raise ToolError(_PREFIX_RE.sub("", str(exc))) from exc
