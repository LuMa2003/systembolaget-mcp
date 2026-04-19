# 12 — Packaging & deployment

Dockerfile, s6 service definitions, entrypoint, first-run flow, TrueNAS deployment.

## Image composition

Single image, CUDA-enabled, three supervised processes. Built from a PyTorch base to avoid wrestling with CUDA installation ourselves.

Final image: **~5.5 GB uncompressed** (~2.1 GB compressed).

Layer composition:

```
pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime   [4.8 GB]
├── s6-overlay + system tools                   [ 30 MB]
├── uv binary                                   [ 15 MB]
├── Python deps (.venv)                         [450 MB]
│   ├── duckdb, fastmcp, httpx, ...
│   └── sentence-transformers (minus model weights)
├── Application source (src/)                   [  1 MB]
└── s6 service definitions                      [ <1 MB]
```

Not baked in (downloaded at first run to persistent volume):
- **Qwen3-Embedding-4B weights (~8 GB)** — HuggingFace cache under `/data/models/hf`. Keeps image lean and lets you swap models without rebuilds.

## Dockerfile

Multi-stage build. Stage 1 uses a slim Python image to build dependencies via `uv` (fast, cacheable). Stage 2 is the runtime with CUDA.

```dockerfile
# syntax=docker/dockerfile:1.7

# ==============================================================
# Stage 1: Build .venv via uv on a slim Python image
# ==============================================================
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

# Install deps first (cache-friendly layer)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Then install the project itself
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ==============================================================
# Stage 2: Runtime with CUDA + s6-overlay
# ==============================================================
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

ARG S6_OVERLAY_VERSION=3.2.0.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    \
    SB_DATA_DIR=/data \
    HF_HOME=/data/models/hf \
    TRANSFORMERS_CACHE=/data/models/hf \
    HUGGINGFACE_HUB_CACHE=/data/models/hf \
    \
    TZ=Europe/Stockholm \
    \
    S6_KEEP_ENV=1 \
    S6_VERBOSITY=1 \
    S6_KILL_GRACETIME=30000 \
    S6_SERVICES_GRACETIME=5000 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=900000

# System packages we need
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates xz-utils tini tzdata \
    && ln -fs /usr/share/zoneinfo/Europe/Stockholm /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

# s6-overlay (supervisor)
RUN curl -fsSL -o /tmp/s6-noarch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
 && curl -fsSL -o /tmp/s6-x86_64.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz" \
 && tar -C / -Jxpf /tmp/s6-noarch.tar.xz \
 && tar -C / -Jxpf /tmp/s6-x86_64.tar.xz \
 && rm /tmp/s6-*.tar.xz

WORKDIR /app

# Import virtualenv from builder
COPY --from=builder /build/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Application source
COPY src/ /app/src/

# s6 service definitions
COPY deploy/s6-rc.d /etc/s6-overlay/s6-rc.d/
COPY deploy/scripts/ /app/deploy/scripts/

# Make scripts executable
RUN find /etc/s6-overlay/s6-rc.d -type f \( -name run -o -name up -o -name finish \) \
        -exec chmod +x {} + \
 && chmod +x /app/deploy/scripts/*.sh

EXPOSE 8000

# Doctor-based healthcheck. start_period is generous because the first
# container boot downloads the Qwen3 model (~8 GB) before sb-embed can serve.
HEALTHCHECK --interval=5m --timeout=30s --start-period=15m --retries=3 \
    CMD sb-stack doctor \
        --only db_reachable,api_key_valid,disk_space,embed_service_reachable \
        --exit-on-warn

# s6-overlay init takes PID 1
ENTRYPOINT ["/init"]
```

### Why these specific choices

- **pytorch/pytorch runtime image** (not `nvidia/cuda:*-runtime`): saves us installing PyTorch + CUDA manually. Adds ~1 GB vs a bare CUDA image but eliminates a class of version-compatibility bugs.
- **Multi-stage with separate Python-slim builder**: keeps the runtime layer clean. `uv` on python-slim is faster than on the pytorch image (fewer things to ignore).
- **`uv sync --frozen --no-dev`** in the builder: deterministic installs from `uv.lock`, skip dev dependencies.
- **`UV_LINK_MODE=copy`**: cross-filesystem copies; required because builder's `/root/.cache/uv` is on a cache mount.
- **`S6_CMD_WAIT_FOR_SERVICES_MAXTIME=900000`** (15 min): first-run model download can take time. Default is 5s.
- **`HEALTHCHECK start_period=15m`**: lets the container stay "starting" during first-run model download. After that, sync progresses in background but doctor subset doesn't depend on sync being done.

