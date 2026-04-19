"""Integration test for the sb-embed FastAPI app.

Swaps in a tiny fake encoder via `loader.set_model_override` so the
suite doesn't download an 8 GB model on every CI run. Exercises the
real app + lifespan + routes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import numpy as np
import pytest

from sb_stack.embed_server import create_app
from sb_stack.embed_server.loader import set_model_override
from sb_stack.settings import Settings


class _FakeEncoder:
    """Deterministic stand-in for SentenceTransformer."""

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim

    def encode(
        self,
        inputs: list[str],
        *,
        batch_size: int = 32,
        convert_to_numpy: bool = True,
        normalize_embeddings: bool = False,
    ) -> np.ndarray:
        return np.array(
            [[float(hash((t, i)) % 1000) / 1000 for i in range(self.dim)] for t in inputs],
            dtype=np.float32,
        )

    def get_sentence_embedding_dimension(self) -> int:
        return self.dim


@pytest.fixture
def fake_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        embed_model="fake/fake-embedder",
        embed_device="cpu",
        embed_port=0,
        embed_max_batch=16,
        embed_gpu_batch_size=4,
        log_to_file=False,
        log_to_stdout=False,
    )


@pytest.fixture
def _override_model() -> Iterator[None]:
    set_model_override(_FakeEncoder())
    try:
        yield
    finally:
        set_model_override(None)


@pytest.fixture
async def client(
    fake_settings: Settings, _override_model: None
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=fake_settings)
    # httpx.ASGITransport doesn't drive the ASGI lifespan; run it manually
    # via the router so the fake model is loaded into app state before
    # the first request.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


async def test_health_after_lifespan_is_200(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["model"] == "fake/fake-embedder"

    r = await client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["data"][0]["id"] == "fake/fake-embedder"


async def test_embeddings_shape_matches_openai(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/v1/embeddings",
        json={"model": "fake/fake-embedder", "input": ["hej", "välkommen"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "list"
    assert body["model"] == "fake/fake-embedder"
    assert len(body["data"]) == 2
    for i, item in enumerate(body["data"]):
        assert item["object"] == "embedding"
        assert item["index"] == i
        assert isinstance(item["embedding"], list)
        assert len(item["embedding"]) == 8


async def test_embeddings_single_string_input(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/v1/embeddings",
        json={"model": "fake/fake-embedder", "input": "hej"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 1


async def test_embeddings_rejects_empty_input(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/embeddings",
        json={"model": "fake/fake-embedder", "input": []},
    )
    assert r.status_code == 400


async def test_embeddings_rejects_oversize_batch(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/v1/embeddings",
        json={"model": "fake/fake-embedder", "input": [str(i) for i in range(17)]},
    )
    assert r.status_code == 413
