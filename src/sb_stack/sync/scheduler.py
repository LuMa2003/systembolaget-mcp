"""APScheduler-backed cron driver for `sb-stack sync-scheduler`.

Long-running service. Waits for sb-embed, checks for a cold-start "first
run", then registers the SB_SYNC_CRON trigger. Coalesces missed fires
and uses the DB-level lockfile (via run_sync) as the real concurrency
guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from sb_stack.api_client import SBApiClient, extract_config
from sb_stack.db import DB
from sb_stack.embed import EmbeddingClient, wait_for_embed_ready
from sb_stack.settings import Settings
from sb_stack.sync.lockfile import LockfileBusyError
from sb_stack.sync.orchestrator import run_sync


async def run_scheduler(*, settings: Settings, db: DB, logger: Any) -> None:
    logger.info(
        "sync_scheduler_starting",
        cron=settings.sync_cron,
        timezone=settings.sync_timezone,
    )

    api_key = await _get_api_key(settings, logger)

    async with (
        SBApiClient(
            api_key=api_key,
            base_url=settings.api_base_url,
            app_base_url=settings.app_base_url,
            max_concurrent=settings.sync_concurrency,
            logger=logger,
        ) as api,
        EmbeddingClient(
            url=settings.embed_url, model=settings.embed_model, logger=logger
        ) as embed_client,
    ):
        # Wait for sb-embed before any run.
        # 30 min so first-boot has time to pull the ~8 GB Qwen3 model
        # before the scheduler gives up.
        await wait_for_embed_ready(embed_client, timeout_s=1800, logger=logger)

        if settings.first_run_on_bootstrap and _needs_first_run(db):
            logger.info("first_run_starting")
            await _run_with_logging(
                settings=settings,
                db=db,
                api=api,
                embed_client=embed_client,
                logger=logger,
                reason="first_run_bootstrap",
                full_refresh=True,
            )

        scheduler = AsyncIOScheduler(timezone=settings.sync_timezone)
        scheduler.add_job(
            _run_with_logging,
            trigger=CronTrigger.from_crontab(settings.sync_cron, timezone=settings.sync_timezone),
            kwargs={
                "settings": settings,
                "db": db,
                "api": api,
                "embed_client": embed_client,
                "logger": logger,
                "reason": "cron",
                "full_refresh": False,
            },
            id="sync",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()

        job = scheduler.get_job("sync")
        logger.info(
            "sync_scheduler_started",
            next_fire=str(job.next_run_time) if job else None,
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Signal handlers aren't available in some test environments.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        logger.info("sync_scheduler_stopping")
        scheduler.shutdown(wait=True)


async def _run_with_logging(
    *,
    settings: Settings,
    db: DB,
    api: SBApiClient,
    embed_client: EmbeddingClient,
    logger: Any,
    reason: str,
    full_refresh: bool,
) -> None:
    t0 = time.monotonic()
    try:
        result = await run_sync(
            settings=settings,
            db=db,
            api=api,
            embed_client=embed_client,
            logger=logger,
            full_refresh=full_refresh,
            reason=reason,
        )
        logger.info(
            "sync_run_finished_logged",
            reason=reason,
            status=result.status,
            duration_s=int(time.monotonic() - t0),
        )
    except LockfileBusyError as e:
        logger.warning("sync_run_skipped_locked", reason=reason, error=str(e))
    except Exception as e:
        logger.error("sync_run_unhandled", reason=reason, error=repr(e), alert=True)


def _needs_first_run(db: DB) -> bool:
    with db.reader() as conn:
        row = conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()
    return int(row[0] if row else 0) == 0


async def _get_api_key(settings: Settings, logger: Any) -> str:
    if settings.api_key:
        logger.info("api_key_from_env", key_prefix=settings.api_key[:8])
        return settings.api_key
    cfg = await extract_config(app_base_url=settings.app_base_url, logger=logger)
    return cfg.api_key
