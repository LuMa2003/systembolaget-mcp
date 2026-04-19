"""Stable source_hash for embedding templates.

Drives the "re-embed only when the rendered text actually changed"
optimisation. Writes into `product_embeddings.source_hash` and is
compared on next sync.
"""

from __future__ import annotations

import hashlib


def source_hash(text: str) -> str:
    """Return the sha256 hex digest of `text` (utf-8 encoded)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
