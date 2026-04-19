# 10 — Sync orchestration

How the six phases wire together into a reliable nightly run. Extends [05_sync_pipeline.md](./05_sync_pipeline.md) with retry semantics, failure classification, lockfile, scheduler, replay, dry-run, and metrics.

## The orchestrator

A single async function owns the full run lifecycle:

```python
# src/sb_stack/sync/orchestrator.py

async def run_sync(
    *,
    full_refresh: bool = False,
    from_raw: date | None = None,
    only_phase: Phase | None = None,
    dry_run: bool = False,
) -> SyncRunResult:
    """
    Execute a full sync run or a scoped variant.
    Returns a SyncRunResult summarizing the outcome.
    """
```

Every caller (scheduler, manual CLI, bootstrap) goes through this one function. No other entrypoints initiate a sync.

## State machine

```
   ┌──────────────────────┐
   │ start_run            │  acquire lockfile, INSERT sync_runs row
   │                      │  (status='running')
   └──────────┬───────────┘
              │
              ▼
   ╔══════════════════════╗  outcomes:
   ║ Phase A: fetch        ║   ok        → all pages fetched
   ║                       ║   partial   → some pages failed
   ║                       ║   catastro. → 0 pages fetched OR stores call failed
   ╚══════════╤═══════════╝
              │
              ▼
   ╔══════════════════════╗  outcomes:
   ║ Phase B: diff+persist ║   ok, partial, or catastrophic (rollback)
   ╚══════════╤═══════════╝
              │
              ▼
   ╔══════════════════════╗  outcomes:
   ║ Phase C: details      ║   ok, partial, or skipped (no products to fetch)
   ╚══════════╤═══════════╝
              │
              ▼
   ╔══════════════════════╗  outcomes:
   ║ Phase D: embeddings   ║   ok, partial, or skipped (sb-embed down / nothing to embed)
   ╚══════════╤═══════════╝
              │
              ▼
   ╔══════════════════════╗  outcomes:
   ║ Phase E: FTS rebuild  ║   ok, partial, or skipped (no products changed)
   ╚══════════╤═══════════╝
              │
              ▼
   ┌──────────────────────┐  ALWAYS runs (finally block):
   │ Phase F: finalize    │   update sync_runs, backup DB, cleanup retention,
   │                      │   write metrics, release lockfile
   └──────────────────────┘
```

Critical: **Phase F always runs**, including when earlier phases raised catastrophic. Implemented as a `try/finally` over phases A–E.

## Phase result types

```python
class PhaseOutcome(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"        # phase-level failure, but not catastrophic
    CATASTROPHIC = "catastrophic"

@dataclass
class PhaseResult:
    phase: Phase
    outcome: PhaseOutcome
    duration_ms: int
    counts: dict[str, int]           # phase-specific, e.g. {"products_added": 7}
    errors: list[PhaseError]         # per-item failures (partial case)
    summary: str                     # human-readable one-liner

class PhaseError(Exception):
    """Individual item failure within a phase (recoverable)."""

class CatastrophicError(Exception):
    """Abort the entire run; no further phases should execute."""
```

Each phase returns a `PhaseResult`. The orchestrator aggregates them into the final `status`:

```python
def overall_status(results: list[PhaseResult]) -> str:
    if any(r.outcome == PhaseOutcome.CATASTROPHIC for r in results):
        return "failed"
    if any(r.outcome in (PhaseOutcome.FAILED, PhaseOutcome.PARTIAL) for r in results):
        return "partial"
    return "success"
```

## Failure classification

### Catastrophic (abort run)

These indicate the system is in a state where continuing would compound damage:

- **Database file unopenable** — file missing, corrupt, or locked by another process (not us).
- **Migration integrity failure** — applied migration's sha256 doesn't match disk. Don't write over a drifted schema.
- **Subscription key invalid after re-extraction** — both cached and freshly-scraped keys 401. Alert loudly; user intervention required.
- **Disk full** — writes will fail partway; stop cleanly.
- **Phase B transaction rollback due to programming error** — signals a bug, not an environmental issue.

Behavior: log at `alert=true` severity, record `status='failed'` in sync_runs, release lockfile, raise to caller.

### Phase-level failure (mark partial, continue)

These are recoverable; the remaining phases can still contribute:

- **Phase A stores call fails** — we have no home-store context. Treat as catastrophic (stock mapping impossible).
- **Phase A catalog page fails** (some pages) — partial catalog; don't mark products missing-from-response as discontinued (can't distinguish "gone" from "we didn't see it"). Mark partial.
- **Phase A taxonomy call fails** — taxonomy snapshot skipped; older snapshot still in DB. Mark partial (skip).
- **Phase A per-store-stock page fails** — partial stock for that store; don't DELETE from `stock` for products we couldn't re-confirm. Mark partial.
- **Phase C product detail fails** — skip that product's deep fetch; it stays with its previously-known detail fields. Mark partial.
- **Phase D sb-embed unreachable** — skip the whole phase. Existing embeddings remain valid; new products just miss semantic search until next run. Mark partial (skip).
- **Phase D single batch fails** — skip those products' embeddings. Mark partial.
- **Phase E FTS rebuild fails** — retry once, then give up. Old FTS index remains (unless it was already dropped — DuckDB FTS `create_fts_index` drops-and-recreates, so a failure mid-rebuild might leave no index; see §"FTS rebuild safety" below).

### Ignore / skip (not a failure)

