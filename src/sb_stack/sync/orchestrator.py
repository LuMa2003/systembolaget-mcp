"""Single entrypoint that wires Phase A→F together.

Every caller (scheduler, manual CLI, bootstrap) goes through `run_sync`.
See docs/10_sync_orchestration.md for full design including replay and
dry-run modes — both are lighter-weight variants that reuse the phase
building blocks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sb_stack.api_client import SBApiClient
from sb_stack.db import DB, MigrationRunner
from sb_stack.embed import EmbeddingClient
from sb_stack.errors import SBError
from sb_stack.raw_archive import RawArchiveReader, RawArchiveWriter
from sb_stack.settings import Settings
from sb_stack.sync.lockfile import Lockfile, LockfileBusyError
from sb_stack.sync.phase_types import (
    CatastrophicError,
    Phase,
    PhaseOutcome,
    PhaseResult,
    overall_status,
)
from sb_stack.sync.phases.details import run_phase_c
from sb_stack.sync.phases.embed import run_phase_d
from sb_stack.sync.phases.fetch import run_phase_a
from sb_stack.sync.phases.finalize import run_phase_f
from sb_stack.sync.phases.index import run_phase_e
from sb_stack.sync.phases.persist import run_phase_b


@dataclass
class SyncRunResult:
    run_id: int
    status: str
    phase_results: list[PhaseResult]
    duration_ms: int


async def run_sync(  # noqa: PLR0915 — the phase sequencer is deliberately linear.
    *,
    settings: Settings,
    db: DB,
    api: SBApiClient,
    embed_client: EmbeddingClient,
    logger: Any,
    full_refresh: bool = False,
    reason: str = "manual",
) -> SyncRunResult:
    """Execute one end-to-end sync run. Returns a summary result.

    Raises `LockfileBusyError` if another run is in progress. Catastrophic
    errors propagate up; phase-level failures are recorded as partial and
    the run still reaches Phase F.
    """
    t_start = time.monotonic()
    MigrationRunner(db, settings, logger).run()

    lockfile = Lockfile(settings.state_dir / "lockfile", logger=logger)
    lockfile.acquire()
    try:
        run_id = _start_run_row(db, reason=reason, full_refresh=full_refresh)
        logger.info(
            "sync_run_started",
            run_id=run_id,
            reason=reason,
            full_refresh=full_refresh,
        )

        today = date.today()
        archive = RawArchiveWriter(settings.raw_dir, today)
        reader = RawArchiveReader(settings.raw_dir, today)
        home_stores = list(settings.store_subset)
        phase_results: list[PhaseResult] = []

        try:
            # Phase A
            logger.info("phase_started", run_id=run_id, phase=Phase.FETCH.value)
            phase_a, catalog_products = await run_phase_a(
                api=api,
                archive=archive,
                home_store_ids=home_stores,
                logger=logger,
            )
            phase_results.append(phase_a)
            logger.info(
                "phase_finished",
                run_id=run_id,
                phase=Phase.FETCH.value,
                outcome=phase_a.outcome.value,
                duration_ms=phase_a.duration_ms,
                counts=phase_a.counts,
            )

            # Phase B
            logger.info("phase_started", run_id=run_id, phase=Phase.PERSIST.value)
            phase_b = run_phase_b(
                db=db,
                raw=reader,
                home_store_ids=home_stores,
                phase_a_ok=phase_a.outcome == PhaseOutcome.OK,
                logger=logger,
            )
            phase_results.append(phase_b)
            logger.info(
                "phase_finished",
                run_id=run_id,
                phase=Phase.PERSIST.value,
                outcome=phase_b.outcome.value,
                duration_ms=phase_b.duration_ms,
                counts=phase_b.counts,
            )

            # Phase C — only products we just added or updated.
            changed = _changed_product_numbers(db, phase_b.counts, full_refresh)
            logger.info("phase_started", run_id=run_id, phase=Phase.DETAILS.value)
            phase_c = await run_phase_c(
                api=api,
                db=db,
                archive=archive,
                product_numbers=changed,
                concurrency=settings.sync_concurrency,
                logger=logger,
            )
            phase_results.append(phase_c)
            logger.info(
                "phase_finished",
                run_id=run_id,
                phase=Phase.DETAILS.value,
                outcome=phase_c.outcome.value,
                duration_ms=phase_c.duration_ms,
                counts=phase_c.counts,
            )

            # Phase D — candidates = changed + newly-detailed products.
            logger.info("phase_started", run_id=run_id, phase=Phase.EMBED.value)
            phase_d = await run_phase_d(
                db=db,
                settings=settings,
                embed_client=embed_client,
                product_numbers=changed,
                full_refresh=full_refresh,
                logger=logger,
            )
            phase_results.append(phase_d)
            logger.info(
                "phase_finished",
                run_id=run_id,
                phase=Phase.EMBED.value,
                outcome=phase_d.outcome.value,
                duration_ms=phase_d.duration_ms,
                counts=phase_d.counts,
            )

            # Phase E — FTS rebuild iff anything changed.
            products_touched = (
                phase_b.counts.get("products_added", 0)
                + phase_b.counts.get("products_updated", 0)
                + phase_b.counts.get("products_discontinued", 0)
                + phase_c.counts.get("fetched", 0)
            )
            logger.info("phase_started", run_id=run_id, phase=Phase.INDEX.value)
            phase_e = await run_phase_e(
                db=db,
                products_touched=products_touched,
                logger=logger,
            )
            phase_results.append(phase_e)
            logger.info(
                "phase_finished",
                run_id=run_id,
                phase=Phase.INDEX.value,
                outcome=phase_e.outcome.value,
                duration_ms=phase_e.duration_ms,
                counts=phase_e.counts,
            )
        except CatastrophicError as e:
            logger.error(
                "sync_run_catastrophic",
                run_id=run_id,
                error=str(e),
                alert=True,
            )
            phase_results.append(
                PhaseResult(
                    phase=Phase.FETCH,
                    outcome=PhaseOutcome.CATASTROPHIC,
                    summary=str(e),
                )
            )
        finally:
            # Phase F ALWAYS runs.
            logger.info("phase_started", run_id=run_id, phase=Phase.FINALIZE.value)
            phase_f = run_phase_f(
                db=db,
                settings=settings,
                run_id=run_id,
                phase_results=phase_results,
                logger=logger,
            )
            phase_results.append(phase_f)
            logger.info(
                "phase_finished",
                run_id=run_id,
                phase=Phase.FINALIZE.value,
                outcome=phase_f.outcome.value,
                duration_ms=phase_f.duration_ms,
            )

        status = overall_status(phase_results)
        total_ms = int((time.monotonic() - t_start) * 1000)
        logger.info(
            "sync_run_finished",
            run_id=run_id,
            status=status,
            duration_ms=total_ms,
        )
        return SyncRunResult(
            run_id=run_id,
            status=status,
            phase_results=phase_results,
            duration_ms=total_ms,
        )
    finally:
        lockfile.release()


# ── Helpers ────────────────────────────────────────────────────────────


def _start_run_row(db: DB, *, reason: str, full_refresh: bool) -> int:
    """INSERT a sync_runs row; return the allocated run_id."""
    now = datetime.now(UTC)
    note = "full_refresh" if full_refresh else reason
    with db.writer() as conn:
        seq_row = conn.execute("SELECT nextval('sync_run_id_seq')").fetchone()
        assert seq_row is not None, "sync_run_id_seq returned no value"
        run_id = int(seq_row[0])
        conn.execute(
            """
            INSERT INTO sync_runs (run_id, started_at, status, error)
            VALUES (?, ?, 'running', ?)
            """,
            [run_id, now, note],
        )
    return run_id


def _changed_product_numbers(
    db: DB, persist_counts: dict[str, int], full_refresh: bool
) -> list[str]:
    """Products that need Phase C+D attention."""
    if full_refresh:
        with db.reader() as conn:
            rows = conn.execute(
                "SELECT product_number FROM products WHERE is_discontinued IS NOT TRUE"
            ).fetchall()
            return [r[0] for r in rows]
    # Heuristic for the thin pipeline: fetch new + recently-updated rows.
    touched = persist_counts.get("products_added", 0) + persist_counts.get("products_updated", 0)
    if touched == 0:
        return []
    with db.reader() as conn:
        rows = conn.execute(
            """
            SELECT product_number FROM products
             WHERE last_fetched_at = (SELECT MAX(last_fetched_at) FROM products)
            """
        ).fetchall()
        return [r[0] for r in rows]


__all__ = ["SyncRunResult", "run_sync", "LockfileBusyError", "SBError"]