### `.dockerignore`

Prevents dev cruft from invalidating layer cache:

```
.git
.github
.venv
__pycache__
.pytest_cache
.mypy_cache
.ruff_cache
dev-data
tests/fixtures/cassettes
docs/
*.md
!README.md
docker-compose*.yaml
.env*
```

## s6-overlay service layout

Four services defined; three run as longruns, one is a one-shot.

```
deploy/s6-rc.d/
├── user/
│   └── contents.d/
│       ├── init-bootstrap           one-shot: migrate + seed
│       ├── sb-embed                 longrun: embedding service
│       ├── sb-mcp                   longrun: MCP HTTP server
│       └── sb-sync-scheduler        longrun: cron scheduler
│
├── init-bootstrap/
│   ├── type                         contents: "oneshot"
│   ├── up                           script to execute
│   └── dependencies.d/
│       └── base                     empty file (built-in bundle)
│
├── sb-embed/
│   ├── type                         contents: "longrun"
│   ├── run
│   └── dependencies.d/
│       └── base
│
├── sb-embed-ready/                  oneshot that polls /health
│   ├── type                         contents: "oneshot"
│   ├── up
│   └── dependencies.d/
│       ├── base
│       └── sb-embed
│
├── sb-mcp/
│   ├── type                         contents: "longrun"
│   ├── run
│   └── dependencies.d/
│       ├── base
│       ├── init-bootstrap
│       └── sb-embed-ready
│
└── sb-sync-scheduler/
    ├── type                         contents: "longrun"
    ├── run
    └── dependencies.d/
        ├── base
        ├── init-bootstrap
        └── sb-embed-ready
```

### File contents

Each `type` file is a single line: `oneshot` or `longrun`.

Each `run` file is a shell script (`#!/bin/bash`) that execs the service.

Each `dependencies.d/<name>` is an **empty file** whose presence declares the dependency.

Each `user/contents.d/<name>` is an **empty file** whose presence includes that service in the default `user` bundle (what s6-rc starts).

### `init-bootstrap/up`

```bash
#!/bin/bash
set -euo pipefail

# Create data subdirectories (idempotent)
mkdir -p /data/raw /data/backup /data/backup/pre-migration \
         /data/models /data/models/hf \
         /data/logs /data/state /data/duckdb_extensions

# Apply schema migrations (forward-only, sha256-verified)
echo "Applying schema migrations..."
/app/.venv/bin/sb-stack migrate

# Seed home stores from SB_STORE_SUBSET (idempotent; uses INSERT OR IGNORE)
echo "Seeding home stores..."
/app/.venv/bin/sb-stack bootstrap

echo "Init bootstrap complete."
```

### `sb-embed/run`

```bash
#!/bin/bash
exec 2>&1
exec /app/.venv/bin/sb-stack embed-server
```

### `sb-mcp/run`

```bash
#!/bin/bash
exec 2>&1
exec /app/.venv/bin/sb-stack mcp
```

### `sb-sync-scheduler/run`

```bash
#!/bin/bash
exec 2>&1
exec /app/.venv/bin/sb-stack sync-scheduler
```

### `sb-embed-ready/up`

```bash
#!/bin/bash
set -e

TIMEOUT_SECONDS="${SB_EMBED_READY_TIMEOUT:-900}"
PORT="${SB_EMBED_PORT:-9000}"

echo "Waiting up to ${TIMEOUT_SECONDS}s for sb-embed on localhost:${PORT}..."
deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))

while (( $(date +%s) < deadline )); do
    if curl -sf "http://localhost:${PORT}/health" 2>/dev/null \
       | grep -q '"status":"ok"'; then
        echo "sb-embed is ready."
        exit 0
    fi
    sleep 2
done

echo "ERROR: sb-embed did not become ready within ${TIMEOUT_SECONDS}s" >&2
exit 1
```