- Phase C skipped when Phase B reported no changed or new products.
- Phase D skipped when no product has a changed `embed_text_hash`.
- Phase E skipped when Phase B touched 0 products AND Phase C wrote 0 updates.

## Retry semantics per phase

Using `tenacity`:

```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

HTTP_RETRY = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=16),
    retry=retry_if_exception_type((
        httpx.ConnectError, httpx.ReadError, httpx.TimeoutException, ServerError
    )),
    reraise=True,
)
```

Applied to:
- All `SBApiClient.get()` calls (Phase A, C)
- Embedding client calls (Phase D — already inside the client, see [09_embedding_service.md](./09_embedding_service.md))

### Status-code handling

Inside `SBApiClient.request()`, before raising, classify:

| Status | Action |
|---|---|
| 200 | return body |
| 204 | return empty |
| 304 | n/a (we don't send conditional headers) |
| 401, 403 | force-refresh API key via extractor → retry once → if still 401/403 raise `AuthenticationError` (catastrophic upstream) |
| 429 | sleep Retry-After seconds (fallback 30), then retry |
| 4xx other | raise `NotFoundError` (404) or `SystembolagetAPIError` — don't retry |
| 5xx | raise `ServerError` — retry via tenacity |
| network errors | retry via tenacity |

### Per-phase retry budgets

A single phase must not spin indefinitely. Each phase has a wall-clock budget:

```python
PHASE_BUDGETS = {
    Phase.FETCH:     timedelta(minutes=20),  # plenty for ~1200 calls at 5 concurrent
    Phase.PERSIST:   timedelta(minutes=5),   # pure CPU+DB
    Phase.DETAILS:   timedelta(minutes=60),  # first-run can be ~30 min
    Phase.EMBED:     timedelta(minutes=60),  # first-run can be ~20 min
    Phase.INDEX:     timedelta(minutes=2),   # FTS rebuild
    Phase.FINALIZE:  timedelta(minutes=2),
}
```

When a phase exceeds budget, it raises `PhaseTimeoutError`, which is treated as `PhaseOutcome.FAILED` (mark partial, continue).

### Env-configurable

All budgets are overridable:

```
SB_PHASE_FETCH_TIMEOUT_MINUTES=20
SB_PHASE_PERSIST_TIMEOUT_MINUTES=5
SB_PHASE_DETAILS_TIMEOUT_MINUTES=60
SB_PHASE_EMBED_TIMEOUT_MINUTES=60
SB_PHASE_INDEX_TIMEOUT_MINUTES=2
SB_PHASE_FINALIZE_TIMEOUT_MINUTES=2
```

Raising a budget is cheap. Lowering it is useful for CI (fail fast on stuck tests).

## Phase A — details

```python
async def run_phase_a(
    orchestrator_ctx: OrchestratorContext,
) -> PhaseResult:
    t_start = time.monotonic()
    errors: list[PhaseError] = []
    counts = {"catalog_pages": 0, "stock_pages": 0, "stores_fetched": 0,
              "taxonomy_fetched": 0, "details_skipped": 0}

    async with asyncio.TaskGroup() as tg:
        # 1. Stores (critical — catastrophic if fails)
        stores_task = tg.create_task(_fetch_stores(orchestrator_ctx))

        # 2. Taxonomy (best-effort)
        taxonomy_task = tg.create_task(_fetch_taxonomy(orchestrator_ctx))

        # 3. Catalog (big, paginated, partition by categoryLevel1)
        catalog_task = tg.create_task(_fetch_catalog(orchestrator_ctx, counts, errors))

    # Phase A catastrophic conditions:
    if stores_task.exception():
        raise CatastrophicError("stores fetch failed") from stores_task.exception()
    if counts["catalog_pages"] == 0:
        raise CatastrophicError("no catalog pages fetched")

    # Per-store stock (after stores, so we know what to fetch)
    home_site_ids = [s.site_id for s in stores_task.result() if s.site_id in orchestrator_ctx.settings.store_subset]
    for site_id in home_site_ids:
        try:
            pages = await _fetch_stock_for_store(orchestrator_ctx, site_id)
            counts["stock_pages"] += pages
        except Exception as e:
            errors.append(PhaseError(f"stock fetch failed for {site_id}", cause=e))

    outcome = (
        PhaseOutcome.PARTIAL if errors else PhaseOutcome.OK
    )
    return PhaseResult(
        phase=Phase.FETCH, outcome=outcome,
        duration_ms=int((time.monotonic() - t_start) * 1000),
        counts=counts, errors=errors,
        summary=f"{counts['catalog_pages']} catalog, {counts['stock_pages']} stock, "
                f"{len(errors)} errors",
    )
```

Each sub-fetch writes its raw JSON response to `/data/raw/YYYY-MM-DD/...` *before* the orchestrator considers it "done." This is the source-of-truth invariant: **if a raw file exists, we saw the response; if not, we didn't**.

## Phase B — details

Phase B is the one phase that wraps everything in a single DuckDB transaction:

```python
async def run_phase_b(
    orchestrator_ctx, raw_dir: Path,
) -> PhaseResult:
    counts = {"products_added": 0, "products_updated": 0,
              "products_discontinued": 0, "stock_rows_updated": 0,
              "history_rows_written": 0}
    errors = []

    with db.writer() as conn:
        try:
            conn.begin()

            # Load catalog from raw/; compute diffs; write
            await _persist_products(conn, raw_dir, counts, errors,
                                    full_refresh=orchestrator_ctx.full_refresh)
            await _persist_stock(conn, raw_dir, counts, errors)
            await _persist_stores_and_hours(conn, raw_dir)
            await _persist_orders_daily(conn, raw_dir)
            await _persist_taxonomy(conn, raw_dir)
            await _persist_scheduled_launches(conn, raw_dir)

            conn.commit()

        except Exception as e:
            conn.rollback()
            raise CatastrophicError(f"persist transaction failed: {e}") from e

    outcome = PhaseOutcome.PARTIAL if errors else PhaseOutcome.OK
    return PhaseResult(...)
```

The commit is atomic: MCP readers see pre-sync state until the commit returns.

**Important: the discontinuation mark is conditional on Phase A being fully successful.** If Phase A is partial, we don't mark missing-from-response products as discontinued, because we might just not have received that page.

```python
def should_mark_missing_as_discontinued(phase_a: PhaseResult) -> bool:
    # Only mark discontinuation if we're confident we saw the entire catalog
    return phase_a.outcome == PhaseOutcome.OK
```

Skipping discontinuation on partial runs is safer: products stay "alive" for an extra day until the next successful full run. False-discontinuation recoveries are awkward (we'd have `product_history` rows showing `is_discontinued=true → false` which doesn't reflect reality).

## Phase C — details

Fan-out per product, concurrency-limited:

```python
async def run_phase_c(orchestrator_ctx, changed_product_numbers: list[str]) -> PhaseResult:
    if not changed_product_numbers:
        return PhaseResult(Phase.DETAILS, PhaseOutcome.SKIPPED, counts={"fetched": 0}, ...)

    semaphore = asyncio.Semaphore(orchestrator_ctx.settings.sync_concurrency)
    errors = []
    fetched_count = 0

    async def _one(product_number: str):
        nonlocal fetched_count
        async with semaphore:
            try:
                await _fetch_and_persist_one_detail(orchestrator_ctx, product_number)
                fetched_count += 1
            except SystembolagetAPIError as e:
                errors.append(PhaseError(
                    f"detail fetch failed for {product_number}", cause=e,
                ))
                log.warning("detail_fetch_failed",
                            product_number=product_number, error=str(e))

    await asyncio.gather(*[_one(pn) for pn in changed_product_numbers])

    outcome = PhaseOutcome.PARTIAL if errors else PhaseOutcome.OK
    return PhaseResult(
        phase=Phase.DETAILS, outcome=outcome,
        counts={"fetched": fetched_count, "failed": len(errors)},
        ...
    )
```

Products whose detail fetch fails keep their previously-known detail fields from the catalog search response; the fully-rich detail just lags one cycle. Next sync will retry them (since `field_hash` remains unchanged, they won't be picked up unless they actually change again — so retry isn't automatic; might want explicit "force refetch stale detail" list in v2).

## Phase D — details

Delegates to `EmbeddingClient` (see [09_embedding_service.md](./09_embedding_service.md)) for the HTTP calls. Orchestrator's responsibility:

```python
async def run_phase_d(orchestrator_ctx, candidate_product_numbers: list[str]) -> PhaseResult:
    # 1. Wait for sb-embed
    if not await orchestrator_ctx.embedding_client.ready(timeout_s=300):
        log.warning("phase_d_skipped_embed_unavailable")
        return PhaseResult(Phase.EMBED, PhaseOutcome.SKIPPED, ...)

    # 2. Validate dimension match (catches Ollama-swap-without-reembedding scenarios)
    if not await _validate_embed_dim(orchestrator_ctx):
        raise CatastrophicError(
            f"embed service serves different dim than SB_EMBED_DIM={orchestrator_ctx.settings.embed_dim}"
        )

    # 3. Build text for each candidate using category-specific templates
    to_embed = []
    with db.reader() as conn:
        for pn in candidate_product_numbers:
            row = conn.execute(
                "SELECT * FROM products WHERE product_number = ?", [pn]
            ).fetchone()
            rendered = render_embedding_text(row)  # from embed/templates.py
            if rendered is None:
                continue  # e.g. Presentartiklar — skipped category
            text, version = rendered
            new_hash = source_hash(text)
            existing = conn.execute(
                "SELECT source_hash FROM product_embeddings WHERE product_number = ?",
                [pn],
            ).fetchone()
            if existing and existing[0] == new_hash and not orchestrator_ctx.full_refresh:
                continue
            to_embed.append((pn, text, version, new_hash))

    if not to_embed:
        return PhaseResult(Phase.EMBED, PhaseOutcome.SKIPPED,
                           counts={"embedded": 0}, ...)

    # 4. Batch through the HTTP client
    errors = []
    embedded = 0
    batch_size = orchestrator_ctx.settings.embed_client_batch_size
    with db.writer() as conn:
        for i in range(0, len(to_embed), batch_size):
            batch = to_embed[i : i + batch_size]
            texts = [t for _, t, _, _ in batch]
            try:
                vectors = await orchestrator_ctx.embedding_client.embed(texts)
            except Exception as e:
                errors.append(PhaseError(
                    f"embed batch {i} failed", cause=e,
                ))
                log.warning("embed_batch_failed", start=i, size=len(batch), error=str(e))
                continue
            # Verify dim
            for v in vectors:
                if len(v) != orchestrator_ctx.settings.embed_dim:
                    raise CatastrophicError(
                        f"embed service returned dim {len(v)}, expected {orchestrator_ctx.settings.embed_dim}"
                    )
            # UPSERT
            for (pn, _, version, hash_), vec in zip(batch, vectors):
                conn.execute("""
                    INSERT OR REPLACE INTO product_embeddings
                        (product_number, embedding, source_hash, model_name,
                         template_version, embedded_at)
                    VALUES (?, ?, ?, ?, ?, now())
                """, [pn, vec, hash_, orchestrator_ctx.settings.embed_model, version])
                embedded += 1

    outcome = PhaseOutcome.PARTIAL if errors else PhaseOutcome.OK
    return PhaseResult(
        phase=Phase.EMBED, outcome=outcome,
        counts={"embedded": embedded, "failed_batches": len(errors)},
        ...
    )
```

## Phase E — FTS rebuild

DuckDB's `PRAGMA create_fts_index` does a **drop + recreate** internally. If it fails mid-rebuild the table could be left without an FTS index for longer than expected. Our mitigation:

1. One retry on failure (wait 5 s, try again).
2. If both fail, log, mark phase FAILED. MCP clients handling FTS-missing-errors with the retry wrapper (see [08_mcp_implementation.md](./08_mcp_implementation.md)) will continue to work; the UX is "text search slow for today until tomorrow's rebuild succeeds."
3. A monitoring check: `doctor` subcommand's `fts_index_healthy` verifies the FTS function exists and returns results for a canary query.

```python
async def run_phase_e(orchestrator_ctx, products_touched: int) -> PhaseResult:
    if products_touched == 0:
        return PhaseResult(Phase.INDEX, PhaseOutcome.SKIPPED, ...)

    async def _rebuild():
        with db.writer() as conn:
            conn.execute("PRAGMA drop_fts_index('products');")
            conn.execute("""
                PRAGMA create_fts_index(
                    'products', 'product_number',
                    'name_bold', 'name_thin', 'producer_name', 'country',
                    'taste', 'aroma', 'usage', 'producer_description',
                    stemmer='swedish', stopwords='swedish',
                    lower=1, strip_accents=0
                )
            """)

    try:
        await _rebuild()
        outcome = PhaseOutcome.OK
    except Exception as first_error:
        log.warning("fts_rebuild_failed_first_attempt", error=str(first_error))
        await asyncio.sleep(5)
        try:
            await _rebuild()
            outcome = PhaseOutcome.OK
        except Exception as second_error:
            log.error("fts_rebuild_failed_both_attempts", error=str(second_error))
            outcome = PhaseOutcome.FAILED

    return PhaseResult(Phase.INDEX, outcome=outcome, ...)
```

## Phase F — finalize (always runs)

```python
async def run_phase_f(
    run_id: int,
    phase_results: list[PhaseResult],
    orchestrator_ctx,
):
    status = overall_status(phase_results)
    total_duration_ms = sum(r.duration_ms for r in phase_results)

    # 1. Update sync_runs row
    counts = merge_counts(phase_results)
    with db.writer() as conn:
        conn.execute("""
            UPDATE sync_runs
               SET finished_at = now(),
                   status = ?,
                   products_added = ?,
                   products_updated = ?,
                   products_discontinued = ?,
                   stock_rows_updated = ?,
                   embeddings_generated = ?,
                   error = ?
             WHERE run_id = ?
        """, [
            status,
            counts.get("products_added", 0),
            counts.get("products_updated", 0),
            counts.get("products_discontinued", 0),
            counts.get("stock_rows_updated", 0),
            counts.get("embeddings_generated", 0),
            _summarize_errors(phase_results),
            run_id,
        ])
        conn.execute("PRAGMA checkpoint;")

    # 2. Backup DB (best-effort; don't fail run if backup fails)
    try:
        await _backup_db(orchestrator_ctx.settings)
    except Exception as e:
        log.warning("backup_failed", error=str(e))

    # 3. Retention cleanup
    try:
        await _cleanup_old_backups(orchestrator_ctx.settings)
        await _cleanup_old_raw(orchestrator_ctx.settings)
    except Exception as e:
        log.warning("retention_cleanup_failed", error=str(e))

    # 4. Write metrics file
    try:
        await _write_metrics(orchestrator_ctx.settings, phase_results, status)
    except Exception as e:
        log.warning("metrics_write_failed", error=str(e))

    # 5. Release lockfile
    orchestrator_ctx.lockfile.release()

    log.info("sync_run_completed",
             run_id=run_id, status=status,
             duration_ms=total_duration_ms,
             counts=counts)
```

### DB backup

Copy `/data/sb.duckdb` to `/data/backup/sb.duckdb.YYYY-MM-DD`. Hardlinks won't work (DuckDB appends WAL); use `shutil.copy2`. Then delete backups older than `SB_BACKUP_RETENTION_DAYS`.

### Raw archive retention

```python
async def _cleanup_old_raw(settings: Settings) -> int:
    cutoff = date.today() - timedelta(days=settings.raw_retention_days)
    deleted = 0
    for entry in settings.raw_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            entry_date = date.fromisoformat(entry.name)
        except ValueError:
            continue  # not a dated archive dir
        if entry_date < cutoff:
            await asyncio.to_thread(shutil.rmtree, entry)
            deleted += 1
            log.info("raw_archive_removed", date=entry.name)
    return deleted
```

Defensive: only remove directories whose name parses as a date. A hand-created `raw/test/` is preserved.

## Lockfile

Prevents two sync runs stepping on each other (e.g., user manually invokes while scheduler fires).

```python
# src/sb_stack/sync/lockfile.py

class Lockfile:
    def __init__(self, path: Path, stale_after_hours: int = 6):
        self.path = path
        self.stale_after_hours = stale_after_hours

    def acquire(self) -> None:
        """Raises LockError if another run holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            content = self.path.read_text().splitlines()
            pid, start_iso = content[0], content[1]
            start = datetime.fromisoformat(start_iso)
            age = (datetime.utcnow() - start).total_seconds() / 3600
            if age < self.stale_after_hours and _pid_alive(int(pid)):
                raise LockError(
                    f"another sync is running (pid={pid}, started {age:.1f}h ago)"
                )
            log.warning("stale_lockfile_taken_over",
                        age_hours=age, old_pid=pid)
        self.path.write_text(f"{os.getpid()}\n{datetime.utcnow().isoformat()}\n")

    def release(self) -> None:
        try:
            if self.path.exists():
                content = self.path.read_text().splitlines()
                if content and int(content[0]) == os.getpid():
                    self.path.unlink()
        except Exception as e:
            log.warning("lockfile_release_error", error=str(e))

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # can't signal but it's alive somewhere
```

Path: `/data/state/lockfile`. Stale-lock threshold: 6 hours (> longest expected first-run duration of ~50 min).

## Scheduler

```python
# src/sb_stack/sync/scheduler.py

async def run_scheduler(orchestrator_ctx: OrchestratorContext):
    settings = orchestrator_ctx.settings
    log.info("sync_scheduler_starting", cron=settings.sync_cron,
             tz=settings.sync_timezone)

    # 1. Wait for sb-embed (other phases work without it, but Phase D needs it)
    log.info("waiting_for_embedding_service")
    await orchestrator_ctx.embedding_client.wait_ready(timeout_s=600)

    # 2. Bootstrap: first-run check
    if settings.first_run_on_bootstrap and await _needs_first_run(orchestrator_ctx):
        log.info("first_run_starting")
        asyncio.create_task(_run_with_logging(
            orchestrator_ctx, full_refresh=True, reason="first_run_bootstrap"
        ))

    # 3. Register cron
    scheduler = AsyncIOScheduler(timezone=settings.sync_timezone)
    scheduler.add_job(
        lambda: _run_with_logging(orchestrator_ctx, reason="cron"),
        trigger=CronTrigger.from_crontab(settings.sync_cron,
                                         timezone=settings.sync_timezone),
        id="sync",
        misfire_grace_time=3600,   # 1 hour: if container was down during cron,
                                    # run as soon as we come up (if within 1h)
        coalesce=True,              # if multiple fires were missed, run once
        max_instances=1,            # never run two concurrently
    )
    scheduler.start()
    log.info("sync_scheduler_started", next_fire=scheduler.get_job("sync").next_run_time)

    # 4. Keep running
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_event_loop().add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("sync_scheduler_stopping")
    scheduler.shutdown(wait=True)

async def _run_with_logging(orchestrator_ctx, *, reason: str, **kwargs):
    t_start = time.monotonic()
    try:
        result = await run_sync(**kwargs)
        log.info("sync_run_finished", reason=reason, status=result.status,
                 duration_s=int(time.monotonic() - t_start))
    except LockError as e:
        log.warning("sync_run_skipped_locked", reason=reason, error=str(e))
    except CatastrophicError as e:
        log.error("sync_run_catastrophic",
                  reason=reason, error=str(e), alert=True)
    except Exception as e:
        log.exception("sync_run_unhandled", reason=reason)
```

### misfire handling

`misfire_grace_time=3600` means: if container was down at the scheduled cron time, and it comes back up within 1 hour, the missed run still fires. After 1 hour, we wait for the next natural cron tick. 1 hour is tuned to tolerate planned TrueNAS reboots (~10 min typical) but not let a multi-day outage flood us with retries.

`coalesce=True` ensures multiple missed fires collapse into one catch-up run.

`max_instances=1` is a soft guard — the real enforcement is the lockfile.

## Replay mode (`--from-raw`)

Reconstruct DB state from an archived raw/ directory. Skips Phase A (network), Phase D (unless `--re-embed`), Phase E (only if products changed).

Use cases:
- Schema migration: added a column; replay last night's raw to repopulate without burning API calls.
- Debugging: "what did yesterday's data actually contain?"
- Recovery: DB corrupted, last backup is stale, but raw archive has recent data.

```python
async def run_sync_from_raw(
    date_: date,
    *,
    re_embed: bool = False,
) -> SyncRunResult:
    raw_dir = settings.raw_dir / date_.isoformat()
    if not raw_dir.exists():
        raise SyncError(f"no raw archive for {date_}")

    # Reuse Phase B with a different source of data
    run_id = await _start_run(mode="replay", source_date=date_)
    phase_results: list[PhaseResult] = []
    try:
        # Phase A: "synthetic" — report that catalog/stock/etc are loaded from raw
        phase_results.append(_synthetic_phase_a(raw_dir))
        # Phase B: normal persist, reading from raw
        phase_results.append(await run_phase_b(ctx, raw_dir))
        # Phase C: load product details from raw_dir/details/ (skip network)
        phase_results.append(await run_phase_c_from_raw(ctx, raw_dir))
        # Phase D: only if --re-embed (network to sb-embed)
        if re_embed:
            phase_results.append(await run_phase_d(ctx, ...))
        # Phase E: always
        phase_results.append(await run_phase_e(ctx, ...))
    finally:
        await run_phase_f(run_id, phase_results, ctx)

    return SyncRunResult(run_id=run_id, status=overall_status(phase_results))
```

The `source_date` is recorded in `sync_runs.error` field as `"replay from 2026-04-18"` so we can distinguish replay runs from genuine ones.

## Dry-run mode (`--dry-run`)

Runs Phase A (to see what's current), runs Phase B's *diff computation* without writes, emits a report.

```python
async def run_sync_dry(orchestrator_ctx) -> DryRunReport:
    # Phase A (real fetch, raw archived)
    phase_a = await run_phase_a(orchestrator_ctx)

    # Phase B diff in memory, no DB writes
    with db.reader() as conn:
        diff = compute_diff_in_memory(conn, raw_dir)

    return DryRunReport(
        products_would_be_added=diff.new_products,
        products_would_be_updated=len(diff.updated_products),
        products_would_be_discontinued=len(diff.discontinued_products),
        stock_changes=len(diff.stock_deltas),
        embedding_rebuilds_needed=diff.embed_text_changes,
        top_changed_fields=diff.top_10_field_change_counts,
        stock_per_store={site_id: count for site_id, count in diff.stock_by_store.items()},
    )
```

### Output formats

**Default: text** (human readable). Example:

```
$ sb-stack sync --dry-run

Dry-run report  (fetched 2026-04-19 04:00:15 UTC, 57 s)
─────────────────────────────────────────────────────────
Products
  would add ............. 7
  would update .......... 143
    top fields: price_incl_vat×89, comparison_price×89,
                taste_clock_body×12, assortment_text×9
  would discontinue ..... 2   (642008, 471207)

Stock
  1701  Duvan ............ +12 changes  (+8 restocked, -4 depleted)
  1702  Bergvik-Karlstad ..  +5
  1716  Skoghall .........  +2
  1718  Välsviken ........  +9

Embeddings
  would recompute ....... 12

No writes performed.  Run without --dry-run to apply.
```

**JSON** (via `--json`):

```
$ sb-stack sync --dry-run --json
{
  "fetched_at": "2026-04-19T04:00:15Z",
  "fetch_duration_seconds": 57.3,
  "products": {
    "would_add": 7,
    "would_update": 143,
    "would_discontinue": 2,
    "discontinued_product_numbers": ["642008", "471207"]
  },
  "top_changed_fields": {
    "price_incl_vat": 89,
    "comparison_price": 89,
    "taste_clock_body": 12,
    "assortment_text": 9
  },
  "stock": {
    "1701": {"changes": 12, "restocked": 8, "depleted": 4},
    "1702": {"changes": 5, "restocked": 4, "depleted": 1},
    "1716": {"changes": 2, "restocked": 1, "depleted": 1},
    "1718": {"changes": 9, "restocked": 7, "depleted": 2}
  },
  "embeddings": {"would_recompute": 12}
}
```

Useful for validating pairing-template tweaks: change the wine template, dry-run, see "12,345 wines would be re-embedded" and decide if that's what you wanted.

Dry-run does not update `sync_runs` (it's a read).

## CLI summary

### Run commands

```
sb-stack sync                         # normal nightly run
sb-stack sync --full-refresh          # force Phase C + D for all products
sb-stack sync --from-raw=2026-04-18   # replay a past day
sb-stack sync --from-raw=2026-04-18 --re-embed
                                       # replay + re-embed (calls sb-embed)
sb-stack sync --phase=fetch           # Phase A only (then exit)
sb-stack sync --phase=persist         # Phases B only (using TODAY's raw/)
sb-stack sync --phase=details
sb-stack sync --phase=embed
sb-stack sync --phase=fts
sb-stack sync --dry-run               # Phase A + in-memory diff, no writes (text output)
sb-stack sync --dry-run --json        # same, JSON output

sb-stack sync-scheduler               # long-running; calls `sync` on cron
```

Flags compose: `--from-raw=... --phase=embed` replays embedding from a specific day, etc.

### Inspect commands

```
sb-stack runs [--limit N]             # list recent runs with run_id → timestamp
sb-stack run-info <run-id>            # full details for one run (phase breakdown)
```

Example:

```
$ sb-stack runs --limit 5

  run_id  started_at           status     duration   products
      42  2026-04-19 04:00:15  success         287s  +7  ~143  -2
      41  2026-04-18 04:00:12  success         291s  +3  ~89   -0
      40  2026-04-17 04:00:08  partial         312s  +0  ~51   -0  (embed skipped)
      39  2026-04-16 04:00:05  success         278s  +5  ~120  -1
      38  2026-04-15 04:00:02  success         301s  +2  ~95   -0


$ sb-stack run-info 42

  Run 42
  Started ................. 2026-04-19 04:00:15 UTC
  Finished ................ 2026-04-19 04:04:42 UTC
  Duration ................ 287 seconds
  Status .................. success

  Phase breakdown
    fetch      58 s  ok         1,203 calls
    persist    12 s  ok         143 updates, 7 inserts
    details   124 s  ok         152 fetched
    embed      85 s  ok         12 embeddings
    fts         5 s  ok         rebuilt
    finalize    1 s  ok         backup + retention ran

  Counts
    products added ........ 7
    products updated ...... 143
    products discontinued . 2
    stock rows updated .... 28
    embeddings generated .. 12

  Error ................... (none)
```

Both subcommands are read-only and work against the live DB or a backup.

## Observability

### `sync_runs` table — authoritative log

One row per invocation (including replay and dry-run). Queried by MCP's `sync_status` tool.

### Structured logs — every phase

```
sync_run_started        run_id=42 reason=cron full_refresh=false
phase_a_started         run_id=42
catalog_page_fetched    run_id=42 category=Vin page=17 items=30 duration_ms=312
stock_fetched           run_id=42 site_id=1701 pages=72 duration_ms=18422
phase_a_finished        run_id=42 outcome=ok duration_ms=58321 errors=0
phase_b_started         run_id=42
phase_b_finished        run_id=42 outcome=ok duration_ms=12104 products_updated=143
...
sync_run_finished       run_id=42 status=success duration_ms=287154
```

All tee'd to `/data/logs/sb-sync.log` (rotated daily, 30 days) and to stdout (Docker collection).

### Metrics file (`/data/state/metrics.prom`)

Prometheus text format, overwritten at end of each run. Minimal set:

```
# HELP sb_sync_last_run_timestamp_seconds Unix time of the last sync run attempt
# TYPE sb_sync_last_run_timestamp_seconds gauge
sb_sync_last_run_timestamp_seconds 1713463200

# HELP sb_sync_last_success_timestamp_seconds Unix time of the last successful sync run
# TYPE sb_sync_last_success_timestamp_seconds gauge
sb_sync_last_success_timestamp_seconds 1713463200

# HELP sb_sync_last_status The status of the last run (0=success, 1=partial, 2=failed)
# TYPE sb_sync_last_status gauge
sb_sync_last_status 0

# HELP sb_sync_duration_seconds Total wall time of the last run
# TYPE sb_sync_duration_seconds gauge
sb_sync_duration_seconds 287.15

# HELP sb_sync_consecutive_failures Count of consecutive non-success runs
# TYPE sb_sync_consecutive_failures gauge
sb_sync_consecutive_failures 0

# HELP sb_sync_products Current products tracked
# TYPE sb_sync_products gauge
sb_sync_products 27312

# HELP sb_sync_phase_duration_seconds Duration by phase
# TYPE sb_sync_phase_duration_seconds gauge
sb_sync_phase_duration_seconds{phase="fetch"}    58.32
sb_sync_phase_duration_seconds{phase="persist"}  12.10
sb_sync_phase_duration_seconds{phase="details"}  84.51
sb_sync_phase_duration_seconds{phase="embed"}    127.92
sb_sync_phase_duration_seconds{phase="fts"}       3.14
sb_sync_phase_duration_seconds{phase="finalize"}  1.16
```

Not a running exporter — just a file. If the user wants Prometheus scraping, they can use `textfile collector` or similar.

### Alert-level logs

Certain log events include `alert=true` in their structured context. Same events also feed the ntfy notifier (see next).

- `sync_run_catastrophic` — any catastrophic failure
- `api_key_invalid_after_refresh` — key rotation failed
- `consecutive_failures_threshold` — two back-to-back non-success runs
- `stale_data_warning` — emitted at sync start if last success > 30 h ago

Users who want richer routing can also grep for `alert=true` in journalctl/Loki and pipe to a custom notifier.

## Ntfy alerts (opt-in)

For severe events we fire a notification to an [ntfy.sh](https://ntfy.sh) topic. Built to be quiet: fires only on state *transitions*, with a per-alert-key cooldown, so a persistent failure doesn't page you every night.

### Configuration

```
SB_NTFY_URL=                     # e.g. https://ntfy.sh/my-sb-topic
                                 # or https://ntfy.internal.lan/sb-stack
                                 # unset → notifier disabled entirely
SB_NTFY_TOKEN=                   # optional bearer token (private ntfy server)
SB_NTFY_COOLDOWN_HOURS=6         # minimum time between same-key alerts
SB_NTFY_MIN_PRIORITY=3           # 1-5; don't send below this
```

### Events and priorities

| Event | Trigger | Priority | Cooldown applies? |
|---|---|---|---|
| `sync_failing` | First non-success after a success streak | 3 (default) | yes |
| `sync_repeatedly_failing` | Transition from <2 to ≥2 consecutive failures | 5 (urgent) | yes |
| `sync_recovered` | First success after ≥1 consecutive failures | 2 (low) | no |
| `api_key_invalid` | Both cached and freshly-scraped keys 401 | 5 (urgent) | yes (24h) |
| `data_very_stale` | Last success > 48h ago (checked at every run start) | 4 | yes (12h) |
| `migration_integrity_violation` | Any applied migration's sha256 drifted | 5 | no (one-shot) |

### State-transition model

Key insight for spam avoidance: **we alert on state changes, not on persistent conditions.** If sync fails tonight, tomorrow, and the night after, we fire the "failing" alert once (when it first broke) and the "repeatedly failing" alert once (when it crossed the threshold). No further pings until it either recovers (fires `sync_recovered`) or crosses another threshold.

```python
# src/sb_stack/notifications/alerts.py

class AlertManager:
    """
    Fires ntfy notifications only on state transitions. Tracks
    consecutive-failure counter + per-alert-key cooldowns in
    /data/state/alerts.json.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.state_path = settings.state_dir / "alerts.json"
        self.state = self._load()

    async def evaluate(self, sync_result: SyncRunResult) -> None:
        """Called at end of every sync run."""
        prev_consec = self.state["consecutive_failures"]
        new_consec = (prev_consec + 1) if sync_result.status != "success" else 0
        self.state["consecutive_failures"] = new_consec

        # Transitions
        if prev_consec == 0 and new_consec == 1:
            await self._fire(
                key="sync_failing",
                title="Systembolaget sync: failed",
                message=f"Run {sync_result.run_id} ended with "
                        f"status={sync_result.status}. See logs.",
                priority=3, tags=["warning"],
            )
        elif prev_consec < 2 and new_consec >= 2:
            await self._fire(
                key="sync_repeatedly_failing",
                title="Systembolaget sync: {} consecutive failures"
                      .format(new_consec),
                message=f"Multiple runs failed. Latest: {sync_result.run_id}. "
                        f"Manual intervention likely needed.",
                priority=5, tags=["rotating_light"],
            )
        elif prev_consec > 0 and new_consec == 0:
            await self._fire(
                key="sync_recovered",
                title="Systembolaget sync: recovered",
                message=f"Back to success after {prev_consec} failure(s).",
                priority=2, tags=["white_check_mark"],
                ignore_cooldown=True,   # recoveries always fire
            )

        self._save()

    async def fire_critical(self, key: str, title: str, message: str):
        """For catastrophic single events (migration drift, key revocation)."""
        await self._fire(key=key, title=title, message=message,
                         priority=5, tags=["no_entry"])

    async def _fire(
        self, *, key: str, title: str, message: str,
        priority: int, tags: list[str],
        ignore_cooldown: bool = False,
    ):
        if not self.settings.ntfy_url:
            return
        if priority < self.settings.ntfy_min_priority:
            return
        if not ignore_cooldown:
            last = self.state["last_sent"].get(key)
            if last:
                age = datetime.utcnow() - datetime.fromisoformat(last)
                if age < timedelta(hours=self.settings.ntfy_cooldown_hours):
                    log.info("ntfy_suppressed_cooldown", key=key,
                             age_hours=round(age.total_seconds() / 3600, 1))
                    return
        try:
            await self._send(title, message, priority, tags)
            self.state["last_sent"][key] = datetime.utcnow().isoformat()
            log.info("ntfy_sent", key=key, priority=priority)
        except Exception as e:
            # Notifier failure must never break sync
            log.warning("ntfy_send_failed", key=key, error=str(e))

    async def _send(
        self, title: str, message: str, priority: int, tags: list[str],
    ):
        headers = {
            "Title": title,
            "Priority": str(priority),
            "Tags": ",".join(tags),
        }
        if self.settings.ntfy_token:
            headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                self.settings.ntfy_url, content=message, headers=headers
            )
            r.raise_for_status()
```

### Self-hosting note

For a private home network, ntfy is trivially self-hostable (single Go binary, `docker run binwiederhier/ntfy`). A lot of TrueNAS users already run it. If the user doesn't want to run one, `ntfy.sh` is fine for low-volume personal alerts — just use a non-guessable topic name.

### What is *never* alerted

- Partial syncs with only a handful of per-item errors (noisy, not actionable; check the logs).
- Individual product-detail fetch failures (routine; retried next run).
- sb-embed being slow on first boot (expected).
- Taxonomy fetch failing (low impact).

Everything listed here is `log.warning` level; ntfy doesn't see it.

## Concurrency model recap

Inside a single sync run:
- Phase A uses `asyncio.TaskGroup` for parallel sub-fetches with `asyncio.Semaphore(5)` per bucket.
- Phases B, E run serially (single DuckDB writer).
- Phase C uses `asyncio.Semaphore(5)` for per-product detail fetches.
- Phase D batches HTTP calls; GPU-side concurrency is handled inside `sb-embed`.

Between runs:
- Lockfile prevents two sync runs concurrently.
- DuckDB WAL allows MCP readers to continue unaffected during all of this.
- sb-embed is single-model-single-GPU; requests queue.

## First-run vs incremental (finalized timings)

| Phase | First run | Daily run |
|---|---|---|
| A (fetch) | ~60 s | ~60 s |
| B (persist) | ~15 s (27k INSERTs) | ~3 s |
| C (details) | ~30 min (27k fetches) | ~1 min (~100-500 fetches) |
| D (embed) | ~20 min (HTTP batching + GPU) | ~20 s (<100 products) |
| E (FTS) | ~3 s | ~3 s |
| F (finalize) | ~1 s | ~1 s |
| **Total** | **~52 min** | **~3 min** |

## Resolved decisions (from Step 3 open questions)

1. **Alerting** → ntfy for severe events, state-transition-based, per-key 6h cooldown, silent when `SB_NTFY_URL` unset. Full design in §"Ntfy alerts" above.
2. **Auto-replay on missed days** → no. Replay is always manual via `--from-raw`.
3. **Per-phase timeouts** → env-configurable via `SB_PHASE_*_TIMEOUT_MINUTES` (see §"Env-configurable" above).
4. **Dry-run output** → text by default, `--json` flag for machine-readable.
5. **Run ID allocation** → auto-increment via DuckDB sequence; mapped to timestamp via `started_at` column. `sb-stack runs` and `sb-stack run-info` CLI subcommands expose the mapping (see §"Inspect commands").

Next: [Step 4 — observability + testing + dev workflow](./11_observability_and_testing.md).
