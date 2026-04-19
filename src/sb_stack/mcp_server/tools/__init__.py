"""Registered MCP tools. Each module defines one FastMCP tool function."""

from sb_stack.mcp_server.tools import (
    compare_products,
    find_similar_products,
    get_product,
    get_store_schedule,
    list_home_stores,
    list_taxonomy_values,
    pair_with_dish,
    search_products,
    semantic_search,
    sync_status,
)


def register_all(server: object) -> None:
    """Register every tool on the given FastMCP server instance.

    Each tool module exposes `register(server)` that attaches its tool.
    """
    for module in (
        search_products,
        semantic_search,
        find_similar_products,
        pair_with_dish,
        get_product,
        compare_products,
        list_home_stores,
        get_store_schedule,
        list_taxonomy_values,
        sync_status,
    ):
        module.register(server)
