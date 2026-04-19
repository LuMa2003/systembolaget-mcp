"""Integration tests for the MCP tool implementations.

We exercise each tool's function body directly (no FastMCP dispatch
machinery) — the tool modules expose `register(server)`, and the tool
function itself is a closure registered via @server.tool. To keep the
tests simple we seed a sample DB, build the AppContext manually, and
invoke the registered callbacks by poking a fake server that records
them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from sb_stack.db import DB, MigrationRunner
from sb_stack.mcp_server.context import AppContext, reset_context, set_context
from sb_stack.mcp_server.tools import (
    compare_products,
    get_product,
    get_store_schedule,
    list_home_stores,
    list_taxonomy_values,
    search_products,
    sync_status,
)
from sb_stack.settings import Settings


class _ToolRecorder:
    """Captures @tool registrations so the tests can call them directly."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_: Any) -> Any:
        def _decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func

        return _decorator


class _SilentLog:
    def __getattr__(self, _: str) -> Any:
        return lambda *a, **k: None


def _register(modules: list[Any]) -> _ToolRecorder:
    rec = _ToolRecorder()
    for m in modules:
        m.register(rec)
    return rec


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        api_key="x",
        embed_dim=2560,
        store_subset=["1701", "1702"],
        main_store="1701",
        log_to_file=False,
        log_to_stdout=False,
        mcp_token="test-token",
    )


@pytest.fixture
def db(settings: Settings) -> DB:
    d = DB(settings)
    MigrationRunner(d, settings, _SilentLog()).run()
    return d


@pytest.fixture
def ctx(settings: Settings, db: DB) -> Iterator[AppContext]:
    c = AppContext(settings=settings, db=db, embed_client=None, logger=_SilentLog())
    set_context(c)
    try:
        yield c
    finally:
        reset_context()


