"""Site-id sugar resolution shared by every tool that accepts a store param.

"main"       → settings.main_store
"home"       → every settings.store_subset
"<siteId>"   → single-element list [<siteId>]
None / ""    → [] (tool-specific default applies)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sb_stack.errors import UnknownStoreError
from sb_stack.settings import Settings

if TYPE_CHECKING:
    import duckdb


def resolve_site_ids(value: str | None, settings: Settings) -> list[str]:
    if not value:
        return []
    v = value.strip().lower()
    if v == "main":
        return [settings.main_store]
    if v == "home":
        return list(settings.store_subset)
    # Concrete siteId (keep original casing of the returned value).
    return [value.strip()]


def assert_stores_exist(conn: duckdb.DuckDBPyConnection, site_ids: list[str]) -> None:
    """Raise a Swedish UnknownStoreError for the first site_id absent from `stores`.

    Tools that accept a store parameter call this after resolve_site_ids so a
    typo'd / unknown store fails loudly instead of silently returning 0 rows.
    """
    for sid in site_ids:
        row = conn.execute("SELECT 1 FROM stores WHERE site_id = ? LIMIT 1", [sid]).fetchone()
        if row is None:
            raise UnknownStoreError(sid)
