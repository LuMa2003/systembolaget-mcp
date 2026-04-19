"""SentenceTransformer loader with a test-friendly override hook.

Real deployments load `Qwen/Qwen3-Embedding-4B` on the GPU. Tests can
swap in a tiny CPU model (e.g. `all-MiniLM-L6-v2`) by setting
`SB_EMBED_MODEL` + `SB_EMBED_DEVICE=cpu`, or they can pass an
`encode`-compatible object directly via `set_model_override`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

import numpy as np


class EncodeProtocol(Protocol):
    """The subset of `SentenceTransformer` we actually use."""

    def encode(
        self,
        inputs: list[str],
        *,
        batch_size: int = ...,
        convert_to_numpy: bool = ...,
        normalize_embeddings: bool = ...,
    ) -> np.ndarray: ...

    def get_sentence_embedding_dimension(self) -> int: ...


_OVERRIDE: EncodeProtocol | None = None


def set_model_override(model: EncodeProtocol | None) -> None:
    """Test-only — replace the real model with a fake encoder."""
    global _OVERRIDE  # noqa: PLW0603 — singleton override is the point of this helper
    _OVERRIDE = model


def load_model(
    *,
    name: str,
    device: str,
    cache_folder: Path | str,
) -> EncodeProtocol:
    """Load the SentenceTransformer, honouring any test override."""
    if _OVERRIDE is not None:
        return _OVERRIDE

    # Heavy import is deferred so importing the module doesn't pay the
    # torch + transformers startup cost.
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    model = SentenceTransformer(
        name,
        device=device,
        cache_folder=str(cache_folder),
    )
    # Warmup: forces CUDA kernel compilation before the first real request.
    _ = model.encode(["warmup"], convert_to_numpy=True)
    return cast("EncodeProtocol", model)
