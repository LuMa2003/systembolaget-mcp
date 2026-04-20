"""Phase D fires N batches in parallel and writes vectors to the DB.

Observable behaviour: with `embed_client_parallel=4` and 10 batches worth
of input, we see at most 4 concurrent embed-server calls at any instant,
and every row still lands in product_embeddings.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sb_stack.db import DB, MigrationRunner
from sb_stack.settings import Settings
from sb_stack.sync.phases.embed import run_phase_d


class _Silent:
    def info(self, *_a, **_k): ...
    def error(self, *_a, **_k): ...
    def warning(self, *_a, **_k): ...
    def debug(self, *_a, **_k): ...


class _InstrumentedEmbedClient:
    """Tracks peak concurrency across overlapping embed() calls."""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.in_flight = 0
        self.peak = 0
        self.batch_count = 0
        self._lock = asyncio.Lock()

    async def ready(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.batch_count += 1
        # Yield long enough that parallel sends really overlap — not
        # instant like a synchronous function.
        await asyncio.sleep(0.05)
        async with self._lock:
            self.in_flight -= 1
        return [[float(i % 100) / 100.0] * self.dim for i in range(len(texts))]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        # Schema column is FLOAT[2560]; keep dim at the real value so
        # UPSERTs don't fail on array-length mismatch.
        embed_dim=2560,
        embed_client_batch_size=5,
        embed_client_parallel=4,
        log_to_file=False,
        log_to_stdout=False,
    )


@pytest.fixture
def db(settings: Settings) -> DB:
    d = DB(settings)
    MigrationRunner(d, settings, _Silent()).run()
    # Seed 50 products → 10 batches of 5 texts each at the configured batch size.
    now = datetime.now(UTC)
    with d.writer() as conn:
        for i in range(50):
            conn.execute(
                """
                INSERT INTO products (product_number, name_bold, category_level_1,
                    taste, first_seen_at, last_fetched_at)
                VALUES (?, ?, 'Vin', 'test taste', ?, ?)
                """,
                [f"p{i:04d}", f"Wine {i}", now, now],
            )
    return d


async def test_phase_d_runs_batches_in_parallel(settings: Settings, db: DB) -> None:
    client = _InstrumentedEmbedClient(settings.embed_dim)
    pns = [f"p{i:04d}" for i in range(50)]

    result = await run_phase_d(
        db=db,
        settings=settings,
        embed_client=client,  # type: ignore[arg-type]
        product_numbers=pns,
        full_refresh=True,
        logger=_Silent(),
    )

    assert result.counts["embedded"] == 50
    assert client.batch_count == 10  # 50 / 5
    # Peak concurrency must reach the cap — otherwise we're still serial.
    assert client.peak == settings.embed_client_parallel, client.peak

    with db.reader() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM product_embeddings").fetchone()
    assert n == 50


async def test_phase_d_parallel_one_is_still_serial(settings: Settings, db: DB) -> None:
    settings.embed_client_parallel = 1
    client = _InstrumentedEmbedClient(settings.embed_dim)
    pns = [f"p{i:04d}" for i in range(50)]

    await run_phase_d(
        db=db,
        settings=settings,
        embed_client=client,  # type: ignore[arg-type]
        product_numbers=pns,
        full_refresh=True,
        logger=_Silent(),
    )

    assert client.peak == 1
    assert client.batch_count == 10
