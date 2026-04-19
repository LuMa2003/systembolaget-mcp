"""Unit tests for EmbeddingClient via respx mocks."""

from __future__ import annotations

import httpx
import pytest
import respx

from sb_stack.embed.client import EmbeddingClient, wait_for_embed_ready
from sb_stack.errors import EmbeddingError

URL = "http://localhost:9000/v1/embeddings"


@respx.mock
async def test_embed_sends_openai_shape_and_returns_vectors() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "model": "mock",
                "data": [
                    {"object": "embedding", "embedding": [0.1, 0.2], "index": 0},
                    {"object": "embedding", "embedding": [0.3, 0.4], "index": 1},
                ],
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )
    )
    async with EmbeddingClient(url=URL, model="mock") as embed:
        vectors = await embed.embed(["hello", "world"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    sent = route.calls.last.request
    body = sent.read().decode()
    assert '"model":"mock"' in body or '"model": "mock"' in body
    assert '"hello"' in body


@respx.mock
async def test_embed_reassembles_out_of_order_response() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "model": "mock",
                "data": [
                    {"object": "embedding", "embedding": [0.3, 0.4], "index": 1},
                    {"object": "embedding", "embedding": [0.1, 0.2], "index": 0},
                ],
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )
    )
    async with EmbeddingClient(url=URL, model="m") as embed:
        vectors = await embed.embed(["a", "b"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


@respx.mock
async def test_embed_chunks_inputs_by_client_batch_size() -> None:
    route = respx.post(URL)
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "object": "list",
                "model": "m",
                "data": [
                    {"object": "embedding", "embedding": [float(i)], "index": i} for i in range(2)
                ],
                "usage": {},
            },
        ),
        httpx.Response(
            200,
            json={
                "object": "list",
                "model": "m",
                "data": [
                    {"object": "embedding", "embedding": [float(i + 2)], "index": i}
                    for i in range(2)
                ],
                "usage": {},
            },
        ),
    ]
    async with EmbeddingClient(url=URL, model="m", client_batch_size=2) as embed:
        vectors = await embed.embed(["a", "b", "c", "d"])

    assert vectors == [[0.0], [1.0], [2.0], [3.0]]
    assert route.call_count == 2


@respx.mock
async def test_embed_retries_on_503_then_succeeds() -> None:
    route = respx.post(URL)
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(
            200,
            json={
                "object": "list",
                "model": "m",
                "data": [
                    {"object": "embedding", "embedding": [0.1], "index": 0},
                ],
                "usage": {},
            },
        ),
    ]
    async with EmbeddingClient(url=URL, model="m", retry_attempts=5) as embed:
        vectors = await embed.embed(["x"])

    assert vectors == [[0.1]]
    assert route.call_count == 2


@respx.mock
async def test_embed_retries_exhaust_on_persistent_500() -> None:
    respx.post(URL).mock(return_value=httpx.Response(500))
    async with EmbeddingClient(url=URL, model="m", retry_attempts=3) as embed:
        with pytest.raises(EmbeddingError):
            await embed.embed(["x"])


@respx.mock
async def test_embed_response_size_mismatch_raises() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "model": "m",
                "data": [{"object": "embedding", "embedding": [0.0], "index": 0}],
                "usage": {},
            },
        )
    )
    async with EmbeddingClient(url=URL, model="m", retry_attempts=1) as embed:
        with pytest.raises(EmbeddingError, match="size mismatch"):
            await embed.embed(["a", "b"])


@respx.mock
async def test_ready_true_on_200_with_status_ok() -> None:
    respx.get("http://localhost:9000/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "model": "m"})
    )
    async with EmbeddingClient(url=URL, model="m") as embed:
        assert await embed.ready() is True


@respx.mock
async def test_ready_false_on_503_loading() -> None:
    respx.get("http://localhost:9000/health").mock(
        return_value=httpx.Response(503, json={"status": "loading"})
    )
    async with EmbeddingClient(url=URL, model="m") as embed:
        assert await embed.ready() is False


@respx.mock
async def test_wait_for_embed_ready_returns_when_ready() -> None:
    route = respx.get("http://localhost:9000/health")
    route.side_effect = [
        httpx.Response(503, json={"status": "loading"}),
        httpx.Response(200, json={"status": "ok", "model": "m"}),
    ]
    async with EmbeddingClient(url=URL, model="m") as embed:
        await wait_for_embed_ready(embed, timeout_s=5, poll_interval_s=0.01)


@respx.mock
async def test_wait_for_embed_ready_times_out() -> None:
    respx.get("http://localhost:9000/health").mock(
        return_value=httpx.Response(503, json={"status": "loading"})
    )
    async with EmbeddingClient(url=URL, model="m") as embed:
        with pytest.raises(TimeoutError):
            # timeout_s=0 forces the loop to emit one ping then raise.
            await wait_for_embed_ready(embed, timeout_s=0, poll_interval_s=0.01)
