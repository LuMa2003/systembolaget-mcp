"""`list_home_stores` — the user's flagged home stores + today's hours."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from pydantic import BaseModel

from sb_stack.mcp_server.context import get_context
from sb_stack.mcp_server.responses import HomeStore, ListHomeStoresResult

_DESCRIPTION = "Lista användarens hemmabutiker med öppettider och position."


class _NoInput(BaseModel):
    pass


def register(server: Any) -> None:
    @server.tool(description=_DESCRIPTION)
    def list_home_stores(_: _NoInput | None = None) -> ListHomeStoresResult:
        ctx = get_context()
        today = date.today()
        with ctx.db.reader() as conn:
            rows = conn.execute(
                """
                SELECT site_id, alias, is_main_store, address, city, county,
                       latitude, longitude
                  FROM stores
                 WHERE is_home_store = TRUE
                 ORDER BY is_main_store DESC, alias
                """
            ).fetchall()

            main_loc: tuple[float, float] | None = None
            stores_raw: list[dict[str, Any]] = []
            for r in rows:
                d = {
                    "site_id": r[0],
                    "alias": r[1],
                    "is_main_store": bool(r[2]),
                    "address": r[3],
                    "city": r[4],
                    "county": r[5],
                    "latitude": r[6],
                    "longitude": r[7],
                }
                if d["is_main_store"] and d["latitude"] and d["longitude"]:
                    main_loc = (float(d["latitude"]), float(d["longitude"]))
                stores_raw.append(d)

            hours_by_site: dict[str, tuple[str | None, str | None]] = {}
            if stores_raw:
                placeholders = ", ".join(["?"] * len(stores_raw))
                hours_rows = conn.execute(
                    f"""
                    SELECT site_id, open_from, open_to
                      FROM store_opening_hours
                     WHERE site_id IN ({placeholders}) AND date = ?
                    """,
                    [s["site_id"] for s in stores_raw] + [today],
                ).fetchall()
                for site_id, of, ot in hours_rows:
                    hours_by_site[site_id] = (
                        _fmt_time(of),
                        _fmt_time(ot),
                    )

        result_stores = []
        for s in stores_raw:
            distance_km = None
            if main_loc is not None and s["latitude"] is not None and s["longitude"] is not None:
                distance_km = round(
                    _haversine_km(
                        main_loc,
                        (float(s["latitude"]), float(s["longitude"])),
                    ),
                    2,
                )
            open_from, open_to = hours_by_site.get(s["site_id"], (None, None))
            result_stores.append(
                HomeStore(
                    site_id=s["site_id"],
                    alias=s["alias"],
                    address=s["address"],
                    city=s["city"],
                    county=s["county"],
                    is_main_store=s["is_main_store"],
                    latitude=s["latitude"],
                    longitude=s["longitude"],
                    today_open_from=open_from,
                    today_open_to=open_to,
                    distance_from_main_km=distance_km,
                )
            )
        return ListHomeStoresResult(stores=result_stores)


def _fmt_time(v: Any) -> str | None:
    if v is None:
        return None
    return str(v)[:5]  # HH:MM


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = (math.radians(x) for x in a)
    lat2, lon2 = (math.radians(x) for x in b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))
