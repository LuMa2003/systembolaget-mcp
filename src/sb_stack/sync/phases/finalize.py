"""Phase F — update sync_runs, backup DB, clean retention, write metrics.

Always runs, including when earlier phases raised catastrophic — wrap
the whole phase pipeline in try/finally so finalize executes.
"""

from __future__ import annotations

import shutil
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sb_stack.db import DB
from sb_stack.raw_archive import cleanup_old_raw
from sb_stack.settings import Settings
from sb_stack.sync.phase_types import (
    Phase,
    PhaseOutcome,
    PhaseResult,
    merge_counts,
    overall_status,
)

# Prometheus gauge: 0=success, 1=partial, 2=failed.
_STATUS_CODE = {"success": 0, "partial": 1, "failed": 2}


def run_phase_f(
    *,
    db: DB,
    settings: Settings,
    run_id: int,
    phase_results: list[PhaseResult],
    logger: Any,
) -> PhaseResult:
    t0 = time.monotonic()
    status = overall_status(phase_results)
    counts = merge_counts(phase_results)
    now = datetime.now(UTC)

    with db.writer() as conn:
        conn.execute(
            """
            UPDATE sync_runs
               SET finished_at = ?,
                   status = ?,
                   products_added = ?,
                   products_updated = ?,
                   products_discontinued = ?,
                   stock_rows_updated = ?,
                   embeddings_generated = ?,
                   error = ?
             WHERE run_id = ?
            """,
            [
                now,
                status,
                counts.get("products_added", 0),
                counts.get("products_updated", 0),
                counts.get("products_discontinued", 0),
                counts.get("stock_rows_updated", 0),
                counts.get("embedded", 0),
                _summarize_errors(phase_results),
                run_id,
            ],
        )
        for pr in phase_results:
            row = pr.to_row()
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_run_phases
                    (run_id, phase, started_at, finished_at, outcome, counts, error_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    row["phase"],
                    now - timedelta(milliseconds=row["duration_ms"]),
                    now,
                    row["outcome"],
                    _json_dump(row["counts"]),
                    row["error_summary"],
                ],
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO sync_config (key, value, updated_at)
            VALUES ('last_run_id', ?, ?)
            """,
            [str(run_id), now],
        )

    # Backup — best-effort; don't fail the run if it fails.
    try:
        _backup_db(settings, now.date())
    except Exception as e:
        logger.warning("backup_failed", error=str(e))

    # Retention
    try:
        _cleanup_old_backups(settings, now.date())
    except Exception as e:
        logger.warning("backup_retention_failed", error=str(e))
    try:
        cleanup_old_raw(
            settings.raw_dir,
            retention_days=settings.raw_retention_days,
            today=now.date(),
            logger=logger,
        )
    except Exception as e:
        logger.warning("raw_retention_failed", error=str(e))

    # Metrics file
    try:
        _write_metrics(settings, phase_results, status, now, db)
    except Exception as e:
        logger.warning("metrics_write_failed", error=str(e))

    return PhaseResult(
        phase=Phase.FINALIZE,
        outcome=PhaseOutcome.OK,
        duration_ms=int((time.monotonic() - t0) * 1000),
        counts={"run_id": run_id},
        summary=f"run {run_id}: {status}",
    )


# ── Helpers ────────────────────────────────────────────────────────────


def _summarize_errors(phase_results: list[PhaseResult]) -> str | None:
    errs = []
    for r in phase_results:
        if not r.errors:
            continue
        errs.append(f"{r.phase.value}:{len(r.errors)}")
    return ", ".join(errs) if errs else None


def _json_dump(obj: Any) -> str:
    import json  # noqa: PLC0415

    return json.dumps(obj, default=str)


def _backup_db(settings: Settings, today: date) -> None:
    src = settings.db_path
    if not src.exists():
        return
    dest_dir = settings.backup_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"sb.duckdb.{today.isoformat()}"
    shutil.copy2(src, dest)
    wal = src.with_suffix(src.suffix + ".wal")
    if wal.exists():
        shutil.copy2(wal, dest.with_suffix(dest.suffix + ".wal"))


def _cleanup_old_backups(settings: Settings, today: date) -> int:
    dest_dir = settings.backup_dir
    if not dest_dir.exists():
        return 0
    cutoff = today - timedelta(days=settings.backup_retention_days)
    deleted = 0
    for p in dest_dir.iterdir():
        if not p.is_file() or not p.name.startswith("sb.duckdb."):
            continue
        try:
            when = date.fromisoformat(p.name.split(".")[-1])
        except ValueError:
            continue
        if when < cutoff:
            p.unlink()
            deleted += 1
    return deleted


def _write_metrics(
    settings: Settings,
    phase_results: list[PhaseResult],
    status: str,
    now: datetime,
    db: DB,
) -> None:
    metrics_path = settings.state_dir / "metrics.prom"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)

    counts = merge_counts(phase_results)
    lines: list[str] = []

    def _g(name: str, help_: str, value: Any, labels: str = "") -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{labels} {value}")

    ts = int(now.timestamp())
    _g("sb_sync_last_run_timestamp_seconds", "Unix time of the last sync attempt", ts)
    if status == "success":
        _g(
            "sb_sync_last_success_timestamp_seconds",
            "Unix time of the last successful sync",
            ts,
        )
    _g("sb_sync_last_status", "0=success, 1=partial, 2=failed", _STATUS_CODE.get(status, 2))
    total_ms = sum(r.duration_ms for r in phase_results)
    _g("sb_sync_duration_seconds", "Total wall time", round(total_ms / 1000, 2))
    for r in phase_results:
        _g(
            "sb_sync_phase_duration_seconds",
            "Duration per phase",
            round(r.duration_ms / 1000, 2),
            labels=f'{{phase="{r.phase.value}"}}',
        )
    _g("sb_sync_products_added", "Products added in the last run", counts.get("products_added", 0))
    _g(
        "sb_sync_products_updated",
        "Products updated in the last run",
        counts.get("products_updated", 0),
    )
    _g(
        "sb_sync_products_discontinued",
        "Products discontinued in the last run",
        counts.get("products_discontinued", 0),
    )
    _g(
        "sb_sync_stock_rows_updated",
        "Stock rows updated in the last run",
        counts.get("stock_rows_updated", 0),
    )
    _g(
        "sb_sync_embeddings_generated",
        "Embeddings generated in the last run",
        counts.get("embedded", 0),
    )

    active: int = 0
    disc: int = 0
    try:
        with db.reader() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM products WHERE is_discontinued IS NOT TRUE"
            ).fetchone()
            active = int(row[0]) if row else 0
            row = conn.execute(
                "SELECT COUNT(*) FROM products WHERE is_discontinued = TRUE"
            ).fetchone()
            disc = int(row[0]) if row else 0
    except Exception:
        active, disc = 0, 0
    _g(
        "sb_db_product_count",
        "Current product counts",
        int(active),
        labels='{state="active"}',
    )
    _g(
        "sb_db_product_count",
        "Current product counts",
        int(disc),
        labels='{state="discontinued"}',
    )

    size_bytes = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    _g("sb_db_size_bytes", "Size of sb.duckdb on disk", size_bytes)

    metrics_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = ["run_phase_f"]
