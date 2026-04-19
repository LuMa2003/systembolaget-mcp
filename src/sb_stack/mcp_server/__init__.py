"""MCP server subpackage — FastMCP app exposing 10 read-only tools.

See docs/04_mcp_surface.md for the tool contract; docs/08_mcp_implementation.md
for per-tool SQL notes.
"""

from sb_stack.mcp_server.server import create_server

__all__ = ["create_server"]
