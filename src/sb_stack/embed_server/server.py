"""FastAPI app for the `sb-embed` service.

Exposes:
  - GET  /health           — returns 200 only after the model is loaded.
  - GET  /v1/models        — OpenAI-compatible model list (single entry).
  - POST /v1/embeddings    — OpenAI-compatible embeddings endpoint.

The heavy SentenceTransformer load happens in the lifespan hook, before
the server starts accepting requests. `loader.set_model_override` lets
tests skip the real load.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from sb_stack.embed_server.loader import EncodeProtocol, load_model
from sb_stack.embed_server.models import (
    EmbeddingItem,
    EmbedRequest,
    EmbedResponse,
    ModelEntry,
    ModelList,
)
from sb_stack.logging import configure_logging, get_logger
from sb_stack.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Factory form so tests can spin up variants."""
    cfg = settings or get_settings()
    # Only configure logging when the server owns the process (via uvicorn +
    # the cli). Tests can call `create_app()` without log noise.
    log = get_logger("sb_stack.embed_server")
    state: dict[str, Any] = {"model": None, "cfg": cfg}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure_logging(cfg, process_name="sb-embed")
        log.info("embedding_model_loading", name=cfg.embed_model, device=cfg.embed_device)
        t0 = time.monotonic()
        model = await asyncio.to_thread(
            load_model,
            name=cfg.embed_model,
            device=cfg.embed_device,
            cache_folder=cfg.models_cache_dir,
        )
        state["model"] = model
        log.info(
            "embedding_model_loaded",
            dim=model.get_sentence_embedding_dimension(),
            load_time_s=round(time.monotonic() - t0, 2),
        )
        try:
            yield
        finally:
            # Process exit releases VRAM; no explicit unload.
            pass

    app = FastAPI(
        title="sb-embed",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        if state["model"] is None:
            return JSONResponse({"status": "loading"}, status_code=503)
        return JSONResponse({"status": "ok", "model": cfg.embed_model})

    @app.get("/v1/models", response_model=ModelList)
    async def list_models() -> ModelList:
        return ModelList(data=[ModelEntry(id=cfg.embed_model)])

    @app.post("/v1/embeddings", response_model=EmbedResponse)
    async def embeddings(req: EmbedRequest) -> EmbedResponse:
        model: EncodeProtocol | None = state["model"]
        if model is None:
            raise HTTPException(503, detail="model loading")

        inputs = [req.input] if isinstance(req.input, str) else list(req.input)
        if not inputs:
            raise HTTPException(400, detail="input is empty")
        if len(inputs) > cfg.embed_max_batch:
            raise HTTPException(
                413,
                detail=f"input too large, max={cfg.embed_max_batch}",
            )

        t0 = time.monotonic()
        vectors: np.ndarray = await asyncio.to_thread(
            model.encode,
            inputs,
            batch_size=cfg.embed_gpu_batch_size,
            convert_to_numpy=True,
            normalize_embeddings=False,
        )
        log.debug(
            "embedding_request_served",
            batch_size=len(inputs),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return EmbedResponse(
            data=[EmbeddingItem(embedding=v.tolist(), index=i) for i, v in enumerate(vectors)],
            model=cfg.embed_model,
        )

    return app
