"""Thin shims the `sb-stack sync` / `runs` / `run-info` commands call into.

Keeping typer wiring in cli/main.py and the actual logic here lets us
test these functions directly without spinning up CliRunner.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from sb_stack.api_client import SBApiClient, extract_config
from sb_stack.db import DB
from sb_stack.embed import EmbeddingClient
from sb_stack.logging import configure_logging, get_logger
from sb_stack.settings import Settings, get_settings
from sb_stack.sync.orchestrator import SyncRunResult, run_sync


def cli_sync(*, full_refresh: bool = False, reason: str = "manual") -> SyncRunResult:
    """Run a single sync end-to-end against the configured API."""
    settings = get_settings()
    configure_logging(settings, process_name="sb-sync")
    log = get_logger("sb_stack.sync")
    db = DB(settings)
    return asyncio.run(
        _run_once(
            settings=settings,
            db=db,
            logger=log,
            full_refresh=full_refresh,
            reason=reason,
        )
    )


async def _run_once(
    *,
    settings: Settings,
    db: DB,
    logger: Any,
    full_refresh: bool,
    reason: str,
) -> SyncRunResult:
    api_key = await _resolve_api_key(settings, logger)

    async def _refresh_key() -> str:
        cfg = await extract_config(app_base_url=settings.app_base_url, logger=logger)
        return cfg.api_key

    async with (
        SBApiClient(
            api_key=api_key,
            api_key_mobile=settings.api_key_mobile,
            base_url=settings.api_base_url,
            app_base_url=settings.app_base_url,
            max_concurrent=settings.sync_concurrency,
            logger=logger,
            # Only wire up refresh when we're using the extractor (env-
            # provided keys are user-managed; refreshing from the frontend
            # would overwrite the user's explicit choice). The refresh only
            # applies to the ecommerce key; mobile has no self-heal path.
            key_refresher=None if settings.api_key else _refresh_key,
        ) as api,
        EmbeddingClient(
            url=settings.embed_url,
            model=settings.embed_model,
            client_batch_size=settings.embed_client_batch_size,
            logger=logger,
        ) as embed_client,
    ):
        return await run_sync(
            settings=settings,
            db=db,
            api=api,
            embed_client=embed_client,
            logger=logger,
            full_refresh=full_refresh,
            reason=reason,
        )


async def _resolve_api_key(settings: Settings, logger: Any) -> str:
    if settings.api_key:
        return settings.api_key
    cfg = await extract_config(app_base_url=settings.app_base_url, logger=logger)
    return cfg.api_key


def cli_runs(limit: int = 20) -> list[dict[str, Any]]:
    settings = get_settings()
    db = DB(settings)
    with db.reader() as conn:
        rows = conn.execute(
            """
            SELECT run_id, started_at, finished_at, status,
                   products_added, products_updated, products_discontinued
              FROM sync_runs
             ORDER BY run_id DESC
             LIMIT ?
            """,
            [limit],
        ).fetchall()
    cols = [
        "run_id",
        "started_at",
        "finished_at",
        "status",
        "products_added",
        "products_updated",
        "products_discontinued",
    ]
    return [dict(zip(cols, r, strict=True)) for r in rows]


def cli_run_info(run_id: int) -> dict[str, Any] | None:
    settings = get_settings()
    db = DB(settings)
    with db.reader() as conn:
        row = conn.execute("SELECT * FROM sync_runs WHERE run_id = ?", [run_id]).fetchone()
        if row is None:
            return None
        run_cols = [d[0] for d in conn.description]
        run = dict(zip(run_cols, row, strict=True))

        phases = conn.execute(
            """
            SELECT phase, started_at, finished_at, outcome, counts, error_summary
              FROM sync_run_phases
             WHERE run_id = ?
             ORDER BY started_at
            """,
            [run_id],
        ).fetchall()
        phase_cols = [
            "phase",
            "started_at",
            "finished_at",
            "outcome",
            "counts",
            "error_summary",
        ]
        phase_rows = [dict(zip(phase_cols, r, strict=True)) for r in phases]
    run["phases"] = phase_rows
    # Make datetimes JSON-safe for callers that dump this to stdout.
    for k, v in list(run.items()):
        if isinstance(v, datetime):
            run[k] = v.isoformat()
    for ph in phase_rows:
        for k, v in list(ph.items()):
            if isinstance(v, datetime):
                ph[k] = v.isoformat()
    return run
