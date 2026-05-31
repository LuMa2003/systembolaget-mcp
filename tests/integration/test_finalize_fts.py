"""Phase F (finalize) rebuilds the DuckDB FTS index over the persisted catalog.

This is the piece search_products depends on for BM25 text ranking — without
it the tool silently falls back to substring matching.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sb_stack.db import DB, MigrationRunner
from sb_stack.settings import Settings
from sb_stack.sync.phase_types import Phase, PhaseOutcome, PhaseResult
from sb_stack.sync.phases.finalize import run_phase_f


class _Silent:
    def __getattr__(self, _: str) -> object:
        return lambda *a, **k: None


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


def test_finalize_builds_fts_index(settings: Settings) -> None:
    db = DB(settings)
    MigrationRunner(db, settings, _Silent()).run()
    now = datetime.now(UTC)
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO products (product_number, product_id, name_bold, "
            "first_seen_at, last_fetched_at) VALUES "
            "('1', 'p1', 'Barolo Riserva', ?, ?), "
            "('2', 'p2', 'Chianti Classico', ?, ?)",
            [now, now, now, now],
        )
        conn.execute("SELECT nextval('sync_run_id_seq')").fetchone()
        conn.execute(
            "INSERT INTO sync_runs (run_id, started_at, status) VALUES (1, ?, 'running')",
            [now],
        )

    result = run_phase_f(
        db=db,
        settings=settings,
        run_id=1,
        phase_results=[PhaseResult(phase=Phase.PERSIST, outcome=PhaseOutcome.OK)],
        logger=_Silent(),
    )
    assert result.outcome == PhaseOutcome.OK

    with db.reader() as conn:
        # The FTS schema now exists ...
        assert (
            conn.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'fts_main_products'"
            ).fetchone()
            is not None
        )
        # ... and it actually indexed the rows (BM25 finds 'Barolo').
        hit = conn.execute(
            "SELECT product_number FROM ("
            "  SELECT product_number, fts_main_products.match_bm25(product_number, ?) AS s "
            "  FROM products"
            ") WHERE s IS NOT NULL",
            ["Barolo"],
        ).fetchone()
        assert hit is not None
        assert hit[0] == "1"
