"""Phase D — render embedding text per product, embed, UPSERT.

Compares freshly-rendered text's `source_hash` against the DB-recorded
one; only re-embeds when the hash changed or the caller forced a
full refresh.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sb_stack.db import DB
from sb_stack.embed import EmbeddingClient, render, source_hash
from sb_stack.errors import EmbeddingError
from sb_stack.settings import Settings
from sb_stack.sync.phase_types import (
    CatastrophicError,
    Phase,
    PhaseError,
    PhaseOutcome,
    PhaseResult,
)


async def run_phase_d(  # noqa: PLR0912 — the candidate-gathering + batching loop is cohesive.
    *,
    db: DB,
    settings: Settings,
    embed_client: EmbeddingClient,
    product_numbers: list[str],
    full_refresh: bool,
    logger: Any,
) -> PhaseResult:
    t0 = time.monotonic()
    counts = {"embedded": 0, "skipped": 0, "failed": 0}
    errors: list[PhaseError] = []

    if not await embed_client.ready():
        return PhaseResult(
            phase=Phase.EMBED,
            outcome=PhaseOutcome.SKIPPED,
            counts=counts,
            summary="embed service not ready",
        )

    # Candidates: supplied list, or every product if full_refresh.
    candidates = list(product_numbers)
    if full_refresh or not candidates:
        with db.reader() as conn:
            rows = conn.execute(
                "SELECT product_number FROM products WHERE is_discontinued IS NOT TRUE"
            ).fetchall()
            candidates = [r[0] for r in rows]

    to_embed: list[tuple[str, str, str, str]] = []  # (pn, text, version, hash)
    with db.reader() as conn:
        for pn in candidates:
            row = _load_product_dict(conn, pn)
            if row is None:
                counts["skipped"] += 1
                continue
            rendered = render(row)
            if rendered is None:
                counts["skipped"] += 1
                continue
            text, version = rendered
            new_hash = source_hash(text)
            if not full_refresh:
                existing = conn.execute(
                    "SELECT source_hash FROM product_embeddings WHERE product_number = ?",
                    [pn],
                ).fetchone()
                if existing is not None and existing[0] == new_hash:
                    counts["skipped"] += 1
                    continue
            to_embed.append((pn, text, version, new_hash))

    if not to_embed:
        return PhaseResult(
            phase=Phase.EMBED,
            outcome=PhaseOutcome.SKIPPED,
            counts=counts,
            duration_ms=int((time.monotonic() - t0) * 1000),
            summary="nothing to embed",
        )

    batch_size = settings.embed_client_batch_size
    parallel = max(1, settings.embed_client_parallel)

    # Pre-chunk into client-sized batches, then drive N in parallel through
    # the embed server per round. Between rounds we UPSERT the returned
    # vectors under the single writer connection — DuckDB is single-writer,
    # and the DB-write stage is fast enough (~ms/row) that serialising it
    # after each gather doesn't re-introduce GPU idle time to matter.
    #
    # Per-batch exceptions are caught so one bad batch doesn't cancel the
    # siblings fired together with it.
    async def _one_batch(
        start: int, batch: list[tuple[str, str, str, str]]
    ) -> tuple[
        int, list[tuple[str, str, str, str]], list[list[float]] | None, BaseException | None
    ]:
        texts = [t for (_, t, _, _) in batch]
        try:
            vectors = await embed_client.embed(texts)
        except EmbeddingError as e:
            return start, batch, None, e
        return start, batch, vectors, None

    chunks: list[tuple[int, list[tuple[str, str, str, str]]]] = [
        (i, to_embed[i : i + batch_size]) for i in range(0, len(to_embed), batch_size)
    ]

    with db.writer() as conn:
        for round_start in range(0, len(chunks), parallel):
            round_chunks = chunks[round_start : round_start + parallel]
            results = await asyncio.gather(*(_one_batch(i, b) for i, b in round_chunks))
            for start, batch, vectors, err in results:
                if err is not None:
                    errors.append(PhaseError(f"embed batch @ {start} failed: {err}", cause=err))
                    counts["failed"] += len(batch)
                    logger.warning(
                        "embed_batch_failed", start=start, size=len(batch), error=str(err)
                    )
                    continue
                assert vectors is not None
                for vec in vectors:
                    if len(vec) != settings.embed_dim:
                        raise CatastrophicError(
                            f"embed service returned dim {len(vec)}, "
                            f"expected SB_EMBED_DIM={settings.embed_dim}"
                        )
                for (pn, _text, version, new_hash), vec in zip(batch, vectors, strict=True):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO product_embeddings
                            (product_number, embedding, source_hash, model_name,
                             template_version, embedded_at)
                        VALUES (?, ?, ?, ?, ?, now())
                        """,
                        [pn, vec, new_hash, settings.embed_model, version],
                    )
                    counts["embedded"] += 1

    outcome = PhaseOutcome.PARTIAL if errors else PhaseOutcome.OK
    return PhaseResult(
        phase=Phase.EMBED,
        outcome=outcome,
        duration_ms=int((time.monotonic() - t0) * 1000),
        counts=counts,
        errors=errors,
        summary=f"{counts['embedded']} embedded, {counts['failed']} failed",
    )


def _load_product_dict(conn: Any, pn: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM products WHERE product_number = ?", [pn]).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.description]
    return dict(zip(cols, row, strict=True))
