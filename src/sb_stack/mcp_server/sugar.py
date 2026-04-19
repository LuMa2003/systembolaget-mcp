"""Site-id sugar resolution shared by every tool that accepts a store param.

"main"       → settings.main_store
"home"       → every settings.store_subset
"<siteId>"   → single-element list [<siteId>]
None / ""    → [] (tool-specific default applies)
"""

from __future__ import annotations

from sb_stack.settings import Settings


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
