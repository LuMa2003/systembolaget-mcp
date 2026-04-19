"""Smoke test: the real 001_initial.sql applies cleanly to an empty DuckDB.

Catches typos / missing extensions before they bite in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sb_stack.db import DB, MigrationRunner
from sb_stack.settings import Settings


class _SilentLog:
    def info(self, *_a: object, **_k: object) -> None: ...
    def error(self, *_a: object, **_k: object) -> None: ...


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, log_to_file=False, log_to_stdout=False)


def test_initial_migration_applies_to_empty_db(settings: Settings) -> None:
    runner = MigrationRunner(DB(settings), settings, _SilentLog())
    applied = runner.run()
    assert applied == 1

    # Sanity-check a representative set of tables exists.
    expected_tables = {
        "products",
        "product_variants",
        "product_embeddings",
        "stores",
        "store_opening_hours",
        "store_orders_daily",
        "stock",
        "stock_history",
        "product_history",
        "scheduled_launches",
        "filter_taxonomy",
        "sync_runs",
        "sync_run_phases",
        "sync_config",
        "schema_migrations",
    }
    with DB(settings).reader() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        present = {r[0] for r in rows}
    missing = expected_tables - present
    assert not missing, f"missing tables: {missing}"


def test_initial_migration_is_idempotent(settings: Settings) -> None:
    runner = MigrationRunner(DB(settings), settings, _SilentLog())
    assert runner.run() == 1
    assert runner.run() == 0


def test_products_generated_image_url(settings: Settings) -> None:
    runner = MigrationRunner(DB(settings), settings, _SilentLog())
    runner.run()
    with DB(settings).writer() as conn:
        conn.execute(
            "INSERT INTO products (product_number, product_id, name_bold) "
            "VALUES ('642008', '1004489', 'Apothic Red')"
        )
        url = conn.execute(
            "SELECT image_url FROM products WHERE product_number = '642008'"
        ).fetchone()
        assert url is not None
        assert url[0] == (
            "https://product-cdn.systembolaget.se/productimages/1004489/1004489_400.webp"
        )


def test_sync_run_id_sequence_exists(settings: Settings) -> None:
    runner = MigrationRunner(DB(settings), settings, _SilentLog())
    runner.run()
    with DB(settings).writer() as conn:
        (v,) = conn.execute("SELECT nextval('sync_run_id_seq')").fetchone()
        assert v == 1
        (v2,) = conn.execute("SELECT nextval('sync_run_id_seq')").fetchone()
        assert v2 == 2