def _seed_products(db: DB) -> None:
    now = datetime.now(UTC)
    with db.writer() as conn:
        for row in (
            (
                "1001",
                "10001",
                "Alpha Röd",
                "AlphaCo",
                "Vin",
                "Rött vin",
                "Italien",
                99.0,
                7,
                ["Grillat"],
                ["Sangiovese"],
                False,
            ),
            (
                "1002",
                "10002",
                "Beta Röd",
                "BetaCo",
                "Vin",
                "Rött vin",
                "Frankrike",
                149.0,
                9,
                ["Kött", "Grillat"],
                ["Merlot"],
                False,
            ),
            (
                "2001",
                "20001",
                "Ljus Ipa",
                "Bryggeri",
                "Öl",
                "Starköl",
                "Sverige",
                35.0,
                3,
                ["Grillat"],
                [],
                False,
            ),
            (
                "9999",
                "99999",
                "Utgått",
                None,
                "Vin",
                "Rött vin",
                "Sverige",
                50.0,
                4,
                [],
                [],
                True,
            ),
        ):
            (
                pn,
                pid,
                name,
                producer,
                cat1,
                cat2,
                country,
                price,
                body,
                taste_symbols,
                grapes,
                discontinued,
            ) = row
            conn.execute(
                """
                INSERT INTO products (
                    product_number, product_id, name_bold, producer_name,
                    category_level_1, category_level_2, country,
                    price_incl_vat, taste_clock_body, taste_symbols, grapes,
                    is_discontinued, first_seen_at, last_fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    pn,
                    pid,
                    name,
                    producer,
                    cat1,
                    cat2,
                    country,
                    price,
                    body,
                    taste_symbols,
                    grapes,
                    discontinued,
                    now,
                    now,
                ],
            )
        # Stock at 1701 for Alpha + IPA, nothing for Beta.
        for pn, stock in (("1001", 7), ("2001", 3)):
            conn.execute(
                """
                INSERT INTO stock
                    (site_id, product_number, stock, shelf,
                     is_in_assortment, observed_at)
                VALUES (?, ?, ?, 'A12', TRUE, ?)
                """,
                ["1701", pn, stock, now],
            )
        # Stores: flag 1701 home + main, 1702 home only.
        conn.execute(
            """
            INSERT INTO stores (site_id, alias, is_home_store, is_main_store,
                                city, county, latitude, longitude, last_fetched_at)
            VALUES ('1701', 'Duvan', TRUE, TRUE, 'Karlstad', 'Värmland',
                    59.382, 13.505, ?)
            """,
            [now],
        )
        conn.execute(
            """
            INSERT INTO stores (site_id, alias, is_home_store, is_main_store,
                                city, county, latitude, longitude, last_fetched_at)
            VALUES ('1702', 'Bergvik', TRUE, FALSE, 'Karlstad', 'Värmland',
                    59.390, 13.460, ?)
            """,
            [now],
        )
        # Opening hours today
        today = date.today()
        conn.execute(
            """
            INSERT INTO store_opening_hours (site_id, date, open_from, open_to, reason)
            VALUES ('1701', ?, TIME '10:00', TIME '19:00', NULL)
            """,
            [today],
        )
        # Taxonomy sample
        conn.execute(
            """
            INSERT INTO filter_taxonomy (captured_at, filter_name, value, count)
            VALUES (?, 'Country', 'Italien', 250),
                   (?, 'Country', 'Frankrike', 300),
                   (?, 'Country', 'Sverige', 80)
            """,
            [today, today, today],
        )
        # A successful sync_runs row
        conn.execute("SELECT nextval('sync_run_id_seq')").fetchone()
        conn.execute(
            """
            INSERT INTO sync_runs
                (run_id, started_at, finished_at, status,
                 products_added, products_updated, products_discontinued,
                 stock_rows_updated, embeddings_generated)
            VALUES (1, ?, ?, 'success', 2, 1, 0, 2, 2)
            """,
            [now, now],
        )


# ── search_products ──────────────────────────────────────────────────────


def test_search_products_filters_by_category(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([search_products])
    out = rec.tools["search_products"](
        search_products.SearchInput(category="Vin", include_discontinued=False)
    )
    pns = {p.product_number for p in out.results}
    assert pns == {"1001", "1002"}
    assert out.total_count == 2


def test_search_products_in_stock_at_main_filters(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([search_products])
    out = rec.tools["search_products"](
        search_products.SearchInput(in_stock_at="main", category="Vin")
    )
    pns = {p.product_number for p in out.results}
    assert pns == {"1001"}  # Beta is not stocked at 1701


def test_search_products_pairs_with_any(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([search_products])
    out = rec.tools["search_products"](search_products.SearchInput(pairs_with_any=["Kött"]))
    pns = {p.product_number for p in out.results}
    assert pns == {"1002"}


def test_search_products_price_range(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([search_products])
    out = rec.tools["search_products"](search_products.SearchInput(price_min=100, price_max=200))
    pns = {p.product_number for p in out.results}
    assert pns == {"1002"}


def test_search_products_home_stock_populated(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([search_products])
    out = rec.tools["search_products"](search_products.SearchInput(category="Vin"))
    alpha = next(p for p in out.results if p.product_number == "1001")
    assert "1701" in alpha.home_stock
    assert alpha.home_stock["1701"].stock == 7


# ── get_product / compare_products ───────────────────────────────────────


def test_get_product_by_number(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([get_product])
    out = rec.tools["get_product"](get_product.GetProductInput(product_number="1001"))
    assert out.product["name_bold"] == "Alpha Röd"
    assert out.home_stock and out.home_stock[0].site_id == "1701"
    assert len(out.image_urls) == 4


def test_compare_products_validates_dupes(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([compare_products])
    with pytest.raises(Exception, match="at least 2"):
        rec.tools["compare_products"](
            compare_products.CompareInput(product_numbers=["1001", "1001"])
        )


def test_compare_products_returns_rows(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([compare_products])
    out = rec.tools["compare_products"](
        compare_products.CompareInput(product_numbers=["1001", "1002"])
    )
    name_row = next(r for r in out.rows if r.field == "name_bold")
    assert name_row.values == ["Alpha Röd", "Beta Röd"]


# ── stores ───────────────────────────────────────────────────────────────


def test_list_home_stores(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([list_home_stores])
    out = rec.tools["list_home_stores"]()
    assert {s.site_id for s in out.stores} == {"1701", "1702"}
    main = next(s for s in out.stores if s.is_main_store)
    assert main.today_open_from == "10:00"
    bergvik = next(s for s in out.stores if s.site_id == "1702")
    # Haversine from Duvan to Bergvik is ~3 km; tolerate ±2 km.
    assert bergvik.distance_from_main_km is not None
    assert 0 <= bergvik.distance_from_main_km <= 10


def test_get_store_schedule_resolves_main(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([get_store_schedule])
    out = rec.tools["get_store_schedule"](
        get_store_schedule.ScheduleInput(site_id="main", days_ahead=3)
    )
    assert out.store.site_id == "1701"
    assert len(out.schedule) >= 1
    assert out.schedule[0].is_open is True


# ── taxonomy ─────────────────────────────────────────────────────────────


def test_list_taxonomy_values_latest_snapshot(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([list_taxonomy_values])
    out = rec.tools["list_taxonomy_values"](
        list_taxonomy_values.TaxonomyInput(filter_name="Country")
    )
    values = {e.value: e.count for e in out.values}
    assert values["Frankrike"] == 300
    assert out.captured_at == date.today()


# ── sync_status ──────────────────────────────────────────────────────────


def test_sync_status_reports_fresh(ctx: AppContext) -> None:
    _seed_products(ctx.db)
    rec = _register([sync_status])
    out = rec.tools["sync_status"]()
    assert out.last_run.status == "success"
    assert out.product_count == 4
    assert out.home_stock_rows == 2
    assert out.stale is False


def test_sync_status_stale_when_no_success(settings: Settings, db: DB) -> None:
    ctx_local = AppContext(settings=settings, db=db, embed_client=None, logger=_SilentLog())
    set_context(ctx_local)
    try:
        rec = _register([sync_status])
        out = rec.tools["sync_status"]()
        assert out.stale is True
        assert out.hours_since_last_success is None
    finally:
        reset_context()
    logging.getLogger("httpx").setLevel(logging.ERROR)
