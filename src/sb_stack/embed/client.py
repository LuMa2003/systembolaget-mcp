"""Async OpenAI-compatible embeddings client.

Used by both sb-sync (Phase D) and sb-mcp (semantic_search,
find_similar_products, pair_with_dish). Speaks the OpenAI `/v1/embeddings`
shape, so it works against our own `sb-embed`, Ollama, vLLM, or a hosted
provider — whatever `SB_EMBED_URL` points at.
"""

from __future__ import annotations

import asyncio
import time
from types import TracebackType
from typing import Any, Self, cast

import httpx
import tenacity

from sb_stack.errors import EmbeddingError

DEFAULT_READY_TIMEOUT_S = 300
DEFAULT_READY_POLL_INTERVAL_S = 5.0
DEFAULT_RETRY_ATTEMPTS = 5
DEFAULT_RETRY_MAX_S = 30.0
DEFAULT_CLIENT_BATCH_SIZE = 128

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
    return isinstance(exc, EmbeddingError)


class EmbeddingClient:
    """Talk to any OpenAI-compatible `/v1/embeddings` endpoint.

    Usage:
        async with EmbeddingClient(url=..., model=...) as embed:
            vectors = await embed.embed(["text one", "text two"])
    """

    def __init__(
        self,
        *,
        url: str,
        model: str,
        client_batch_size: int = DEFAULT_CLIENT_BATCH_SIZE,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        timeout: httpx.Timeout | None = None,
        http_client: httpx.AsyncClient | None = None,
        logger: Any | None = None,
    ) -> None:
        self.url = url
        self.model = model
        self.client_batch_size = client_batch_size
        self._retry_attempts = retry_attempts
        self._log = logger
        self._owned_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=timeout or httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0),
        )

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

    # ── Public ───────────────────────────────────────────────────────────

    async def ready(self) -> bool:
        """One-shot probe — True iff `/health` returns 200 with status=ok."""
        try:
            resp = await self._client.get(self._health_url())
        except httpx.HTTPError:
            return False
        if resp.status_code != 200:
            return False
        try:
            body = resp.json()
        except ValueError:
            return False
        return bool(body.get("status") == "ok")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, preserving input order.

        Splits `texts` into `client_batch_size` chunks, sends each to the
        server, reassembles. Retries transient failures per batch.
        """
        if not texts:
            return []
        all_vectors: list[list[float]] = []
        for start in range(0, len(texts), self.client_batch_size):
            batch = texts[start : start + self.client_batch_size]
            if self._log is not None:
                self._log.debug("embed_request_sent", size=len(batch), url=self.url)
            t0 = time.monotonic()
            vectors = await self._embed_batch_with_retry(batch)
            if self._log is not None:
                self._log.debug(
                    "embed_request_completed",
                    size=len(batch),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
            all_vectors.extend(vectors)
        return all_vectors

    # ── Internals ────────────────────────────────────────────────────────

    async def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        retrying = tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(self._retry_attempts),
            wait=tenacity.wait_exponential(multiplier=1, max=DEFAULT_RETRY_MAX_S),
            retry=tenacity.retry_if_exception(_is_retriable),
            reraise=True,
            before_sleep=self._log_retry if self._log is not None else None,
        )
        async for attempt in retrying:
            with attempt:
                return await self._embed_batch_once(batch)
        # Unreachable — tenacity re-raises on exhaustion; mypy insists on it.
        raise EmbeddingError("embed batch retries exhausted without exception")

    async def _embed_batch_once(self, batch: list[str]) -> list[list[float]]:
        resp = await self._client.post(
            self.url,
            json={"model": self.model, "input": batch},
        )
        # 503 = still warming; surface as a retriable EmbeddingError.
        if resp.status_code == 503:
            raise EmbeddingError("embedding service not ready yet")
        if 500 <= resp.status_code < 600:
            raise EmbeddingError(f"embedding server returned {resp.status_code}")
        if resp.status_code >= 400:
            # 400/413 etc are not retriable — raise a plain EmbeddingError
            # without the retriable marker by nesting under ValueError.
            raise ValueError(f"embedding request rejected: {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        data = list(payload.get("data", []))
        # Server-side "index" preserves input order; sort defensively.
        data.sort(key=lambda d: d["index"])
        if len(data) != len(batch):
            raise EmbeddingError(
                f"response size mismatch: sent {len(batch)} texts, got {len(data)}"
            )
        return [cast("list[float]", d["embedding"]) for d in data]

    def _log_retry(self, retry_state: tenacity.RetryCallState) -> None:
        if self._log is None:
            return
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        self._log.warning(
            "embed_request_retrying",
            attempt=retry_state.attempt_number,
            error=repr(exc),
        )

    def _health_url(self) -> str:
        # `url` is the /v1/embeddings path; strip that to find /health.
        base = self.url.rstrip("/")
        for tail in ("/v1/embeddings", "/embeddings"):
            if base.endswith(tail):
                base = base[: -len(tail)]
                break
        return base.rstrip("/") + "/health"


async def wait_for_embed_ready(
    client: EmbeddingClient,
    *,
    timeout_s: int = DEFAULT_READY_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_READY_POLL_INTERVAL_S,
    logger: Any | None = None,
) -> None:
    """Block until the embedding service reports ready, or timeout.

    Sync calls this before Phase D; MCP calls it at startup. See
    docs/09_embedding_service.md §Startup ordering.
    """
    start = time.monotonic()
    while True:
        if await client.ready():
            if logger is not None:
                logger.info("embed_service_ready")
            return
        waited = int(time.monotonic() - start)
        if logger is not None:
            logger.info("embed_service_not_ready", waited_s=waited)
        if waited >= timeout_s:
            raise TimeoutError(
                f"embedding service not ready after {timeout_s}s; check sb-embed logs"
            )
        await asyncio.sleep(poll_interval_s)
