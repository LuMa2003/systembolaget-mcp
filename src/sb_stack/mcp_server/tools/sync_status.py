"""`sync_status` — freshness + last-run summary for the LLM."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.responses import SyncLastRun, SyncStatusResult

_DESCRIPTION = (
    "Visa när databasen senast synkades mot Systembolagets API och hur aktuella siffrorna är."
)
_STALE_HOURS_THRESHOLD = 30.0


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def sync_status() -> SyncStatusResult:
        ctx = get_context()
        now = datetime.now(UTC)
        with ctx.db.reader() as conn:
            last_row = conn.execute(
                """
                SELECT run_id, started_at, finished_at, status,
                       products_added, products_updated, products_discontinued,
                       stock_rows_updated, embeddings_generated, error
                  FROM sync_runs
                 ORDER BY run_id DESC
                 LIMIT 1
                """
            ).fetchone()
            last_run = _build_last_run(last_row) if last_row else SyncLastRun()

            last_success_row = conn.execute(
                "SELECT MAX(finished_at) FROM sync_runs WHERE status = 'success'"
            ).fetchone()
            last_success = last_success_row[0] if last_success_row else None

            pc_row = conn.execute("SELECT COUNT(*) FROM products").fetchone()
            sr_row = conn.execute("SELECT COUNT(*) FROM stock").fetchone()
            product_count = int(pc_row[0]) if pc_row else 0
            stock_rows = int(sr_row[0]) if sr_row else 0

            # NOTE: `api_key_last_validated` is always null until the sync writes an
            # `api_key_last_validated` key into sync_config (not yet wired). Kept here
            # so the field is ready when that lands.
            key_row = conn.execute(
                "SELECT value FROM sync_config WHERE key = 'api_key_last_validated'"
            ).fetchone()
            key_validated = _parse_ts(key_row[0]) if key_row else None

        hours_since: float | None = None
        last_success_ts: datetime | None = None
        if last_success is not None:
            last_success_ts = _ensure_tz(last_success)
            hours_since = round((now - last_success_ts).total_seconds() / 3600, 2)
        stale = hours_since is None or hours_since > _STALE_HOURS_THRESHOLD

        # NOTE: `home_stock_rows` counts ALL stock rows (no store filter); today they
        # happen to be home-store rows post-bootstrap, but the name asserts "home".
        return SyncStatusResult(
            last_run=last_run,
            hours_since_last_success=hours_since,
            product_count=int(product_count),
            home_stock_rows=int(stock_rows),
            api_key_last_validated=key_validated,
            stale=stale,
            summary=_build_summary(stale, hours_since, last_success_ts),
        )


def _build_summary(stale: bool, hours_since: float | None, last_success: datetime | None) -> str:
    if hours_since is None or last_success is None:
        return (
            "Ingen lyckad synk har körts ännu. Datan kan saknas eller vara inaktuell – "
            "kör `sb-stack sync`."
        )
    days = int(hours_since // 24)
    when = f"för {days} dagar sedan" if days >= 1 else f"för {round(hours_since)} timmar sedan"
    date_str = last_success.date().isoformat()
    if stale:
        return (
            f"Senaste lyckade synk var {when} ({date_str}). Datan är inaktuell – "
            "lager- och prisuppgifter kan vara felaktiga."
        )
    return f"Senaste lyckade synk var {when} ({date_str}). Datan är aktuell."


def _build_last_run(row: tuple[Any, ...]) -> SyncLastRun:
    return SyncLastRun(
        run_id=row[0],
        started_at=_ensure_tz(row[1]),
        finished_at=_ensure_tz(row[2]),
        status=row[3],
        products_added=row[4],
        products_updated=row[5],
        products_discontinued=row[6],
        stock_rows_updated=row[7],
        embeddings_generated=row[8],
        error=row[9],
    )


def _ensure_tz(ts: Any) -> Any:
    if isinstance(ts, datetime) and ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _parse_ts(s: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(s)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts
