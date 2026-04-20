"""Async Systembolaget API client.

One `httpx.AsyncClient` per `SBApiClient` instance. Tenacity wraps each
request with exponential backoff on transient failures (429 / 5xx /
network errors). Auth-class failures (401/403) bypass retry and raise
`AuthenticationError` so the caller can rotate the subscription key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any, Self, cast

import httpx
import tenacity

from sb_stack.api_client.paths import Paths
from sb_stack.api_client.rate_limit import ConcurrencyLimiter
from sb_stack.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitedError,
    ServerError,
    SystembolagetAPIError,
)

KeyRefresher = Callable[[], Awaitable[str]]

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_CONCURRENT = 5
DEFAULT_RETRY_ATTEMPTS = 5
DEFAULT_RETRY_BASE_S = 0.5
DEFAULT_RETRY_MAX_S = 30.0

_RETRIABLE_NETWORK_EXC = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
)


def _is_retriable(exc: BaseException) -> bool:
    if isinstance(exc, _RETRIABLE_NETWORK_EXC):
        return True
    return isinstance(exc, (RateLimitedError, ServerError))


class SBApiClient:
    """High-level async client against the public Systembolaget API.

    Usage:
        async with SBApiClient(api_key="...", base_url="...") as api:
            first_page = await api.search_catalog(category_level_1="Vin", page=1)
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_key_mobile: str | None = None,
        base_url: str = "https://api-extern.systembolaget.se",
        app_base_url: str = "https://www.systembolaget.se",
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        http_client: httpx.AsyncClient | None = None,
        logger: Any | None = None,
        key_refresher: KeyRefresher | None = None,
    ) -> None:
        self.api_key = api_key
        # Mobile namespace takes its own key when supplied; falls back to the
        # ecommerce key when the caller only has one (old single-key flow).
        self.api_key_mobile = api_key_mobile or api_key
        self.base_url = base_url
        self.app_base_url = app_base_url
        self.paths = Paths(base_url)
        self._limiter = ConcurrencyLimiter(max_concurrent)
        self._retry_attempts = retry_attempts
        self._log = logger
        self._key_refresher = key_refresher
        # Single-flight lock so parallel 401s only trigger one extraction.
        self._refresh_lock = asyncio.Lock()
        self._owned_client = http_client is None
        # We don't seed the Ocp-Apim-Subscription-Key header on the session
        # anymore — the key is per-request because ecommerce and mobile use
        # different subscription keys.
        base_headers = {
            "Origin": self.app_base_url,
            "Accept": "application/json",
        }
        self._client = http_client or httpx.AsyncClient(
            timeout=timeout_s,
            headers=base_headers,
        )
        if http_client is not None:
            self._client.headers.update(base_headers)

    # ── Context manager ──────────────────────────────────────────────────
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    # ── Public API methods ───────────────────────────────────────────────

    async def search_catalog(
        self,
        *,
        category_level_1: str | None = None,
        category_level_2: str | None = None,
        page: int = 1,
        size: int = 30,
        extra: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        """Web `productsearch/search` — paged product catalog."""
        params: dict[str, str | int] = {"page": page, "size": size}
        if category_level_1:
            params["categoryLevel1"] = category_level_1
        if category_level_2:
            params["categoryLevel2"] = category_level_2
        if extra:
            params.update(extra)
        raw = await self._get_json(self.paths.productsearch_search(), params=params)
        return cast("dict[str, Any]", raw)

    async def mobile_search_stock(
        self,
        *,
        store_id: str,
        page: int = 1,
        size: int = 30,
        extra: Mapping[str, str | int | bool] | None = None,
    ) -> dict[str, Any]:
        """Mobile `productsearch/search` with in-store-assortment flag.

        Returns shelf + stock inline per product, per the docs — the
        primary stock-fetch path for sync Phase A.
        """
        params: dict[str, str | int | bool] = {
            "storeId": store_id,
            "page": page,
            "size": size,
            "isInStoreAssortmentSearch": "true",
        }
        if extra:
            params.update(extra)
        raw = await self._get_json(self.paths.mobile_productsearch_search(), params=params)
        return cast("dict[str, Any]", raw)

    async def productsearch_filter(self) -> dict[str, Any]:
        """Mobile filter endpoint — all 22 filter groups with counts."""
        return cast("dict[str, Any]", await self._get_json(self.paths.productsearch_filter()))

    async def product_by_number(self, product_number: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._get_json(self.paths.product_by_number(product_number)),
        )

    async def product_by_id(self, product_id: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._get_json(self.paths.product_by_id(product_id)),
        )

    async def product_by_gtin(self, gtin: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._get_json(self.paths.product_by_gtin(gtin)),
        )

    async def site_stores(self) -> list[dict[str, Any]]:
        raw = await self._get_json(self.paths.site_stores())
        # The endpoint returns a bare array, but httpx still wraps it in a
        # parsed JSON value — if Systembolaget ever changes to a wrapper
        # object, surface that clearly instead of silently truncating.
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and "sites" in raw:
            return list(raw["sites"])
        raise SystembolagetAPIError(
            "unexpected site/stores response shape",
            status_code=200,
            url=self.paths.site_stores(),
        )

    async def stockbalance(self, site_id: str, product_id: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._get_json(self.paths.stockbalance(site_id, product_id)),
        )

    # ── Low-level HTTP helpers ───────────────────────────────────────────

    def _default_headers(self) -> dict[str, str]:
        # Kept for backwards compat — emits the ecommerce key only. Per-
        # request key selection now goes through _key_for_url.
        return {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Origin": self.app_base_url,
            "Accept": "application/json",
        }

    def _key_for_url(self, url: str) -> str:
        """Ecommerce and mobile namespaces have independent subscription keys."""
        if "/sb-api-mobile/" in url:
            return self.api_key_mobile
        return self.api_key

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | bool] | None = None,
    ) -> Any:
        try:
            return await self._get_json_inner(url, params=params)
        except AuthenticationError:
            # Only the ecommerce key has an automated recovery path — the
            # extractor reads it from the frontend JS. The mobile key lives
            # only in the mobile app and has no scrape surface, so a mobile
            # 401 propagates immediately for the orchestrator to surface
            # via ntfy.
            if "/sb-api-mobile/" in url:
                raise
            if self._key_refresher is None:
                raise
            refreshed = await self._try_refresh_key()
            if not refreshed:
                raise
            return await self._get_json_inner(url, params=params)

    async def _try_refresh_key(self) -> bool:
        """Single-flight key refresh. Returns True if we got a different key."""
        assert self._key_refresher is not None
        async with self._refresh_lock:
            try:
                new_key = await self._key_refresher()
            except Exception as e:  # noqa: BLE001 — refresh must not crash the call
                if self._log is not None:
                    self._log.warning("api_key_refresh_failed", error=repr(e))
                return False
            if new_key == self.api_key:
                if self._log is not None:
                    self._log.warning(
                        "api_key_refresh_returned_same_key",
                        key_prefix=new_key[:8],
                    )
                return False
            self.api_key = new_key
            # Per-request headers now carry the key via _key_for_url, so no
            # session header to update.
            if self._log is not None:
                self._log.info("api_key_refreshed", key_prefix=new_key[:8])
            return True

    async def _get_json_inner(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | bool] | None,
    ) -> Any:
        async def _once() -> httpx.Response:
            if self._log is not None:
                self._log.debug("api_request_started", url=url, method="GET")
            headers = {"Ocp-Apim-Subscription-Key": self._key_for_url(url)}
            async with self._limiter.acquire():
                resp = await self._client.get(url, params=params, headers=headers)
            self._raise_for_status(resp)
            if self._log is not None:
                self._log.debug(
                    "api_request_completed",
                    url=url,
                    status=resp.status_code,
                    duration_ms=int(resp.elapsed.total_seconds() * 1000),
                )
            return resp

        retrying = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(self._retry_attempts),
            wait=tenacity.wait_exponential(
                multiplier=DEFAULT_RETRY_BASE_S, max=DEFAULT_RETRY_MAX_S
            ),
            retry=tenacity.retry_if_exception(_is_retriable),
            reraise=True,
            before_sleep=self._log_retry if self._log is not None else None,
        )
        async for attempt in retrying:
            with attempt:
                resp = await _once()
        # After reraise/exit we have `resp` defined (tenacity re-raises on
        # exhaustion, so success is the only way out of the loop).
        return resp.json()

    def _log_retry(self, retry_state: tenacity.RetryCallState) -> None:
        if self._log is None:
            return
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        self._log.warning(
            "api_request_retrying",
            attempt=retry_state.attempt_number,
            error=repr(exc),
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        s = resp.status_code
        url = str(resp.request.url) if resp.request else ""
        if s < 400:
            return
        if s in (401, 403):
            raise AuthenticationError("systembolaget authentication failed", status_code=s, url=url)
        if s == 404:
            raise NotFoundError("systembolaget endpoint returned 404", status_code=s, url=url)
        if s == 429:
            raise RateLimitedError("systembolaget rate-limited the client", status_code=s, url=url)
        if s >= 500:
            raise ServerError("systembolaget upstream error", status_code=s, url=url)
        raise SystembolagetAPIError(f"unexpected status {s}", status_code=s, url=url)