15-minute ceiling is generous: first-ever container boot downloads Qwen3 (~8 GB). Subsequent boots warm from the persistent `/data/models/hf` volume and become-ready in ~15 s.

## Why s6 (vs alternatives)

- **tini alone**: handles signal forwarding but doesn't supervise multiple processes or model dependencies between them.
- **supervisord**: works, but Python-based (adds memory) and older dependency semantics.
- **APScheduler hosting everything in one process**: tempting, but a crashed sync would take down MCP. No process isolation.
- **Multiple containers**: violates the "single Docker container" constraint you set up front.

s6-overlay with s6-rc gives us: dependency ordering, automatic restart of crashed services, fast init (written in C), small footprint (~10 MB), proper signal propagation, health-readiness primitives via oneshots.

## First-run flow (concrete timeline)

For a brand-new container with an empty `/data` volume:

```
T+0s       docker run / compose up
           s6 starts as PID 1

T+1s       init-bootstrap oneshot:
             - mkdir /data/{raw,backup,...}
             - sb-stack migrate → creates sb.duckdb, applies 001_initial.sql
             - sb-stack bootstrap → inserts 4 home stores
             Completes in ~2 s.

T+3s       sb-embed longrun starts:
             - Python loads FastAPI app (0.5 s)
             - SentenceTransformer("Qwen/Qwen3-Embedding-4B")
               → first-ever boot: downloads ~8 GB from HuggingFace
                 (~5-10 min depending on bandwidth)
               → warm boot: loads from /data/models/hf (~10-15 s)
             - Warmup encode on GPU
             - /health starts returning 200

T+5min     (first boot) or T+20s (warm) — sb-embed-ready oneshot succeeds

T+5min+1s  sb-mcp longrun starts:
             - MigrationRunner.verify() — no pending migrations (just applied)
             - FastMCP server listens on :8000
             - MCP ready to serve all non-embedding tools immediately;
               semantic/pairing tools return "initializing" if asked before
               first sync completes

T+5min+1s  sb-sync-scheduler longrun starts (in parallel with sb-mcp):
             - Calls embedding_client.wait_ready() — already green
             - Queries sync_runs → empty table
             - Detects first-run condition
             - If SB_FIRST_RUN_ON_BOOTSTRAP=true (default):
                 Fires asyncio task: run_sync(full_refresh=True, reason="first_run")
             - Registers APScheduler cron (04:00 Europe/Stockholm)
             - Waits on main loop

T+5min..   First-run sync proceeds in parallel:
T+55min       Phase A ~1 min    (fetch catalog + stock + taxonomy)
              Phase B ~15 s     (persist)
              Phase C ~30 min   (27k detail fetches at 5 concurrent)
              Phase D ~15 min   (27k embeddings via sb-embed)
              Phase E ~3 s      (FTS rebuild)
              Phase F ~1 s      (finalize)

T+55min    sync_run_finished run_id=1 status=success
           MCP tools fully operational
           ntfy sends "first run complete" (no, actually it doesn't —
           only state transitions send alerts, and this is a 0→0 run;
           user sees success in logs)

T+next 04:00  Scheduler fires next run, ~3 min daily.
```

Warm reboots (container restart with `/data` populated) skip the model download and skip first-run detection, so they go from boot to ready in ~20 s with no background sync.

## Compose files

### Production (`docker-compose.yaml`)

```yaml
services:
  sb-stack:
    image: sb-stack:latest
    build:
      context: .
      dockerfile: Dockerfile
    container_name: sb-stack
    restart: unless-stopped
    runtime: nvidia
    environment:
      - SB_DATA_DIR=/data
      # Home stores (Karlstad + Skoghall)
      - SB_STORE_SUBSET=1701,1702,1716,1718
      - SB_MAIN_STORE=1701
      # Required
      - SB_MCP_TOKEN=${SB_MCP_TOKEN:?SB_MCP_TOKEN required; generate with: openssl rand -hex 32}
      # Optional (unset disables notifier)
      - SB_NTFY_URL=${SB_NTFY_URL:-}
      - SB_NTFY_TOKEN=${SB_NTFY_TOKEN:-}
      # Sync cadence
      - SB_SYNC_CRON=${SB_SYNC_CRON:-0 4 * * *}
      - SB_SYNC_TIMEZONE=Europe/Stockholm
      # GPU passthrough
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility
    ports:
      - "8000:8000"
    volumes:
      - ${SB_DATA_PATH:-./data}:/data
    healthcheck:
      test: ["CMD", "sb-stack", "doctor",
             "--only", "db_reachable,api_key_valid,disk_space,embed_service_reachable",
             "--exit-on-warn"]
      interval: 5m
      timeout: 30s
      start_period: 15m
      retries: 3
```

