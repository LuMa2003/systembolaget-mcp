"""FastMCP server assembly + CLI entry for `sb-stack mcp`.

Wires the shared AppContext, registers the 10 tools, and (for HTTP
transport) mounts bearer-token auth. StaticTokenVerifier is the simplest
first-party option in fastmcp 3 — we accept a single static token from
`SB_MCP_TOKEN`.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier

from sb_stack.db import DB, MigrationRunner
from sb_stack.embed import EmbeddingClient
from sb_stack.logging import configure_logging, get_logger
from sb_stack.mcp_server.context import AppContext, set_context
from sb_stack.mcp_server.tools import register_all
from sb_stack.settings import Settings


def _auth_for_http(settings: Settings) -> StaticTokenVerifier | None:
    if not settings.mcp_token:
        raise RuntimeError(
            "SB_MCP_TOKEN must be set when SB_MCP_TRANSPORT=http "
            "(fail-closed to avoid an unauth'd server on the LAN)."
        )
    return StaticTokenVerifier(
        tokens={
            settings.mcp_token: {
                "client_id": "sb-stack-client",
                "scopes": [],
            }
        }
    )


def create_server(settings: Settings) -> FastMCP:
    """Build the FastMCP server + AppContext and register every tool."""
    # sb-mcp refuses to serve against an unmigrated DB.
    log = get_logger("sb_stack.mcp_server")
    MigrationRunner(DB(settings), settings, log).verify()

    embed_client = EmbeddingClient(
        url=settings.embed_url,
        model=settings.embed_model,
        client_batch_size=settings.embed_client_batch_size,
        logger=log,
    )

    ctx = AppContext(
        settings=settings,
        db=DB(settings),
        embed_client=embed_client,
        logger=log,
    )
    set_context(ctx)

    auth: Any = None
    if settings.mcp_transport == "http":
        auth = _auth_for_http(settings)

    server = FastMCP(
        name="sb-stack",
        version="0.1.0",
        auth=auth,
    )
    register_all(server)
    log.info("mcp_server_tools_registered", count=10)
    return server


def run() -> None:
    """CLI entrypoint used by `sb-stack mcp`."""
    from sb_stack.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings, process_name="sb-mcp")
    log = get_logger("sb_stack.mcp_server")
    log.info(
        "mcp_server_starting",
        transport=settings.mcp_transport,
        port=settings.mcp_port,
    )
    server = create_server(settings)
    log.info("mcp_server_started")
    if settings.mcp_transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="http", port=settings.mcp_port, host="0.0.0.0")  # noqa: S104
