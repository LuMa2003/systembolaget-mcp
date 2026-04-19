"""Client-side embedding helpers.

Talks to the `sb-embed` service (or any OpenAI-compatible embeddings
endpoint) over HTTP. See docs/09_embedding_service.md.
"""

from sb_stack.embed.client import EmbeddingClient, wait_for_embed_ready
from sb_stack.embed.hashing import source_hash
from sb_stack.embed.templates import TEMPLATES, render

__all__ = [
    "TEMPLATES",
    "EmbeddingClient",
    "render",
    "source_hash",
    "wait_for_embed_ready",
]
