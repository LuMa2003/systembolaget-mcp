"""Shared app context handed to every MCP tool.

Tools are module-level functions decorated by FastMCP's @tool; they reach
shared state (DB, settings, embed client) via `get_context()`. Set once
by `server.create_server` at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sb_stack.db import DB
from sb_stack.embed import EmbeddingClient
from sb_stack.settings import Settings


@dataclass
class AppContext:
    settings: Settings
    db: DB
    embed_client: EmbeddingClient | None
    logger: Any


_context: AppContext | None = None


def set_context(ctx: AppContext) -> None:
    global _context  # noqa: PLW0603 — singleton-by-design
    _context = ctx


def get_context() -> AppContext:
    if _context is None:
        raise RuntimeError("MCP context not initialised — did you call create_server()?")
    return _context


def reset_context() -> None:
    """Test-only hook so fixtures can swap in a fresh context."""
    global _context  # noqa: PLW0603
    _context = None
