"""Unit tests for orchestrator._changed_product_numbers.

Guards against regressions of two earlier bugs:
  1. the timestamp-based heuristic that matched every touched product
     because every row in a run shares the same `now`;
  2. no re-heal for products whose detail merge failed on a prior run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sb_stack.db import DB, MigrationRunner
from sb_stack.settings import Settings
from sb_stack.sync.orchestrator import _changed_product_numbers


class _Silent:
    def info(self, *_a, **_k): ...
    def error(self, *_a, **_k): ...
    def warning(self, *_a, **_k): ...


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, log_to_file=False, log_to_stdout=False)


@pytest.fixture
def db(settings: Settings) -> DB:
    d = DB(settings)
    MigrationRunner(d, settings, _Silent()).run()
    return d


def _insert(db: DB, pn: str, *, category: str = "Vin", usage: str | None = None) -> None:
    now = datetime.now(UTC)
    with db.writer() as conn:
        conn.execute(
            """
            INSERT INTO products (product_number, name_bold, category_level_1,
                                  usage, first_seen_at, last_fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [pn, f"n{pn}", category, usage, now, now],
        )


def test_full_refresh_returns_every_non_discontinued_product(settings: Settings, db: DB) -> None:
    _insert(db, "1", usage="u")
    _insert(db, "2", usage="u")
    # Discontinued row is excluded.
    with db.writer() as conn:
        conn.execute("UPDATE products SET is_discontinued = TRUE WHERE product_number = '2'")
    out = _changed_product_numbers(db, changed_set=set(), full_refresh=True)
    assert out == ["1"]


def test_returns_changed_set_plus_reheal_when_not_full_refresh(settings: Settings, db: DB) -> None:
    _insert(db, "1", usage="u")  # has detail — not reheal
    _insert(db, "2", usage=None)  # missing detail — reheal
    _insert(db, "3", usage=None, category="Presentartiklar")  # explicitly excluded
    out = _changed_product_numbers(db, changed_set={"1", "99"}, full_refresh=False)
    # changed_set ∪ reheal = {1, 99, 2}
    assert set(out) == {"1", "2", "99"}
    # Output is sorted for stable logging / diff.
    assert out == sorted(out)


def test_empty_changed_set_still_returns_reheal_candidates(settings: Settings, db: DB) -> None:
    _insert(db, "10", usage=None)
    _insert(db, "11", usage="u")
    out = _changed_product_numbers(db, changed_set=set(), full_refresh=False)
    assert out == ["10"]


def test_presentartiklar_never_reheals(settings: Settings, db: DB) -> None:
    _insert(db, "1", usage=None, category="Presentartiklar")
    out = _changed_product_numbers(db, changed_set=set(), full_refresh=False)
    assert out == []
