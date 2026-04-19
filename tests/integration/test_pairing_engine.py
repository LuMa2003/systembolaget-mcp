"""Integration test for the semantic-retrieval pairing engine.

Uses a deterministic fake embedding client so the test doesn't need a
GPU or a real model. Seeds a tiny product + embeddings dataset where
the expected top match is obvious.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sb_stack.db import DB, MigrationRunner
from sb_stack.pairing import PairingEngine
from sb_stack.settings import Settings


class _FakeEmbedClient:
    """Returns a fixed vector per text — deterministic for test purposes."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        fish = ("fisk", "lax", "torsk", "skaldjur")
        meat = ("kött", "biff", "entrecôte", "oxfilé", "lamm", "rödvinsså")
        vectors: list[list[float]] = []
        for text in texts:
            lower = text.lower()
            if any(t in lower for t in fish):
                vectors.append(_fish_vector(self.dim))
            elif any(t in lower for t in meat):
                vectors.append(_meat_vector(self.dim))
            else:
                vectors.append(_neutral_vector(self.dim))
        return vectors


class _Silent:
    def info(self, *_a, **_k): ...
    def error(self, *_a, **_k): ...
    def warning(self, *_a, **_k): ...
    def debug(self, *_a, **_k): ...


def _fish_vector(dim: int) -> list[float]:
    v = [0.0] * dim
    v[0] = 1.0
    return v


def _meat_vector(dim: int) -> list[float]:
    v = [0.0] * dim
    v[1] = 1.0
    return v


def _neutral_vector(dim: int) -> list[float]:
    v = [0.0] * dim
    v[2] = 1.0
    return v


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        embed_dim=2560,
        store_subset=["1701"],
        main_store="1701",
        log_to_file=False,
        log_to_stdout=False,
    )


@pytest.fixture
def seeded_db(settings: Settings) -> DB:
    db = DB(settings)
    MigrationRunner(db, settings, _Silent()).run()
    now = datetime.now(UTC)
    with db.writer() as conn:
        # Three products with three distinct embeddings.
        for pn, name, cat2, taste_syms, vec_fn in (
            ("1001", "Krispigt Vitt", "Vitt vin", ["Fisk", "Skaldjur"], _fish_vector),
            ("1002", "Kraftigt Rött", "Rött vin", ["Kött", "Lamm"], _meat_vector),
            ("1003", "Mousserande", "Mousserande vin", [], _neutral_vector),
        ):
            conn.execute(
                """
                INSERT INTO products (product_number, product_id, name_bold,
                    category_level_1, category_level_2, taste_symbols,
                    first_seen_at, last_fetched_at)
                VALUES (?, ?, ?, 'Vin', ?, ?, ?, ?)
                """,
                [pn, f"p{pn}", name, cat2, taste_syms, now, now],
            )
            conn.execute(
                """
                INSERT INTO product_embeddings (product_number, embedding,
                    source_hash, model_name, template_version, embedded_at)
                VALUES (?, ?, 'h', 'fake', 'v1', ?)
                """,
                [pn, vec_fn(settings.embed_dim), now],
            )
    return db


async def test_fish_dish_matches_fish_wine(settings: Settings, seeded_db: DB) -> None:
    engine = PairingEngine(
        settings=settings,
        db=seeded_db,
        embed_client=_FakeEmbedClient(settings.embed_dim),
    )
    recs, confidence = await engine.pair(dish="lax med kokt potatis", limit=3)

    assert recs, "expected at least one recommendation"
    assert recs[0].product.product_number == "1001"
    assert recs[0].similarity > 0.9  # exact fake-match
    assert confidence in ("high", "medium")


async def test_meat_dish_matches_meat_wine(settings: Settings, seeded_db: DB) -> None:
    engine = PairingEngine(
        settings=settings,
        db=seeded_db,
        embed_client=_FakeEmbedClient(settings.embed_dim),
    )
    recs, _ = await engine.pair(dish="entrecôte med rödvinssås", limit=3)
    assert recs[0].product.product_number == "1002"


async def test_taste_symbols_hint_filters_candidates(settings: Settings, seeded_db: DB) -> None:
    engine = PairingEngine(
        settings=settings,
        db=seeded_db,
        embed_client=_FakeEmbedClient(settings.embed_dim),
    )
    # Neutral dish, but pin the filter to fish/shellfish — only 1001 matches.
    recs, _ = await engine.pair(
        dish="svamprisotto",
        taste_symbols_hint=["Fisk"],
        limit=3,
    )
    pns = [r.product.product_number for r in recs]
    assert pns == ["1001"]


async def test_diversity_caps_one_per_subcategory(settings: Settings, seeded_db: DB) -> None:
    now = datetime.now(UTC)
    # Add two more products in the same category_level_2 as 1002.
    with seeded_db.writer() as conn:
        for pn, vec_fn in (("1004", _meat_vector), ("1005", _meat_vector)):
            conn.execute(
                """
                INSERT INTO products (product_number, product_id, name_bold,
                    category_level_1, category_level_2, taste_symbols,
                    first_seen_at, last_fetched_at)
                VALUES (?, ?, ?, 'Vin', 'Rött vin', ?, ?, ?)
                """,
                [pn, f"p{pn}", f"Rött {pn}", ["Kött"], now, now],
            )
            conn.execute(
                """
                INSERT INTO product_embeddings (product_number, embedding,
                    source_hash, model_name, template_version, embedded_at)
                VALUES (?, ?, 'h', 'fake', 'v1', ?)
                """,
                [pn, vec_fn(settings.embed_dim), now],
            )

    engine = PairingEngine(
        settings=settings,
        db=seeded_db,
        embed_client=_FakeEmbedClient(settings.embed_dim),
    )
    recs, _ = await engine.pair(dish="kött", limit=3)
    buckets = [r.product.category_level_2 for r in recs]
    # No duplicate category_level_2 among the first len(set(buckets)) picks.
    assert len(set(buckets[:2])) == len(buckets[:2])
