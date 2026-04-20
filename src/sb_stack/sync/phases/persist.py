"""Phase B — diff against existing products, write to DuckDB in one tx.

This is the one phase that holds a DuckDB writer for the whole duration.
MCP readers stay on the pre-sync snapshot until commit.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import Any

import duckdb

from sb_stack.db import DB
from sb_stack.raw_archive import RawArchiveReader
from sb_stack.sync.phase_types import (
    CatastrophicError,
    Phase,
    PhaseError,
    PhaseOutcome,
    PhaseResult,
)
from sb_stack.sync.product_mapper import (
    TRACKED_FIELDS,
    field_hash,
    map_product,
)


def run_phase_b(
    *,
    db: DB,
    raw: RawArchiveReader,
    home_store_ids: list[str],
    phase_a_ok: bool,
    logger: Any,
) -> tuple[PhaseResult, set[str]]:
    """Diff + persist. Returns the result AND the set of product_numbers
    that were added or had a TRACKED-field hash change — exactly the rows
    Phase C should fetch fresh details for.

    (Earlier version returned only the PhaseResult and the orchestrator
    re-derived the changed set via a `last_fetched_at = MAX(...)` query.
    That query matched every touched row because every row in a run shares
    the same `now` timestamp, so Phase C ended up re-fetching the entire
    catalog every night.)
    """
    t0 = time.monotonic()
    counts = {
        "products_added": 0,
        "products_updated": 0,
        "products_discontinued": 0,
        "stock_rows_updated": 0,
        "history_rows_written": 0,
    }
    errors: list[PhaseError] = []
    changed: set[str] = set()
    now = datetime.now(UTC)

    try:
        with db.writer() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                seen_product_numbers = _persist_products(
                    conn, raw, counts, errors, changed, now, logger
                )
                if phase_a_ok:
                    _mark_missing_as_discontinued(conn, seen_product_numbers, counts, now, logger)
                _persist_stores(conn, raw, now)
                _persist_stock(conn, raw, home_store_ids, counts, now)
                _persist_taxonomy(conn, raw, now)

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except Exception as e:
        raise CatastrophicError(f"persist transaction failed: {e}") from e

    outcome = PhaseOutcome.PARTIAL if errors else PhaseOutcome.OK
    result = PhaseResult(
        phase=Phase.PERSIST,
        outcome=outcome,
        duration_ms=int((time.monotonic() - t0) * 1000),
        counts=counts,
        errors=errors,
        summary=(
            f"+{counts['products_added']} ~{counts['products_updated']} "
            f"-{counts['products_discontinued']} products; "
            f"{counts['stock_rows_updated']} stock rows"
        ),
    )
    return result, changed


# ── Products + history ────────────────────────────────────────────────


def _persist_products(
    conn: duckdb.DuckDBPyConnection,
    raw: RawArchiveReader,
    counts: dict[str, int],
    errors: list[PhaseError],
    changed: set[str],
    now: datetime,
    logger: Any,
) -> set[str]:
    """Upsert every catalog product. Populates `changed` with every
    product_number that was added or had a tracked-field hash change.
    Returns the full set of seen product_numbers (used by the
    discontinuation sweep)."""
    seen: set[str] = set()
    # NOTE: we deliberately DON'T catch per-product errors here.
    # DuckDB aborts the whole transaction on any statement error — every
    # subsequent statement then fails with "transaction is aborted". Swallowing
    # the first exception lets the Phase B tx slide into that unrecoverable
    # state and hides the real root cause. Let the first real failure bubble
    # up to the Phase B try/except so the whole tx rolls back with a clear
    # error naming the offending product.
    _ = errors  # kept in the signature for future savepoint-based recovery
    for _category, _page, payload in raw.iter_catalog_pages():
        for api_product in payload.get("products", []) or []:
            try:
                pn = _persist_one_product(conn, api_product, counts, changed, now, logger)
            except duckdb.Error as e:
                raise duckdb.Error(
                    f"persist failed for productNumber={api_product.get('productNumber')!r}: {e}"
                ) from e
            if pn:
                seen.add(pn)
    return seen


def _persist_one_product(
    conn: duckdb.DuckDBPyConnection,
    api_product: dict[str, Any],
    counts: dict[str, int],
    changed: set[str],
    now: datetime,
    logger: Any,
) -> str | None:
    row = map_product(api_product)
    raw_pn = row.get("product_number")
    if not raw_pn:
        return None
    pn: str = str(raw_pn)
    row["field_hash"] = field_hash(row)
    row["last_fetched_at"] = now

    existing = conn.execute(
        "SELECT field_hash FROM products WHERE product_number = ?", [pn]
    ).fetchone()

    if existing is None:
        _insert_product(conn, row, now)
        counts["products_added"] += 1
        changed.add(pn)
        logger.debug("product_persisted", product_number=pn, op="added")
        return pn

    if existing[0] == row["field_hash"]:
        conn.execute(
            "UPDATE products SET last_fetched_at = ? WHERE product_number = ?",
            [now, pn],
        )
        return pn

    # Hash changed: write per-field history for TRACKED fields, then UPDATE.
    _write_history_diff(conn, pn, row, counts, now)
    _update_product(conn, row, now)
    counts["products_updated"] += 1
    changed.add(pn)
    logger.debug("product_persisted", product_number=pn, op="updated")
    return pn


def _insert_product(conn: duckdb.DuckDBPyConnection, row: dict[str, Any], now: datetime) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_sql = ", ".join(cols)
    conn.execute(
        f"INSERT INTO products ({col_sql}, first_seen_at) VALUES ({placeholders}, ?)",
        [*row.values(), now],
    )


def _update_product(
    conn: duckdb.DuckDBPyConnection,
    row: dict[str, Any],
    _now: datetime,
) -> None:
    pn = row["product_number"]
    set_cols = [c for c in row if c != "product_number"]
    if not set_cols:
        return
    set_sql = ", ".join(f"{c} = ?" for c in set_cols)
    values = [row[c] for c in set_cols]
    conn.execute(f"UPDATE products SET {set_sql} WHERE product_number = ?", [*values, pn])


def _write_history_diff(
    conn: duckdb.DuckDBPyConnection,
    pn: str,
    new_row: dict[str, Any],
    counts: dict[str, int],
    now: datetime,
) -> None:
    old_row = conn.execute(
        f"SELECT {', '.join(TRACKED_FIELDS)} FROM products WHERE product_number = ?",
        [pn],
    ).fetchone()
    if old_row is None:
        return
    for field, old in zip(TRACKED_FIELDS, old_row, strict=True):
        new = new_row.get(field)
        if _values_differ(old, new):
            conn.execute(
                """
                INSERT OR IGNORE INTO product_history
                    (product_number, observed_at, field, old_value, new_value)
                VALUES (?, ?, ?, ?, ?)
                """,
                [pn, now, field, _to_str(old), _to_str(new)],
            )
            counts["history_rows_written"] += 1


def _values_differ(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return False
    return bool(a != b)


def _to_str(v: Any) -> str | None:
    if v is None:
        return None
    return str(v)


def _mark_missing_as_discontinued(
    conn: duckdb.DuckDBPyConnection,
    seen: set[str],
    counts: dict[str, int],
    now: datetime,
    logger: Any,
) -> None:
    """Products in DB but not in today's catalog → mark discontinued."""
    if not seen:
        return
    placeholders = ", ".join(["?"] * len(seen))
    missing_rows = conn.execute(
        f"""
        SELECT product_number
          FROM products
         WHERE (is_discontinued IS NULL OR is_discontinued = FALSE)
           AND product_number NOT IN ({placeholders})
        """,
        list(seen),
    ).fetchall()
    if not missing_rows:
        return
    today = now.date()
    for (pn,) in missing_rows:
        conn.execute(
            """
            UPDATE products
               SET is_discontinued = TRUE,
                   discontinued_at = ?,
                   last_fetched_at = ?
             WHERE product_number = ?
            """,
            [today, now, pn],
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO product_history
                (product_number, observed_at, field, old_value, new_value)
            VALUES (?, ?, 'is_discontinued', 'False', 'True')
            """,
            [pn, now],
        )
        counts["products_discontinued"] += 1
        logger.info("product_discontinued", product_number=pn)


# ── Stores / opening hours / orders_daily ─────────────────────────────


def _persist_stores(conn: duckdb.DuckDBPyConnection, raw: RawArchiveReader, now: datetime) -> None:
    stores = raw.load_stores()
    if not stores:
        return
    for store in stores:
        row = _map_store(store)
        if not row.get("site_id"):
            continue
        cols = list(row.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_sql = ", ".join(cols)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "site_id")
        conn.execute(
            f"""
            INSERT INTO stores ({col_sql}, last_fetched_at)
            VALUES ({placeholders}, ?)
            ON CONFLICT (site_id) DO UPDATE SET
                {updates}, last_fetched_at = EXCLUDED.last_fetched_at
            """,
            [*row.values(), now],
        )


_STORE_FIELDS: dict[str, str] = {
    "siteId": "site_id",
    "alias": "alias",
    "address": "address",
    "postalCode": "postal_code",
    "city": "city",
    "county": "county",
    "phone": "phone",
    "latitude": "latitude",
    "longitude": "longitude",
    "isTastingStore": "is_tasting_store",
    "isFullAssortmentOrderStore": "is_full_assortment_order_store",
    "depotStockId": "depot_stock_id",
    "parentSiteId": "parent_site_id",
    "searchArea": "search_area",
    "deliveryTimeDays": "delivery_time_days",
}


def _map_store(payload: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for k, v in payload.items():
        col = _STORE_FIELDS.get(k)
        if col is None:
            continue
        row[col] = v
    return row


# ── Stock ────────────────────────────────────────────────────────────


def _persist_stock(
    conn: duckdb.DuckDBPyConnection,
    raw: RawArchiveReader,
    home_store_ids: list[str],
    counts: dict[str, int],
    now: datetime,
) -> None:
    seen: set[tuple[str, str]] = set()
    for site_id, _page, payload in raw.iter_stock_pages():
        for item in payload.get("products", []) or []:
            pn = item.get("productNumber")
            if not pn:
                continue
            stock_val = int(item.get("stock") or 0)
            shelf = item.get("shelf") or item.get("productShelf")
            in_assortment = bool(item.get("isInAssortment", True))
            key = (site_id, pn)
            if key in seen:
                continue
            seen.add(key)
            conn.execute(
                """
                INSERT INTO stock
                    (site_id, product_number, stock, shelf, is_in_assortment, observed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (site_id, product_number) DO UPDATE SET
                    stock = EXCLUDED.stock,
                    shelf = EXCLUDED.shelf,
                    is_in_assortment = EXCLUDED.is_in_assortment,
                    observed_at = EXCLUDED.observed_at
                """,
                [site_id, pn, stock_val, shelf, in_assortment, now],
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO stock_history
                    (site_id, product_number, observed_at, stock, shelf, is_in_assortment)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [site_id, pn, now, stock_val, shelf, in_assortment],
            )
            counts["stock_rows_updated"] += 1
    # Stock that disappeared for a store we fetched: DELETE + history row with 0.
    for site_id in home_store_ids:
        present_here = {pn for (s, pn) in seen if s == site_id}
        if not present_here:
            continue
        existing_rows = conn.execute(
            "SELECT product_number FROM stock WHERE site_id = ?", [site_id]
        ).fetchall()
        existing = {r[0] for r in existing_rows}
        removed = existing - present_here
        for pn in removed:
            conn.execute(
                "DELETE FROM stock WHERE site_id = ? AND product_number = ?",
                [site_id, pn],
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO stock_history
                    (site_id, product_number, observed_at, stock, shelf, is_in_assortment)
                VALUES (?, ?, ?, 0, NULL, FALSE)
                """,
                [site_id, pn, now],
            )
            counts["stock_rows_updated"] += 1


# ── Taxonomy + scheduled launches ────────────────────────────────────


def _persist_taxonomy(
    conn: duckdb.DuckDBPyConnection, raw: RawArchiveReader, now: datetime
) -> None:
    taxonomy = raw.load_taxonomy()
    if not taxonomy:
        return
    today = now.date()
    # Shape: {filterGroups: [{name, values: [{value, count}]}]}
    for group in taxonomy.get("filterGroups", []) or []:
        name = group.get("name")
        if not name:
            continue
        for value in group.get("values", []) or []:
            conn.execute(
                """
                INSERT OR REPLACE INTO filter_taxonomy
                    (captured_at, filter_name, value, count)
                VALUES (?, ?, ?, ?)
                """,
                [today, name, str(value.get("value")), int(value.get("count") or 0)],
            )
        if name == "UpcomingLaunches":
            for value in group.get("values", []) or []:
                try:
                    launch = date.fromisoformat(str(value.get("value")))
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO scheduled_launches
                        (launch_date, observed_at, product_count)
                    VALUES (?, ?, ?)
                    """,
                    [launch, today, int(value.get("count") or 0)],
                )
