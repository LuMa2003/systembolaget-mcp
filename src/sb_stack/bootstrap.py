"""Idempotent bootstrap: mark home stores + main store from env.

Called from the container entrypoint (init-bootstrap.sh) on every start,
and from the `sb-stack bootstrap` CLI. Safe to re-run: does nothing if
the stores table is empty (sync hasn't populated it yet) and otherwise
just sets is_home_store / is_main_store flags to match
`SB_STORE_SUBSET` / `SB_MAIN_STORE`.
"""

from __future__ import annotations

from typing import Any

from sb_stack.db import DB
from sb_stack.settings import Settings


def bootstrap_home_stores(settings: Settings, logger: Any | None = None) -> dict[str, int]:
    """Return a small counts dict so callers can log what happened."""
    flagged = 0
    main_flagged = 0
    with DB(settings).writer() as conn:
        # Clear previous flags, then flag the configured ones.
        conn.execute("UPDATE stores SET is_home_store = FALSE, is_main_store = FALSE")
        for site_id in settings.store_subset:
            res = conn.execute(
                "UPDATE stores SET is_home_store = TRUE WHERE site_id = ?",
                [site_id],
            )
            if getattr(res, "rowcount", 0):
                flagged += 1
            elif logger is not None:
                logger.warning(
                    "bootstrap_store_not_found",
                    site_id=site_id,
                    hint="sync not run yet? stores table is empty until then",
                )
        if settings.main_store:
            conn.execute(
                "UPDATE stores SET is_main_store = TRUE WHERE site_id = ?",
                [settings.main_store],
            )
            main_flagged = 1
    counts = {"home_stores_flagged": flagged, "main_store_flagged": main_flagged}
    if logger is not None:
        logger.info("bootstrap_completed", **counts)
    return counts
