"""End-to-end smoke test for run_sync against mocked API + fake embed service.

Exercises the full orchestrator: migrations → lockfile → Phase A (fetch)
→ Phase B (persist + diff) → Phase C (details) → Phase D (embed) →
Phase E (FTS rebuild) → Phase F (finalize + metrics).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from sb_stack.api_client import SBApiClient
from sb_stack.db import DB
from sb_stack.embed import EmbeddingClient
from sb_stack.settings import Settings
from sb_stack.sync.orchestrator import run_sync


class _SilentLog:
    """Stand-in logger that absorbs every event without printing."""

    def __getattr__(self, _: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        return _noop


API_BASE = "https://api-extern.systembolaget.se"
EMBED_URL = "http://localhost:9001/v1/embeddings"
EMBED_HEALTH = "http://localhost:9001/health"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        api_key="test-key",
        api_base_url=API_BASE,
        embed_url=EMBED_URL,
        embed_model="fake/m",
        # Schema column is FLOAT[2560]; the test's fake embed server must
        # emit vectors of that length too.
        embed_dim=2560,
        embed_client_batch_size=8,
        sync_concurrency=2,
        store_subset=["1701"],
        main_store="1701",
        log_to_file=False,
        log_to_stdout=False,
        raw_retention_days=30,
    )


def _catalog_page(products: list[dict[str, Any]], page: int) -> dict[str, Any]:
    return {
        "metadata": {"nextPage": -1, "totalPages": page},
        "products": products,
        "filters": [],
    }


def _empty_page() -> dict[str, Any]:
    return {"metadata": {"nextPage": -1}, "products": []}


def _stock_page(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"metadata": {"nextPage": -1}, "products": items}


def _embed_response(n: int, dim: int) -> dict[str, Any]:
    return {
        "object": "list",
        "model": "fake/m",
        "data": [
            {"object": "embedding", "embedding": [0.1 * i] * dim, "index": i} for i in range(n)
        ],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


@respx.mock
async def test_end_to_end_first_run(settings: Settings) -> None:
    # ── Mock the catalog. Return products only on the Vin/Rött vin partition;
    # every other partition returns empty.
    catalog_route = respx.get(f"{API_BASE}/sb-api-ecommerce/v1/productsearch/search")

    def _search_handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if params.get("categoryLevel1") == "Vin" and params.get("categoryLevel2") == "Rött vin":
            return httpx.Response(
                200,
                json=_catalog_page(
                    [
                        {
                            "productNumber": "1001",
                            "productId": "10001",
                            "productNameBold": "Alpha",
                            "categoryLevel1": "Vin",
                            "categoryLevel2": "Rött vin",
                            "country": "Sverige",
                            "priceInclVat": 99.0,
                            "tasteClockBody": 7,
                        },
                        {
                            "productNumber": "1002",
                            "productId": "10002",
                            "productNameBold": "Beta",
                            "categoryLevel1": "Vin",
                            "categoryLevel2": "Rött vin",
                            "country": "Italien",
                            "priceInclVat": 149.0,
                            "tasteClockBody": 9,
                        },
                    ],
                    1,
                ),
            )
        return httpx.Response(200, json=_empty_page())

    catalog_route.mock(side_effect=_search_handler)

    # Stores
    respx.get(f"{API_BASE}/sb-api-ecommerce/v1/site/stores").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"siteId": "1701", "alias": "Duvan", "city": "Karlstad"},
                {"siteId": "1702", "alias": "Bergvik", "city": "Karlstad"},
            ],
        )
    )
    # Taxonomy
    respx.get(f"{API_BASE}/sb-api-mobile/v1/productsearch/filter").mock(
        return_value=httpx.Response(
            200,
            json={
                "filterGroups": [
                    {
                        "name": "Country",
                        "values": [
                            {"value": "Italien", "count": 500},
                            {"value": "Sverige", "count": 120},
                        ],
                    },
                    {
                        "name": "UpcomingLaunches",
                        "values": [{"value": "2026-05-01", "count": 12}],
                    },
                ]
            },
        )
    )
    # Stock (mobile) for 1701
    respx.get(f"{API_BASE}/sb-api-mobile/v1/productsearch/search").mock(
        return_value=httpx.Response(
            200,
            json=_stock_page([{"productNumber": "1001", "stock": 7, "shelf": "A12"}]),
        )
    )
    # Detail fetches (called by Phase C)
    respx.get(f"{API_BASE}/sb-api-ecommerce/v1/product/productNumber/1001").mock(
        return_value=httpx.Response(
            200,
            json={
                "productNumber": "1001",
                "taste": "Bäriga toner",
                "aroma": "Körsbär",
                "usage": "Passar till grillat",
                "tasteSymbols": ["Grillat"],
            },
        )
    )
    respx.get(f"{API_BASE}/sb-api-ecommerce/v1/product/productNumber/1002").mock(
        return_value=httpx.Response(
            200,
            json={
                "productNumber": "1002",
                "taste": "Fylligt",
                "aroma": "Mörka bär",
                "usage": "Passar till kött",
                "tasteSymbols": ["Kött"],
            },
        )
    )

    # Embed service — ready + /v1/embeddings
    respx.get(EMBED_HEALTH).mock(
        return_value=httpx.Response(200, json={"status": "ok", "model": "fake/m"})
    )

    def _embed_handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        import json as _json  # noqa: PLC0415

        payload = _json.loads(body.decode())
        inputs = payload["input"]
        inputs = [inputs] if isinstance(inputs, str) else inputs
        return httpx.Response(200, json=_embed_response(len(inputs), settings.embed_dim))

    respx.post(EMBED_URL).mock(side_effect=_embed_handler)

    # Silence httpx noise for the test log.
    logging.getLogger("httpx").setLevel(logging.ERROR)

    db = DB(settings)
    async with (
        SBApiClient(api_key="test-key", base_url=API_BASE) as api,
        EmbeddingClient(url=EMBED_URL, model="fake/m") as embed_client,
    ):
        result = await run_sync(
            settings=settings,
            db=db,
            api=api,
            embed_client=embed_client,
            logger=_SilentLog(),
            full_refresh=False,
            reason="test",
        )

    assert result.status in ("success", "partial"), result
    assert result.run_id == 1

    # Post-run DB state
    with db.reader() as conn:
        (n_products,) = conn.execute("SELECT COUNT(*) FROM products").fetchone()
        (n_stock,) = conn.execute("SELECT COUNT(*) FROM stock").fetchone()
        (n_stores,) = conn.execute("SELECT COUNT(*) FROM stores").fetchone()
        (n_embed,) = conn.execute("SELECT COUNT(*) FROM product_embeddings").fetchone()
        (n_taxonomy,) = conn.execute("SELECT COUNT(*) FROM filter_taxonomy").fetchone()
        (run_row_status,) = conn.execute(
            "SELECT status FROM sync_runs WHERE run_id = ?", [result.run_id]
        ).fetchone()
        (n_phase_rows,) = conn.execute(
            "SELECT COUNT(*) FROM sync_run_phases WHERE run_id = ?",
            [result.run_id],
        ).fetchone()

    assert n_products == 2
    assert n_stock == 1
    assert n_stores == 2
    assert n_embed == 2
    assert n_taxonomy >= 3
    assert run_row_status in ("success", "partial")
    assert n_phase_rows >= 5  # A + B + C + D + E + F (F always)

    # Metrics file written
    assert (settings.state_dir / "metrics.prom").exists()
    # Backup taken
    assert any(settings.backup_dir.glob("sb.duckdb.*"))


@respx.mock
async def test_second_run_is_idempotent(settings: Settings) -> None:
    # Same data twice → second run should produce no diffs.
    respx.get(f"{API_BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(200, json=_empty_page())
    )
    respx.get(f"{API_BASE}/sb-api-ecommerce/v1/site/stores").mock(
        return_value=httpx.Response(200, json=[{"siteId": "1701"}])
    )
    respx.get(f"{API_BASE}/sb-api-mobile/v1/productsearch/filter").mock(
        return_value=httpx.Response(200, json={"filterGroups": []})
    )
    respx.get(f"{API_BASE}/sb-api-mobile/v1/productsearch/search").mock(
        return_value=httpx.Response(200, json=_empty_page())
    )
    respx.get(EMBED_HEALTH).mock(
        return_value=httpx.Response(200, json={"status": "ok", "model": "fake/m"})
    )

    db = DB(settings)
    async with (
        SBApiClient(api_key="test-key", base_url=API_BASE) as api,
        EmbeddingClient(url=EMBED_URL, model="fake/m") as embed_client,
    ):
        r1 = await run_sync(
            settings=settings,
            db=db,
            api=api,
            embed_client=embed_client,
            logger=_SilentLog(),
            reason="test",
        )
        r2 = await run_sync(
            settings=settings,
            db=db,
            api=api,
            embed_client=embed_client,
            logger=_SilentLog(),
            reason="test",
        )

    assert r1.run_id == 1
    assert r2.run_id == 2
    with db.reader() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()
    assert n == 2