`.env` at repo root (gitignored):

```
SB_MCP_TOKEN=<output of: openssl rand -hex 32>
SB_DATA_PATH=/mnt/tank/sb-data
SB_NTFY_URL=https://ntfy.sh/my-private-sb-topic-fiK7X9
```

### Dev (`docker-compose.dev.yaml`)

See [11_observability_and_testing.md](./11_observability_and_testing.md) §"Local dev compose" for the dev variant with same Qwen3 model and single-store subset.

## Build & distribute

**Local-only workflow (recommended for personal use):**

```bash
# Build on dev machine
docker compose build

# Tag for pushing to TrueNAS
docker tag sb-stack:latest truenas.local:5000/sb-stack:latest

# Or save and transfer manually
docker save sb-stack:latest | gzip > sb-stack.tar.gz
scp sb-stack.tar.gz user@truenas.local:/tmp/
ssh user@truenas.local "docker load < /tmp/sb-stack.tar.gz"
```

**With a private registry (e.g. on TrueNAS itself):**

```bash
docker compose build
docker push truenas.local:5000/sb-stack:latest

# On TrueNAS: docker compose pull && docker compose up -d
```

No public registry (Docker Hub, GHCR) needed for a personal home deployment. Skip unless you want to share.

## TrueNAS Scale deployment walkthrough

1. **Enable NVIDIA on TrueNAS host.** System Settings → Apps → Configure → "Install GPU driver (if NVIDIA detected)". Reboot if prompted. Verify with `sudo nvidia-smi` from a shell.

2. **Create the data dataset.** Storage → Pools → `tank` → Add Dataset → name `sb-data`. Record the path (e.g. `/mnt/tank/sb-data`).

3. **Transfer the image to TrueNAS.** Whichever method from the "Build & distribute" section above.

4. **Generate a bearer token.**
   ```bash
   openssl rand -hex 32
   ```

5. **Deploy via TrueNAS "Custom App".** In the UI: Apps → Discover Apps → "Install Custom App" (exact UI label varies by TrueNAS Scale version). Paste the `docker-compose.yaml` shown above.

6. **Set environment variables** through the UI form or a `.env` file uploaded to the compose context:
   - `SB_MCP_TOKEN` = token from step 4
   - `SB_DATA_PATH` = `/mnt/tank/sb-data`
   - `SB_NTFY_URL` = optional

7. **Assign GPU passthrough** in the app's "Resources" section — select your 1080 Ti.

8. **Deploy** and watch `docker logs -f sb-stack`:
   - First-run model download: 5–10 min on a typical home connection
   - First-run full sync: ~50 min
   - MCP available immediately, but `semantic_search` / `pair_with_dish` / `find_similar_products` return "initializing" until sync completes

9. **Configure your MCP client.** Claude Desktop (or any MCP-aware client):
   ```json
   {
     "mcpServers": {
       "systembolaget": {
         "transport": {
           "type": "http",
           "url": "http://truenas.local:8000",
           "headers": {
             "Authorization": "Bearer <SB_MCP_TOKEN>"
           }
         }
       }
     }
   }
   ```

## Update procedure

```bash
# Rebuild image (pulls base image updates, reinstalls deps)
docker compose build --pull

# Apply
docker compose up -d

# Container restarts:
#  - Migrations auto-apply (forward-only)
#  - Existing DB + models retained (on /data volume)
#  - Warm boot ~20 s to ready
```

Zero-downtime isn't a goal for a personal setup. A brief (~30 s) MCP outage during the swap is fine.

