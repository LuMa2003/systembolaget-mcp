"""Integration tests for the doctor runner + checks."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from sb_stack.db import DB, MigrationRunner
from sb_stack.doctor import run_all
from sb_stack.settings import Settings


class _Silent:
    def info(self, *_a, **_k): ...  # noqa: D401
    def error(self, *_a, **_k): ...
    def warning(self, *_a, **_k): ...


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        api_key="x",
        embed_url="http://localhost:65000/v1/embeddings",
        embed_dim=2560,
        store_subset=["1701"],
        main_store="1701",
        log_to_file=False,
        log_to_stdout=False,
    )


@pytest.fixture
def initialised_db(settings: Settings) -> DB:
    db = DB(settings)
    MigrationRunner(db, settings, _Silent()).run()
    return db


def test_unmigrated_db_warns_but_doesnt_fail(settings: Settings) -> None:
    # No DB yet: migrations_current + db_reachable warn.
    res = run_all(
        settings,
        only=["settings", "data_dir", "db_reachable", "migrations_current"],
    )
    assert res.failed == 0
    by_name = {r.name: r for r in res.results}
    assert by_name["settings"].status == "pass"
    assert by_name["db_reachable"].status == "warn"


def test_healthy_db_passes_core_checks(settings: Settings, initialised_db: DB) -> None:
    res = run_all(
        settings,
        only=[
            "settings",
            "data_dir",
            "db_reachable",
            "migrations_current",
            "duckdb_extensions",
            "product_count",
        ],
    )
    assert res.failed == 0, [(r.name, r.status, r.summary) for r in res.results]
    by_name = {r.name: r for r in res.results}
    assert by_name["db_reachable"].status == "pass"
    assert by_name["migrations_current"].status == "pass"
    # product_count warns when zero — acceptable post-migrate, pre-sync.
    assert by_name["product_count"].status in ("pass", "warn")


def test_last_sync_freshness_reports_recent(settings: Settings, initialised_db: DB) -> None:
    now = datetime.now(UTC)
    with initialised_db.writer() as conn:
        conn.execute("SELECT nextval('sync_run_id_seq')").fetchone()
        conn.execute(
            """
            INSERT INTO sync_runs
                (run_id, started_at, finished_at, status)
            VALUES (1, ?, ?, 'success')
            """,
            [now, now],
        )
    res = run_all(settings, only=["last_sync_freshness"])
    assert [r.status for r in res.results] == ["pass"]


def test_home_stores_seeded_warns_when_missing(settings: Settings, initialised_db: DB) -> None:
    res = run_all(settings, only=["home_stores_seeded"])
    assert [r.status for r in res.results] == ["warn"]


@respx.mock
def test_embed_service_reachable_ok(settings: Settings) -> None:
    respx.get("http://localhost:65000/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "model": "m"})
    )
    res = run_all(settings, only=["embed_service_reachable"])
    assert [r.status for r in res.results] == ["pass"]


@respx.mock
def test_embed_service_reachable_loading(settings: Settings) -> None:
    respx.get("http://localhost:65000/health").mock(
        return_value=httpx.Response(503, json={"status": "loading"})
    )
    res = run_all(settings, only=["embed_service_reachable"])
    assert [r.status for r in res.results] == ["warn"]


def test_only_filter_runs_only_requested_checks(settings: Settings) -> None:
    res = run_all(settings, only=["settings"])
    assert [r.name for r in res.results] == ["settings"]


def test_optional_checks_excluded_by_default(settings: Settings) -> None:
    res = run_all(settings)
    names = [r.name for r in res.results]
    assert "api_key_extractable" not in names
