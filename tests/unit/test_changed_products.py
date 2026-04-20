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


def _insert(
    db: DB,
    pn: str,
    *,
    category: str = "Vin",
    has_detail: bool = False,
    detail_stamped: bool = False,
) -> None:
    """Insert a product. `has_detail` populates taste/aroma/producer_description
    (simulates a successful Phase C merge from before migration 002).
    `detail_stamped` additionally stamps `last_detail_fetched_at`."""
    now = datetime.now(UTC)
    with db.writer() as conn:
        cols = [
            "product_number",
            "name_bold",
            "category_level_1",
            "first_seen_at",
            "last_fetched_at",
        ]
        vals: list[object] = [pn, f"n{pn}", category, now, now]
        if has_detail:
            cols += ["taste", "aroma", "producer_description"]
            vals += ["t", "a", "d"]
        if detail_stamped:
            cols += ["last_detail_fetched_at"]
            vals += [now]
        placeholders = ", ".join(["?"] * len(cols))
        conn.execute(
            f"INSERT INTO products ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )


def test_full_refresh_returns_every_non_discontinued_product(settings: Settings, db: DB) -> None:
    _insert(db, "1", has_detail=True)
    _insert(db, "2", has_detail=True)
    with db.writer() as conn:
        conn.execute("UPDATE products SET is_discontinued = TRUE WHERE product_number = '2'")
    out = _changed_product_numbers(db, changed_set=set(), full_refresh=True)
    assert out == ["1"]


def test_returns_changed_set_plus_reheal_when_not_full_refresh(settings: Settings, db: DB) -> None:
    _insert(db, "1", detail_stamped=True)  # stamped — not reheal
    _insert(db, "2")  # no stamp + no detail — reheal
    _insert(db, "3", category="Presentartiklar")  # excluded category
    out = _changed_product_numbers(db, changed_set={"1", "99"}, full_refresh=False)
    assert set(out) == {"1", "2", "99"}
    assert out == sorted(out)


def test_stamp_trumps_fallback_canary(settings: Settings, db: DB) -> None:
    # Product with last_detail_fetched_at set is never re-healed, even
    # if the fallback canary (taste/aroma/producer_description) is all
    # null — the stamp is authoritative.
    _insert(db, "1", detail_stamped=True)
    out = _changed_product_numbers(db, changed_set=set(), full_refresh=False)
    assert out == []


def test_pre_migration_product_with_detail_is_not_re_healed(settings: Settings, db: DB) -> None:
    # Populated detail columns but no stamp (rows from before migration 002).
    # The fallback canary recognises the successful merge and skips re-heal.
    _insert(db, "1", has_detail=True)
    out = _changed_product_numbers(db, changed_set=set(), full_refresh=False)
    assert out == []


def test_missing_stamp_and_detail_triggers_reheal(settings: Settings, db: DB) -> None:
    _insert(db, "10")  # no stamp, no detail columns
    _insert(db, "11", has_detail=True)  # pre-migration success
    _insert(db, "12", detail_stamped=True)  # post-migration success
    out = _changed_product_numbers(db, changed_set=set(), full_refresh=False)
    assert out == ["10"]


def test_presentartiklar_never_reheals(settings: Settings, db: DB) -> None:
    _insert(db, "1", category="Presentartiklar")
    out = _changed_product_numbers(db, changed_set=set(), full_refresh=False)
    assert out == []