**Rollback:** retag previous working image before rebuilding (`docker tag sb-stack:latest sb-stack:yesterday`), then `compose up -d` with the old tag. DB always has pre-migration backups at `/data/backup/pre-migration/` if a migration bites.

## Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| Container healthcheck never passes | sb-embed stuck downloading model | Check logs for download progress; may be slow connection |
| `runtime: nvidia` errors at startup | nvidia-container-toolkit not installed on TrueNAS host | Reinstall via TrueNAS GPU config |
| `sb-embed did not become ready within 900s` | Model download > 15 min | Pre-download: `docker run --rm -v /mnt/tank/sb-data/models:/data/models/hf huggingface/downloader Qwen/Qwen3-Embedding-4B`; or raise `SB_EMBED_READY_TIMEOUT` |
| MCP rejects with 401 | Wrong bearer token | Check `SB_MCP_TOKEN` matches client's Authorization header |
| Sync completes but `semantic_search` returns "stale" | `sync_runs` table empty or status != success | Check `sb-stack runs --limit 5`; look for phase that failed |
| "Extension not found" errors | DuckDB extensions missing from /data/duckdb_extensions | Delete the dir; next DB open re-downloads (vss + fts are ~5 MB each) |
| Port 8000 conflict | Another service on host | Change mapping: `"8001:8000"` in compose |
| Permission errors writing /data | Host dataset owned by different user than container root | On TrueNAS, set ACLs via Storage → dataset → Edit Permissions |

## Pre-built image checksums (optional)

For reproducibility across rebuilds, generate and record checksums:

```bash
docker compose build
docker image inspect sb-stack:latest --format '{{.Id}}' > image.sha256
```

Commit alongside `uv.lock` so rebuilds on the same inputs produce the same image.

## Open questions

1. **Image registry.** Local build + scp vs. private registry on TrueNAS vs. public GHCR? Lean local-build for personal use.
2. **Pre-cache model weights in the image?** Would add 8 GB to image but remove first-boot download delay. Lean: no — keeps image light, model lives with data.
3. **Non-root user inside the container?** Better practice but adds /data permissions complexity on TrueNAS. Lean: root inside container, let user handle UID mapping via compose `user:` directive if they want.
4. **Multi-arch builds** (arm64 for M-series Macs or Ampere TrueNAS)? Lean: x86_64 only. CUDA implies NVIDIA implies x86_64 for this project.
5. **Supply chain signing** (cosign / Notation)? For a personal project, lean: no.

---

## Design phase complete

That's Step 5, and the fifth of the five steps you laid out at the end of step "Module layout + schema-migration runner" ([06_module_layout.md](./06_module_layout.md)). With this doc, every major decision needed to write the code has a pinned answer, visible location, and rationale.

Total: 13 design docs, ~7,300 lines. No decisions left in head-only memory.

### What's now ready to implement

- [01 Project overview](./01_project_overview.md) — vision, tech stack
- [02 Systembolaget API](./02_systembolaget_api.md) — endpoints, key extraction
- [03 Data schema](./03_data_schema.md) — full DuckDB DDL
- [04 MCP surface](./04_mcp_surface.md) — 10 tools, Swedish descriptions
- [05 Sync pipeline](./05_sync_pipeline.md) — 6 phases, change detection
- [06 Module layout](./06_module_layout.md) — Python structure, migrations, doctor
- [07 Deployment](./07_deployment.md) — Docker + TrueNAS
- [08 MCP implementation](./08_mcp_implementation.md) — per-tool SQL, response shapes
- [09 Embedding service](./09_embedding_service.md) — sb-embed, client, Ollama-swappable
- [10 Sync orchestration](./10_sync_orchestration.md) — phase wiring, retries, ntfy
- [11 Observability + testing](./11_observability_and_testing.md) — logs, metrics, tests, dev workflow
- [12 Packaging](./12_packaging.md) — this doc
- [DISH_PAIRING_DESIGN.md](../DISH_PAIRING_DESIGN.md) — pairing engine (standalone)

When you're ready to move from design to implementation, we can start from any of the modules in the dependency graph — I'd suggest `settings.py` + `logging.py` + `errors.py` first (leaves, foundation for everything else), then `db/` (including migrations and `001_initial.sql`), then verticals on top.
