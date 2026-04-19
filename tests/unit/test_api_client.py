"""Unit tests for SBApiClient — backed by respx mocks of httpx.

These pin header shape, error mapping, and retry behaviour without
hitting the network. Real-response-shape verification lives under
tests/contract/ once cassettes are recorded.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from sb_stack.api_client.client import SBApiClient
from sb_stack.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitedError,
    ServerError,
)

BASE = "https://api-extern.systembolaget.se"


@respx.mock
async def test_get_sets_expected_headers() -> None:
    route = respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(200, json={"products": []})
    )
    async with SBApiClient(api_key="test-key", base_url=BASE) as api:
        body = await api.search_catalog(category_level_1="Vin", page=1)

    assert body == {"products": []}
    sent = route.calls.last.request
    assert sent.headers["Ocp-Apim-Subscription-Key"] == "test-key"
    assert sent.headers["Origin"] == "https://www.systembolaget.se"
    assert sent.headers["Accept"] == "application/json"
    assert sent.url.params["categoryLevel1"] == "Vin"
    assert sent.url.params["page"] == "1"
    assert sent.url.params["size"] == "30"


@respx.mock
async def test_401_maps_to_authentication_error() -> None:
    respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(401)
    )
    async with SBApiClient(api_key="bad", base_url=BASE, retry_attempts=2) as api:
        with pytest.raises(AuthenticationError):
            await api.search_catalog(page=1)


@respx.mock
async def test_404_maps_to_not_found() -> None:
    respx.get(f"{BASE}/sb-api-ecommerce/v1/product/productNumber/999999").mock(
        return_value=httpx.Response(404)
    )
    async with SBApiClient(api_key="k", base_url=BASE, retry_attempts=2) as api:
        with pytest.raises(NotFoundError):
            await api.product_by_number("999999")


@respx.mock
async def test_retries_on_500_then_succeeds() -> None:
    route = respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(500),
        httpx.Response(200, json={"products": [{"productNumber": "1"}]}),
    ]
    async with SBApiClient(api_key="k", base_url=BASE, retry_attempts=5) as api:
        body = await api.search_catalog(page=1)

    assert body["products"][0]["productNumber"] == "1"
    assert route.call_count == 3


@respx.mock
async def test_retries_exhaust_on_persistent_500() -> None:
    respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(500)
    )
    async with SBApiClient(api_key="k", base_url=BASE, retry_attempts=3) as api:
        with pytest.raises(ServerError):
            await api.search_catalog(page=1)


@respx.mock
async def test_retries_on_429() -> None:
    route = respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search")
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"ok": True}),
    ]
    async with SBApiClient(api_key="k", base_url=BASE, retry_attempts=5) as api:
        body = await api.search_catalog(page=1)

    assert body == {"ok": True}
    assert route.call_count == 2


@respx.mock
async def test_429_exhaust_raises_rate_limited() -> None:
    respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(429)
    )
    async with SBApiClient(api_key="k", base_url=BASE, retry_attempts=2) as api:
        with pytest.raises(RateLimitedError):
            await api.search_catalog(page=1)


@respx.mock
async def test_mobile_search_sets_store_and_assortment_flag() -> None:
    route = respx.get(f"{BASE}/sb-api-mobile/v1/productsearch/search").mock(
        return_value=httpx.Response(200, json={"products": []})
    )
    async with SBApiClient(api_key="k", base_url=BASE) as api:
        await api.mobile_search_stock(store_id="1701", page=2, size=50)

    sent = route.calls.last.request
    assert sent.url.params["storeId"] == "1701"
    assert sent.url.params["page"] == "2"
    assert sent.url.params["size"] == "50"
    assert sent.url.params["isInStoreAssortmentSearch"] == "true"


@respx.mock
async def test_site_stores_returns_list_and_handles_wrapped_shape() -> None:
    respx.get(f"{BASE}/sb-api-ecommerce/v1/site/stores").mock(
        return_value=httpx.Response(200, json=[{"siteId": "1701"}])
    )
    async with SBApiClient(api_key="k", base_url=BASE) as api:
        rows = await api.site_stores()
    assert rows == [{"siteId": "1701"}]
