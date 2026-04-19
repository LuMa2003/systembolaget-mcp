# 07 — Deployment

Single Docker container on TrueNAS Scale with GPU passthrough. One persistent volume. Update by re-pulling the image.

## Container architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ Container: sb-stack                                               │
│ Base: pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime               │
│                                                                    │
│    ┌────────────────────────────────────┐                          │
│    │ s6-overlay (init, PID 1)           │                          │
│    └────┬──────────────┬─────────────┬──┘                          │
│         │              │             │                             │
│    ┌────▼────┐    ┌────▼──────┐ ┌────▼────────┐                   │
│    │ sb-mcp  │    │ sb-sync-  │ │ sb-embed    │                   │
│    │ :8000   │    │ scheduler │ │ :9000 (lo)  │                   │
│    │ fastmcp │    │APScheduler│ │ FastAPI     │                   │
│    │         │    │           │ │ Qwen3 on GPU│                   │
│    └────┬────┘    └────┬──────┘ └─────▲───────┘                   │
│         │ reads         │ writes      │ HTTP /v1/embeddings       │
│         ▼               ▼             │                            │
│    ┌────────────────────────────┐     │                            │
│    │ /data  (persistent volume) │─────┘                            │
│    │   sb.duckdb                │                                  │
│    │   raw/YYYY-MM-DD/*.json.gz │                                  │
│    │   backup/sb.duckdb.YYYY-...│                                  │
│    │   models/qwen3-embedding-4b/                                  │
│    │   logs/                    │                                  │
│    │   state/                   │                                  │
│    └────────────────────────────┘                                  │
│                                                                    │
│ GPU: NVIDIA_VISIBLE_DEVICES=all  (1080 Ti passthrough → sb-embed) │
└───────────────────────────────────────────────────────────────────┘
```

## Process supervision: s6-overlay

Chosen over APScheduler-in-process because crashed sync runs must not take down the MCP server. s6 restarts each service independently, handles signals cleanly, adds ~10 MB to the image.

**Three** services:
- **`sb-embed`** — long-running FastAPI embedding server (must be up before dependents); binds `localhost:9000` only. See [09_embedding_service.md](./09_embedding_service.md).
- **`sb-mcp`** — long-running HTTP MCP server; depends on `sb-embed`.
- **`sb-sync-scheduler`** — long-running APScheduler that fires `sb-stack sync` on cron; depends on `sb-embed`.

Both `sb-mcp` and `sb-sync-scheduler` have `sb-embed` listed in their `dependencies.d/`. s6 brings `sb-embed` up first. The dependents then wait for `GET /health` to return 200 before they do any embedding-requiring work (the service's own dependency readiness is "running", but the model takes 60–90 s to load — see embedding-service doc).

s6 service definitions live in `deploy/s6-rc.d/` and are copied into the image during build.

Example layout:

```
/etc/s6-overlay/s6-rc.d/
├── user/
│   └── contents.d/
│       ├── sb-embed
│       ├── sb-mcp
│       └── sb-sync-scheduler
├── sb-embed/
│   ├── type               (contains "longrun")
│   ├── run                (exec sb-stack embed-server)
│   └── dependencies.d/
│       └── base
├── sb-mcp/
│   ├── type
│   ├── run                (exec sb-stack mcp)
│   └── dependencies.d/
│       ├── base
│       └── sb-embed
└── sb-sync-scheduler/
    ├── type
    ├── run                (exec sb-stack sync-scheduler)
    └── dependencies.d/
        ├── base
        └── sb-embed
```

## Dockerfile (sketch)

```dockerfile
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SB_DATA_DIR=/data \
    HF_HOME=/data/models \
    TRANSFORMERS_CACHE=/data/models

# s6-overlay
ARG S6_OVERLAY_VERSION=3.2.0.2
RUN curl -L https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz \
    | tar -C / -Jxpf - \
 && curl -L https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz \
    | tar -C / -Jxpf -

# uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install deps (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# App
COPY src/ ./src/
COPY deploy/entrypoint.sh /entrypoint.sh
COPY deploy/s6-rc.d /etc/s6-overlay/s6-rc.d

RUN chmod +x /entrypoint.sh \
 && find /etc/s6-overlay/s6-rc.d -name run -exec chmod +x {} \; \
 && uv sync --frozen

EXPOSE 8000

HEALTHCHECK --interval=5m --timeout=30s --start-period=2m --retries=3 \
  CMD sb-stack doctor --only db_reachable,api_key_valid,disk_space --exit-on-warn

ENTRYPOINT ["/init"]    # s6-overlay's init
CMD ["/entrypoint.sh"]
```

The container uses s6 as PID 1 (`/init`); `entrypoint.sh` only runs one-time bootstrap (migrations, home-store seeding) before s6 brings up the services.

## docker-compose (reference)

```yaml
services:
  sb-stack:
    image: sb-stack:latest
    restart: unless-stopped
    runtime: nvidia                    # requires nvidia-container-toolkit
    environment:
      - SB_STORE_SUBSET=1701,1702,1716,1718
      - SB_MAIN_STORE=1701
      - SB_MCP_TOKEN=<generate-strong-random>
      - SB_SYNC_CRON=0 4 * * *
      - SB_SYNC_TIMEZONE=Europe/Stockholm
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility
    ports:
      - "8000:8000"
    volumes:
      - /mnt/tank/sb-data:/data
    healthcheck:
      test: ["CMD", "sb-stack", "doctor",
             "--only", "db_reachable,api_key_valid,disk_space",
             "--exit-on-warn"]
      interval: 5m
      timeout: 30s
      start_period: 2m
      retries: 3
```

TrueNAS Scale "Custom App" accepts this shape almost verbatim under its "Install Custom App" / docker-compose YAML option.

## Environment variables (full list)

```
# Storage
SB_DATA_DIR=/data                     # volume mount point

# Home stores
SB_STORE_SUBSET=1701,1702,1716,1718   # comma-separated siteIds
SB_MAIN_STORE=1701                    # user's walkable store

# API
SB_API_KEY=                           # blank → auto-extract from frontend
SB_API_KEY_CACHE_TTL_DAYS=7
SB_API_BASE_URL=https://api-extern.systembolaget.se
SB_APP_BASE_URL=https://www.systembolaget.se

# Embedding (service)
SB_EMBED_URL=http://localhost:9000/v1/embeddings   # where clients call; point at Ollama etc. if desired
SB_EMBED_MODEL=Qwen/Qwen3-Embedding-4B
SB_EMBED_DIM=2560
SB_EMBED_DEVICE=cuda:0                              # used by the sb-embed service
SB_EMBED_PORT=9000                                   # used by the sb-embed service
SB_EMBED_MAX_BATCH=2048                              # server hard limit (per request)
SB_EMBED_GPU_BATCH_SIZE=32                           # server internal GPU batching
SB_EMBED_CLIENT_BATCH_SIZE=128                       # client-side chunking

# MCP
SB_MCP_PORT=8000
SB_MCP_TRANSPORT=http                 # or "stdio"
SB_MCP_TOKEN=                         # required when transport=http

# Sync
SB_SYNC_CRON=0 4 * * *                # cron expression
SB_SYNC_TIMEZONE=Europe/Stockholm
SB_FIRST_RUN_ON_BOOTSTRAP=true
SB_SYNC_CONCURRENCY=5

# Retention
SB_BACKUP_RETENTION_DAYS=7
SB_RAW_RETENTION_DAYS=365

# Phase timeouts (minutes; raise if you see PhaseTimeoutError in logs)
SB_PHASE_FETCH_TIMEOUT_MINUTES=20
SB_PHASE_PERSIST_TIMEOUT_MINUTES=5
SB_PHASE_DETAILS_TIMEOUT_MINUTES=60
SB_PHASE_EMBED_TIMEOUT_MINUTES=60
SB_PHASE_INDEX_TIMEOUT_MINUTES=2
SB_PHASE_FINALIZE_TIMEOUT_MINUTES=2

# Ntfy (optional; leave SB_NTFY_URL unset to disable)
SB_NTFY_URL=                          # e.g. https://ntfy.sh/my-sb-topic
SB_NTFY_TOKEN=                        # optional; for private ntfy servers
SB_NTFY_COOLDOWN_HOURS=6              # minimum time between same-key alerts
SB_NTFY_MIN_PRIORITY=3                # 1-5; don't send below this

# Logging
SB_LOG_LEVEL=info
SB_LOG_FORMAT=json                    # or "text"
SB_LOG_TO_FILE=true
SB_LOG_TO_STDOUT=true
```

`.env.example` at repo root ships with safe defaults; users copy to `.env`.

## Persistent volume layout

```
/data/
├── sb.duckdb                          # live DB
├── sb.duckdb.wal                      # DuckDB WAL (auto-managed)
├── duckdb_extensions/                 # persistent vss/fts cache
├── raw/
│   └── YYYY-MM-DD/                    # 365-day rolling
├── backup/
│   ├── sb.duckdb.YYYY-MM-DD           # 7-day rolling
│   └── pre-migration/
│       └── sb.duckdb.pre-NNN          # one-time snapshots before each migration
├── models/
│   └── qwen3-embedding-4b/            # HF cache (~8 GB; downloads on first run)
├── logs/
│   ├── sb-sync.log                    # rotated daily, 30-day retention
│   └── sb-mcp.log
└── state/
    ├── lockfile                       # sync process lock
    └── last_run_id                    # monotonic counter
```

Single mount point `/data` → TrueNAS dataset. Backup by snapshotting the dataset.

## Bootstrap flow

```
┌─ container start (PID 1: s6 /init) ────────────────────────────┐
│                                                                 │
│  Pre-s6: entrypoint.sh runs                                     │
│    1. Ensure /data subdirectories exist                         │
│    2. Apply chown if needed                                     │
│    3. Invoke `sb-stack migrate` (forward-only, idempotent)      │
│    4. If bootstrap flag needed (first_run_completed_at missing  │
│       in sync_config):                                          │
│         a. `sb-stack bootstrap` seeds stores.is_home_store      │
│            from SB_STORE_SUBSET                                 │
│         b. marks SB_MAIN_STORE as is_main_store=true            │
│         c. if SB_FIRST_RUN_ON_BOOTSTRAP:                         │
│              kicks off a `sb-stack sync --full-refresh`         │
│              in background                                       │
│                                                                 │
│  Then s6 starts services:                                       │
│    5. sb-mcp service starts                                     │
│       a. MigrationRunner.verify()                               │
│       b. Load DB in read-only                                   │
│       c. Start HTTP server on port 8000                         │
│       d. sync_status tool reports "initializing" while the      │
│          bootstrap sync is running                              │
│                                                                 │
│    6. sb-sync-scheduler service starts                          │
│       a. Wait for bootstrap sync to finish (if any)             │
│       b. Register APScheduler cron                              │
│       c. Wait for next fire                                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

First-run detection: `state/last_run_id` absent AND `sync_runs` table empty AND `sync_config.first_run_completed_at` unset → cold start.

Bootstrap is a separate `sb-stack bootstrap` subcommand (user preference: not a flag on `sync`). It seeds home stores + optionally triggers the first full refresh.

## Resource expectations

| Resource | First-run peak | Steady state |
|---|---|---|
| CPU | 2-4 cores during embedding | idle with occasional spikes |
| RAM | ~5 GB (model + DuckDB buffers + FastAPI) | ~2.5 GB (model resident in sb-embed) |
| GPU VRAM | ~8 GB (Qwen3-Embedding-4B fp16, resident in sb-embed) | ~8 GB (resident) |
| Disk (image) | ~5 GB | same |
| Disk (/data) | ~1.5 GB (model + initial DB + first raw/) | grows ~20 MB/day (raw), ~1 MB/day (DB) |
| Network (first run) | ~500 MB | ~200 MB/night |
| Internal HTTP | — | 1–2k req/night (Phase D) + per-query calls from MCP |

At 1-year steady state: `/data` ~9 GB.

## TrueNAS Scale setup

High-level steps (no CLI snippets since TrueNAS UI changes between versions):

1. Install **NVIDIA driver** via System Settings → Apps → Configure → Enable GPU passthrough for 1080 Ti.
2. Create a ZFS dataset at e.g. `/mnt/tank/sb-data`.
3. Apps → Discover Apps → "Install Custom App" (or equivalent on your TrueNAS version).
4. Paste the `docker-compose.yaml` shown above.
5. Generate a strong bearer token (`openssl rand -hex 32`) and set `SB_MCP_TOKEN`.
6. Set volume mapping `/mnt/tank/sb-data → /data`.
7. Assign GPU passthrough.
8. Apply.

First boot downloads: the Docker image (~2 GB compressed) and on first sync, the Qwen3 model weights (~8 GB) into `/data/models/`.

## Update procedure

1. `docker pull sb-stack:latest` (or pinned tag).
2. Apps → sb-stack → Restart.
3. Container boots → entrypoint runs `sb-stack migrate` → any pending migrations apply → services start.

No migration rollback needed; our DB always has a `pre-migration` snapshot from before the last migration batch.

## Observability hooks

- **Docker logs** (`docker logs sb-stack`): tail the JSON structlog output for both services, viewable in TrueNAS UI or `docker logs`.
- **File logs** (`/data/logs/sb-{mcp,sync}.log`): long-form, daily-rotated, 30-day retention. Same content as stdout.
- **Healthcheck** (Docker-native): lightweight `sb-stack doctor` subset every 5 min.
- **MCP `sync_status` tool**: queryable via any MCP client; reports freshness, last-run status, stale flag.
- **DuckDB `sync_runs` table**: full operational history, queryable via `sb-stack shell` (read-only DuckDB CLI).

## Security considerations

- **Bearer token is the only auth**: rotate `SB_MCP_TOKEN` periodically. Container refuses to start with `SB_MCP_TRANSPORT=http` and no token.
- **No inbound from WAN**: bind MCP to internal interface only if your TrueNAS allows; the container itself doesn't add TLS. If exposing beyond LAN, terminate TLS at a reverse proxy (Caddy / Traefik / nginx).
- **API key extraction**: writes to logs at debug level with the first 8 chars masked (e.g. `8d39a734***masked***`). Full key only present in DB config table (at rest on volume).
- **Third-party keys in extracted config** (Episerver, Google Maps, Adyen) are discarded by the extractor — we don't store what we don't use.

## Failure modes

| Scenario | Recovery |
|---|---|
| Container killed mid-sync | Next scheduled run picks up; raw/ archives allow replay via `sync --from-raw=YYYY-MM-DD` |
| DuckDB file corrupted | Restore from `/data/backup/sb.duckdb.YYYY-MM-DD` (last 7 days); re-run sync to catch up |
| Model cache deleted | sb-embed re-downloads weights (~8 GB) on next start; only affects startup time |
| Subscription key rotated | Extractor picks up new key on next sync (within 7 days) or immediately on 401 |
| TrueNAS restart | Container restarts via `restart: unless-stopped`; s6 restores services in dependency order |
| sb-embed crashes | s6 restarts it; sb-mcp and sb-sync transparently retry embedding calls once it's back |
| 1080 Ti removed | sb-embed fails to start with CUDA error; sb-mcp returns clear errors on semantic tools; other tools + non-embedding sync phases unaffected |
| Swap to Ollama | Change `SB_EMBED_URL`, disable sb-embed service, re-run sync to re-embed if model dim differs |

## What we DON'T have

Deliberate omissions, per scope:

- No HTTPS termination (add reverse proxy if needed).
- No user authentication beyond bearer token.
- No Prometheus scrape endpoint (file-based metrics only).
- No multi-replica / HA.
- No automated update mechanism inside the container (user pulls manually).
