"""Phase C — fetch product detail for every product whose hash changed.

Concurrency-limited via the API client's built-in semaphore. Failed
detail fetches are non-fatal — the product keeps its previously-known
fields and the run marks partial.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sb_stack.api_client import SBApiClient
from sb_stack.db import DB
from sb_stack.errors import SystembolagetAPIError
from sb_stack.raw_archive import RawArchiveWriter
from sb_stack.sync.phase_types import (
    Phase,
    PhaseError,
    PhaseOutcome,
    PhaseResult,
)
from sb_stack.sync.product_mapper import map_product


async def run_phase_c(
    *,
    api: SBApiClient,
    db: DB,
    archive: RawArchiveWriter,
    product_numbers: list[str],
    concurrency: int,
    logger: Any,
) -> PhaseResult:
    t0 = time.monotonic()
    if not product_numbers:
        return PhaseResult(
            phase=Phase.DETAILS,
            outcome=PhaseOutcome.SKIPPED,
            counts={"fetched": 0, "failed": 0},
            summary="no products to fetch",
        )

    sem = asyncio.Semaphore(concurrency)
    errors: list[PhaseError] = []
    fetched = 0
    lock = asyncio.Lock()

    async def _one(pn: str) -> None:
        nonlocal fetched
        async with sem:
            try:
                detail = await api.product_by_number(pn)
            except SystembolagetAPIError as e:
                errors.append(PhaseError(f"detail fetch failed for {pn}: {e}", cause=e))
                logger.warning("detail_fetch_failed", product_number=pn, error=str(e))
                return
            await archive.write_detail(pn, detail)
            async with lock:
                # Each detail merge is its own short UPDATE outside a bigger
                # transaction — one bad row (unexpected API type) must not
                # kill the whole phase. Swallow + log; Phase outcome drops
                # to PARTIAL via the errors list.
                try:
                    _merge_detail_into_product(db, pn, detail)
                except Exception as e:  # noqa: BLE001
                    errors.append(PhaseError(f"detail merge failed for {pn}: {e}", cause=e))
                    logger.warning("detail_merge_failed", product_number=pn, error=str(e))
                    return
                fetched += 1
                logger.debug("detail_fetched", product_number=pn)

    await asyncio.gather(*[_one(pn) for pn in product_numbers])

    outcome = PhaseOutcome.PARTIAL if errors else PhaseOutcome.OK
    return PhaseResult(
        phase=Phase.DETAILS,
        outcome=outcome,
        duration_ms=int((time.monotonic() - t0) * 1000),
        counts={"fetched": fetched, "failed": len(errors)},
        errors=errors,
        summary=f"{fetched} fetched, {len(errors)} failed",
    )


def _merge_detail_into_product(db: DB, pn: str, detail: dict[str, Any]) -> None:
    """Overlay detail-only fields onto the existing products row.

    The detail endpoint returns the same shape as search plus richer text
    fields (aroma, usage, producer_description, etc.). We UPDATE only the
    columns actually present in the payload to avoid blanking fields the
    catalog response already populated.
    """
    row = map_product(detail)
    row.pop("product_number", None)
    if not row:
        return
    set_cols = list(row.keys())
    set_sql = ", ".join(f"{c} = ?" for c in set_cols)
    with db.writer() as conn:
        conn.execute(
            f"UPDATE products SET {set_sql} WHERE product_number = ?",
            [*row.values(), pn],
        )
