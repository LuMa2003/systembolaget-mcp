"""Phase A — fetch catalog + stores + stock + taxonomy, archive to raw.

Each sub-fetch writes its JSON payload to `/data/raw/YYYY-MM-DD/` BEFORE
we consider it "done", enforcing the "if a raw file exists, we saw the
response" invariant from docs/05_sync_pipeline.md.

Catalog partitioning (docs/05 §"Category partitioning") keeps every
partition under the 333-page cap.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sb_stack.api_client import SBApiClient
from sb_stack.errors import SystembolagetAPIError
from sb_stack.raw_archive import RawArchiveWriter
from sb_stack.sync.phase_types import (
    CatastrophicError,
    Phase,
    PhaseError,
    PhaseOutcome,
    PhaseResult,
)

# Partitioning strategy. Level 2 partitions for Vin to stay under the
# 333-page pagination cap; every other category fits in one partition.
CATALOG_PARTITIONS: tuple[tuple[str, str | None], ...] = (
    ("Vin", "Rött vin"),
    ("Vin", "Vitt vin"),
    ("Vin", "Mousserande vin"),
    ("Vin", "Rosévin"),
    ("Vin", "Aperitif & dessertvin"),
    ("Öl", None),
    ("Sprit", None),
    ("Cider & blanddrycker", None),
    ("Alkoholfritt", None),
    ("Presentartiklar", None),
)

PAGE_CAP = 333  # Systembolaget hard cap on productsearch/search pagination.


async def run_phase_a(
    *,
    api: SBApiClient,
    archive: RawArchiveWriter,
    home_store_ids: list[str],
    logger: Any,
) -> tuple[PhaseResult, list[dict[str, Any]]]:
    """Run Phase A; return (result, full-catalog product list)."""
    t0 = time.monotonic()
    errors: list[PhaseError] = []
    counts = {
        "catalog_pages": 0,
        "catalog_products": 0,
        "stock_pages": 0,
        "stores_fetched": 0,
        "taxonomy_fetched": 0,
    }

    all_products: list[dict[str, Any]] = []

    # 1. Stores — critical. Fail → catastrophic.
    try:
        stores = await api.site_stores()
        await archive.write_stores(stores)
        counts["stores_fetched"] = len(stores)
        logger.info("stores_fetched", stores_count=len(stores))
    except Exception as e:
        raise CatastrophicError(f"stores fetch failed: {e}") from e

    # 2. Taxonomy — best-effort.
    try:
        taxonomy = await api.productsearch_filter()
        await archive.write_taxonomy(taxonomy)
        counts["taxonomy_fetched"] = 1
    except SystembolagetAPIError as e:
        errors.append(PhaseError(f"taxonomy fetch failed: {e}", cause=e))
        logger.warning("taxonomy_fetch_failed", error=str(e))

    # 3. Catalog, partitioned.
    for cat1, cat2 in CATALOG_PARTITIONS:
        label = cat1 if cat2 is None else f"{cat1}_{cat2}"
        try:
            products = await _fetch_catalog_partition(
                api=api,
                archive=archive,
                category_level_1=cat1,
                category_level_2=cat2,
                label=label,
                counts=counts,
                logger=logger,
            )
        except SystembolagetAPIError as e:
            errors.append(PhaseError(f"catalog partition {label!r} failed: {e}", cause=e))
            logger.warning("catalog_partition_failed", partition=label, error=str(e))
            continue
        all_products.extend(products)

    if counts["catalog_pages"] == 0:
        raise CatastrophicError("no catalog pages fetched")

    # 4. Per-home-store stock. Partial errors are recoverable.
    for site_id in home_store_ids:
        try:
            await _fetch_stock_for_store(
                api=api,
                archive=archive,
                site_id=site_id,
                counts=counts,
                logger=logger,
            )
        except SystembolagetAPIError as e:
            errors.append(PhaseError(f"stock fetch failed for {site_id}: {e}", cause=e))
            logger.warning("stock_fetch_failed", site_id=site_id, error=str(e))

    counts["catalog_products"] = len(all_products)

    outcome = PhaseOutcome.PARTIAL if errors else PhaseOutcome.OK
    return (
        PhaseResult(
            phase=Phase.FETCH,
            outcome=outcome,
            duration_ms=int((time.monotonic() - t0) * 1000),
            counts=counts,
            errors=errors,
            summary=(
                f"{counts['catalog_pages']} catalog pages, "
                f"{counts['stock_pages']} stock pages, {len(errors)} errors"
            ),
        ),
        all_products,
    )


async def _fetch_catalog_partition(
    *,
    api: SBApiClient,
    archive: RawArchiveWriter,
    category_level_1: str,
    category_level_2: str | None,
    label: str,
    counts: dict[str, int],
    logger: Any,
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    page = 1
    while page <= PAGE_CAP:
        t0 = time.monotonic()
        body = await api.search_catalog(
            category_level_1=category_level_1,
            category_level_2=category_level_2,
            page=page,
            size=30,
        )
        await archive.write_catalog_page(label, page, body)
        items = list(body.get("products", []) or [])
        products.extend(items)
        counts["catalog_pages"] += 1
        logger.debug(
            "catalog_page_fetched",
            category=label,
            page=page,
            items_count=len(items),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        next_page = body.get("metadata", {}).get("nextPage")
        # Systembolaget flips nextPage to -1 past the real last page.
        if not items or (isinstance(next_page, int) and next_page < 0):
            break
        page += 1
    return products


async def _fetch_stock_for_store(
    *,
    api: SBApiClient,
    archive: RawArchiveWriter,
    site_id: str,
    counts: dict[str, int],
    logger: Any,
) -> None:
    page = 1
    while page <= PAGE_CAP:
        t0 = time.monotonic()
        body = await api.mobile_search_stock(store_id=site_id, page=page, size=30)
        await archive.write_stock_page(site_id, page, body)
        counts["stock_pages"] += 1
        items = list(body.get("products", []) or [])
        logger.debug(
            "stock_page_fetched",
            site_id=site_id,
            page=page,
            items_count=len(items),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        next_page = body.get("metadata", {}).get("nextPage")
        if not items or (isinstance(next_page, int) and next_page < 0):
            break
        page += 1
    # Small yield so the concurrency limiter releases before the next store.
    await asyncio.sleep(0)
