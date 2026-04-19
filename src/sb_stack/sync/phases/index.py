"""Phase E — rebuild the FTS index over products.

DuckDB's `PRAGMA create_fts_index` drops and recreates the index. If it
fails mid-rebuild the table might briefly have no FTS index — we retry
once and, failing that, mark the phase FAILED. MCP callers handle
FTS-missing errors.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sb_stack.db import DB
from sb_stack.sync.phase_types import Phase, PhaseOutcome, PhaseResult

# DuckDB v1.5.2 FTS ships a Swedish Snowball *stemmer* but not a Swedish
# *stopwords* list — passing stopwords='swedish' makes the extension look
# for a table named "swedish" and crash. 'none' disables stopword
# filtering entirely, which is what we want here anyway: Systembolaget's
# text fields are terse, and filtering common Swedish words would strip
# meaningful matches on short queries like "rökt lax".
_CREATE_FTS_SQL = """
PRAGMA create_fts_index(
    'products',
    'product_number',
    'name_bold', 'name_thin', 'producer_name', 'country',
    'taste', 'aroma', 'usage', 'producer_description',
    stemmer = 'swedish',
    stopwords = 'none',
    lower = 1,
    strip_accents = 0,
    overwrite = 1
)
"""


async def run_phase_e(
    *,
    db: DB,
    products_touched: int,
    logger: Any,
) -> PhaseResult:
    t0 = time.monotonic()
    if products_touched == 0:
        return PhaseResult(
            phase=Phase.INDEX,
            outcome=PhaseOutcome.SKIPPED,
            counts={"rebuilt": 0},
            summary="no products changed",
        )

    def _rebuild() -> None:
        with db.writer() as conn:
            conn.execute(_CREATE_FTS_SQL)

    try:
        await asyncio.to_thread(_rebuild)
        return PhaseResult(
            phase=Phase.INDEX,
            outcome=PhaseOutcome.OK,
            duration_ms=int((time.monotonic() - t0) * 1000),
            counts={"rebuilt": 1},
            summary="fts rebuilt",
        )
    except Exception as first_error:
        logger.warning("fts_rebuild_failed_first_attempt", error=str(first_error))
        await asyncio.sleep(5)
        try:
            await asyncio.to_thread(_rebuild)
            return PhaseResult(
                phase=Phase.INDEX,
                outcome=PhaseOutcome.OK,
                duration_ms=int((time.monotonic() - t0) * 1000),
                counts={"rebuilt": 1},
                summary="fts rebuilt (retry)",
            )
        except Exception as second_error:
            logger.error("fts_rebuild_failed_both_attempts", error=str(second_error))
            return PhaseResult(
                phase=Phase.INDEX,
                outcome=PhaseOutcome.FAILED,
                duration_ms=int((time.monotonic() - t0) * 1000),
                counts={"rebuilt": 0},
                summary=f"fts rebuild failed: {second_error}",
            )
