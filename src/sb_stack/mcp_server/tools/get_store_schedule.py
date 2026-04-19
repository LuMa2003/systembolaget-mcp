"""`get_store_schedule` — opening hours for a store over the next N days."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field

from sb_stack.errors import InvalidInputError
from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.responses import (
    HomeStore,
    ScheduleEntry,
    StoreSchedule,
)
from sb_stack.mcp_server.sugar import resolve_site_ids

_DESCRIPTION = "Visa öppettider för en butik de kommande dagarna."


class ScheduleInput(BaseModel):
    site_id: str = "main"
    days_ahead: int = Field(default=14, ge=1, le=21)


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def get_store_schedule(inp: ScheduleInput) -> StoreSchedule:
        ctx = get_context()
        site_ids = resolve_site_ids(inp.site_id, ctx.settings) or [inp.site_id]
        if len(site_ids) != 1:
            raise InvalidInputError("site_id must resolve to exactly one store")
        site_id = site_ids[0]

        today = date.today()
        cutoff = today + timedelta(days=inp.days_ahead)

        with ctx.db.reader() as conn:
            store_row = conn.execute(
                """
                SELECT site_id, alias, is_main_store, address, city, county,
                       latitude, longitude
                  FROM stores
                 WHERE site_id = ?
                """,
                [site_id],
            ).fetchone()
            if store_row is None:
                raise InvalidInputError(f"unknown site_id: {site_id}")
            store = HomeStore(
                site_id=store_row[0],
                alias=store_row[1],
                is_main_store=bool(store_row[2]),
                address=store_row[3],
                city=store_row[4],
                county=store_row[5],
                latitude=store_row[6],
                longitude=store_row[7],
            )

            rows = conn.execute(
                """
                SELECT date, open_from, open_to, reason
                  FROM store_opening_hours
                 WHERE site_id = ? AND date BETWEEN ? AND ?
                 ORDER BY date
                """,
                [site_id, today, cutoff],
            ).fetchall()

        entries = [
            ScheduleEntry(
                date=r[0],
                open_from=_fmt_time(r[1]),
                open_to=_fmt_time(r[2]),
                reason=r[3],
                is_open=bool(r[1]) and bool(r[2]) and r[3] != "-",
            )
            for r in rows
        ]
        return StoreSchedule(store=store, schedule=entries)


def _fmt_time(v: Any) -> str | None:
    if v is None:
        return None
    return str(v)[:5]
