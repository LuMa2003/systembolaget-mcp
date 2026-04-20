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


# ── Key refresh on 401 ────────────────────────────────────────────────────


@respx.mock
async def test_401_triggers_refresh_and_retry_then_succeeds() -> None:
    route = respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search")
    route.side_effect = [
        httpx.Response(401),
        httpx.Response(200, json={"products": [{"productNumber": "1"}]}),
    ]

    async def refresh() -> str:
        return "fresh-key"

    async with SBApiClient(
        api_key="stale-key",
        base_url=BASE,
        retry_attempts=2,
        key_refresher=refresh,
    ) as api:
        body = await api.search_catalog(page=1)

    assert body["products"][0]["productNumber"] == "1"
    assert route.call_count == 2
    # Second call must carry the fresh key in the header.
    retry_req = route.calls[-1].request
    assert retry_req.headers["Ocp-Apim-Subscription-Key"] == "fresh-key"
    # And the client's stored key was updated.
    assert api.api_key == "fresh-key"


@respx.mock
async def test_401_without_refresher_raises_immediately() -> None:
    respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(401)
    )
    async with SBApiClient(api_key="bad", base_url=BASE, retry_attempts=3) as api:
        with pytest.raises(AuthenticationError):
            await api.search_catalog(page=1)


@respx.mock
async def test_401_refresh_returning_same_key_raises() -> None:
    respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(401)
    )

    async def refresh() -> str:
        return "same-key"

    async with SBApiClient(
        api_key="same-key",
        base_url=BASE,
        key_refresher=refresh,
    ) as api:
        with pytest.raises(AuthenticationError):
            await api.search_catalog(page=1)


@respx.mock
async def test_401_refresh_then_still_401_raises() -> None:
    respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(401)
    )

    async def refresh() -> str:
        return "fresh-but-still-wrong"

    async with SBApiClient(api_key="stale", base_url=BASE, key_refresher=refresh) as api:
        with pytest.raises(AuthenticationError):
            await api.search_catalog(page=1)


@respx.mock
async def test_refresh_failure_does_not_mask_original_auth_error() -> None:
    respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(401)
    )

    async def refresh() -> str:
        raise RuntimeError("extractor blew up")

    async with SBApiClient(api_key="stale", base_url=BASE, key_refresher=refresh) as api:
        with pytest.raises(AuthenticationError):
            await api.search_catalog(page=1)


# ── Per-namespace subscription keys ──────────────────────────────────────


@respx.mock
async def test_ecommerce_and_mobile_requests_carry_different_keys() -> None:
    eco = respx.get(f"{BASE}/sb-api-ecommerce/v1/productsearch/search").mock(
        return_value=httpx.Response(200, json={"products": []})
    )
    mob = respx.get(f"{BASE}/sb-api-mobile/v1/productsearch/search").mock(
        return_value=httpx.Response(200, json={"products": []})
    )
    async with SBApiClient(api_key="eco-key", api_key_mobile="mob-key", base_url=BASE) as api:
        await api.search_catalog(page=1)
        await api.mobile_search_stock(store_id="1701", page=1)
    assert eco.calls.last.request.headers["Ocp-Apim-Subscription-Key"] == "eco-key"
    assert mob.calls.last.request.headers["Ocp-Apim-Subscription-Key"] == "mob-key"


@respx.mock
async def test_mobile_401_does_not_trigger_ecommerce_refresh() -> None:
    # The mobile key isn't extractable from the web frontend, so a mobile
    # 401 must NOT invoke the refresher — it'd waste an extractor call and
    # might mask the real problem.
    respx.get(f"{BASE}/sb-api-mobile/v1/productsearch/search").mock(
        return_value=httpx.Response(401)
    )
    refresh_calls: list[str] = []

    async def refresh() -> str:
        refresh_calls.append("x")
        return "should-not-be-used"

    async with SBApiClient(
        api_key="eco-key",
        api_key_mobile="stale-mob",
        base_url=BASE,
        retry_attempts=2,
        key_refresher=refresh,
    ) as api:
        with pytest.raises(AuthenticationError):
            await api.mobile_search_stock(store_id="1701", page=1)
    assert refresh_calls == []
    # Mobile key unchanged — no accidental mutation from the refresh path.
    assert api.api_key_mobile == "stale-mob"


@respx.mock
async def test_mobile_key_falls_back_to_ecommerce_when_not_supplied() -> None:
    route = respx.get(f"{BASE}/sb-api-mobile/v1/productsearch/search").mock(
        return_value=httpx.Response(200, json={"products": []})
    )
    async with SBApiClient(api_key="single-key", base_url=BASE) as api:
        await api.mobile_search_stock(store_id="1701", page=1)
    # Only one key supplied → used for both namespaces (single-key legacy path).
    assert route.calls.last.request.headers["Ocp-Apim-Subscription-Key"] == "single-key"
