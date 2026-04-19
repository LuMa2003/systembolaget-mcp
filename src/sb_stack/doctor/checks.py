"""Individual doctor checks.

Start with the subset that's actionable given today's implementation:
settings load, data dir writability, DB reachability, migrations
current, DuckDB extensions, product count, last-sync freshness, disk
space, home-stores seeded, raw-archive state, embed-service reachable,
api-key-extractable (optional).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from sb_stack.db import DB, MigrationRunner
from sb_stack.doctor.runner import CheckResult, _Check
from sb_stack.settings import Settings


def _ok(name: str, summary: str = "ok", **_: Any) -> CheckResult:
    return CheckResult(name=name, status="pass", summary=summary)


def _warn(name: str, summary: str, details: str | None = None) -> CheckResult:
    return CheckResult(name=name, status="warn", summary=summary, details=details)


def _fail(name: str, summary: str, details: str | None = None) -> CheckResult:
    return CheckResult(name=name, status="fail", summary=summary, details=details)


# ── Individual checks ────────────────────────────────────────────────────


def settings_loads(_: Settings) -> CheckResult:
    # If we got this far, Settings() already parsed successfully.
    return _ok("settings", "loaded")


def data_dir(settings: Settings) -> CheckResult:
    p = settings.data_dir
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _fail("data_dir", f"cannot create {p}: {e}")
    # Touch a probe file to confirm writability.
    probe = p / ".doctor-probe"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return _fail("data_dir", f"not writable: {e}")
    return _ok("data_dir", str(p))


def db_reachable(settings: Settings) -> CheckResult:
    if not settings.db_path.exists():
        return _warn("db_reachable", "database file not created yet — run migrate")
    try:
        with DB(settings).reader() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as e:
        return _fail("db_reachable", f"cannot open DB: {e}")
    return _ok("db_reachable")


def migrations_current(settings: Settings) -> CheckResult:
    try:
        MigrationRunner(DB(settings), settings, _SilentLog()).verify()
    except Exception as e:
        return _warn("migrations_current", str(e))
    return _ok("migrations_current")


def duckdb_extensions(settings: Settings) -> CheckResult:
    # DB.writer() INSTALLs vss + fts; if that succeeds the extensions load.
    try:
        with DB(settings).writer() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as e:
        return _fail("duckdb_extensions", f"extension load failed: {e}")
    return _ok("duckdb_extensions")


def product_count(settings: Settings) -> CheckResult:
    if not settings.db_path.exists():
        return _warn("product_count", "database not initialised")
    try:
        with DB(settings).reader() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM products WHERE is_discontinued IS NOT TRUE"
            ).fetchone()
    except Exception as e:
        return _fail("product_count", f"query failed: {e}")
    count = int(row[0]) if row else 0
    if count == 0:
        return _warn("product_count", "0 active products — sync not run yet?")
    if count > 60_000:
        return _warn("product_count", f"{count} active products — suspiciously high")
    return _ok("product_count", f"{count} active")


def last_sync_freshness(settings: Settings) -> CheckResult:
    if not settings.db_path.exists():
        return _warn("last_sync_freshness", "database not initialised")
    try:
        with DB(settings).reader() as conn:
            row = conn.execute(
                "SELECT MAX(finished_at) FROM sync_runs WHERE status = 'success'"
            ).fetchone()
    except Exception as e:
        return _fail("last_sync_freshness", f"query failed: {e}")
    last = row[0] if row else None
    if last is None:
        return _warn("last_sync_freshness", "no successful sync recorded yet")
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age = datetime.now(UTC) - last
    hours = age.total_seconds() / 3600
    if age > timedelta(hours=30):
        return _fail("last_sync_freshness", f"last success {hours:.1f}h ago")
    if age > timedelta(hours=25):
        return _warn("last_sync_freshness", f"last success {hours:.1f}h ago")
    return _ok("last_sync_freshness", f"{hours:.1f}h ago")


def disk_space(settings: Settings) -> CheckResult:
    target = settings.data_dir
    try:
        usage = shutil.disk_usage(target)
    except OSError as e:
        return _fail("disk_space", f"statvfs failed: {e}")
    gb = usage.free / (1024**3)
    if usage.free < 1 * 1024**3:
        return _fail("disk_space", f"{gb:.2f} GB free (<1 GB)")
    if usage.free < 5 * 1024**3:
        return _warn("disk_space", f"{gb:.2f} GB free (<5 GB)")
    return _ok("disk_space", f"{gb:.1f} GB free")


def home_stores_seeded(settings: Settings) -> CheckResult:
    if not settings.db_path.exists():
        return _warn("home_stores_seeded", "database not initialised")
    try:
        with DB(settings).reader() as conn:
            rows = conn.execute("SELECT site_id FROM stores WHERE is_home_store = TRUE").fetchall()
    except Exception as e:
        return _fail("home_stores_seeded", f"query failed: {e}")
    seeded = {r[0] for r in rows}
    expected = set(settings.store_subset)
    if not seeded:
        return _warn("home_stores_seeded", "no home stores flagged — run bootstrap")
    missing = expected - seeded
    extra = seeded - expected
    if missing or extra:
        return _warn(
            "home_stores_seeded",
            f"expected {sorted(expected)}, got {sorted(seeded)}",
        )
    return _ok("home_stores_seeded", f"{len(seeded)} stores flagged")


def raw_archive_state(settings: Settings) -> CheckResult:
    p = settings.raw_dir
    if not p.exists():
        return _warn("raw_archive_state", "no raw/ directory yet")
    dated = [d for d in p.iterdir() if d.is_dir() and _is_date_name(d.name)]
    if not dated:
        return _warn("raw_archive_state", "no dated raw/ dirs yet")
    newest = max(dated, key=lambda d: d.name)
    return _ok("raw_archive_state", f"newest: {newest.name}")


async def embed_service_reachable(  # noqa: PLR0911 — one return per HTTP branch is clear.
    settings: Settings,
) -> CheckResult:
    # Derive the /health URL from the configured embeddings endpoint.
    base = settings.embed_url.rstrip("/")
    for tail in ("/v1/embeddings", "/embeddings"):
        if base.endswith(tail):
            base = base[: -len(tail)]
            break
    health_url = base.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(health_url)
    except httpx.HTTPError as e:
        return _fail("embed_service_reachable", f"unreachable: {e!r}")
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            return _warn("embed_service_reachable", "200 but non-json body")
        status = body.get("status")
        if status == "ok":
            return _ok("embed_service_reachable", "ok")
        if status == "loading":
            return _warn("embed_service_reachable", "still loading")
        return _warn("embed_service_reachable", f"unknown status: {status}")
    if resp.status_code == 503:
        return _warn("embed_service_reachable", "503 (loading)")
    return _fail("embed_service_reachable", f"status={resp.status_code}")


async def api_key_extractable(settings: Settings) -> CheckResult:
    from sb_stack.api_client import extract_config  # noqa: PLC0415

    try:
        await extract_config(app_base_url=settings.app_base_url)
    except Exception as e:
        return _fail("api_key_extractable", repr(e))
    return _ok("api_key_extractable")


# ── Registry ─────────────────────────────────────────────────────────────

ALL_CHECKS: list[_Check] = [
    _Check("settings", settings_loads),
    _Check("data_dir", data_dir),
    _Check("db_reachable", db_reachable),
    _Check("migrations_current", migrations_current),
    _Check("duckdb_extensions", duckdb_extensions),
    _Check("product_count", product_count),
    _Check("last_sync_freshness", last_sync_freshness),
    _Check("disk_space", disk_space),
    _Check("home_stores_seeded", home_stores_seeded),
    _Check("raw_archive_state", raw_archive_state),
    _Check("embed_service_reachable", embed_service_reachable),
    # Optional (verbose only) — hits the public Systembolaget site.
    _Check("api_key_extractable", api_key_extractable, optional=True),
]


def _is_date_name(name: str) -> bool:
    try:
        datetime.strptime(name, "%Y-%m-%d")
    except ValueError:
        return False
    return True


class _SilentLog:
    """Stand-in logger the migration runner expects. Doctor swallows output."""

    def info(self, *_a: Any, **_k: Any) -> None:
        pass

    def error(self, *_a: Any, **_k: Any) -> None:
        pass

    def warning(self, *_a: Any, **_k: Any) -> None:
        pass
