# 01 — Project overview

## Vision

Give a non-expert home user expert-grade drink recommendations from Systembolaget's catalog in natural Swedish, grounded in live store stock at their home stores.

Core interactions (all via an MCP server):

- "Hitta ett kraftigt rödvin till oxfilé med rödvinssås" → ranked pairings with sommelier reasoning
- "Finns Apothic Red på Duvan?" → per-store stock + shelf location
- "Visa veganska viner från Italien under 150 kr som finns i lager" → structured search
- "Vad kommer i sortimentet nästa vecka?" → upcoming launches
- "Lätt söt fruktig whisky med låg rökighet" → vector search

## Architecture at a glance

```
┌─────────── Docker container (sb-stack) ────────────────┐
│                                                         │
│   sb-mcp        sb-sync-scheduler       sb-embed        │
│   HTTP :8000    APScheduler             HTTP :9000      │
│                 04:00 CET               (localhost      │
│                                          only)         │
│       │              │                    │             │
│       │ reads        │ writes (WAL)       │ owns GPU    │
│       │              │                    │ Qwen3-4B    │
│       ▼              ▼                    │             │
│   ┌──────────────────────────┐            │             │
│   │   DuckDB  (sb.duckdb)    │            │             │
│   │   + vss (HNSW) + fts     │            │             │
│   └──────────────────────────┘            │             │
│       ▲              ▲                    │             │
│       │              │                    │             │
│       └──────────────┴───── HTTP POST ────┘             │
│                             /v1/embeddings              │
│                             (OpenAI-compatible)         │
│                                                         │
└─────────────────────────────────────────────────────────┘
           │                           │
           │                           │
    Claude Desktop /          Systembolaget APIs
    IDE / custom client       (nightly pull)
```

One container, **three** processes supervised by s6-overlay, one persistent volume, one GPU passthrough.

The embedding service is deliberately isolated behind an OpenAI-compatible HTTP protocol so it can later be swapped for Ollama, LM Studio, vLLM, or a hosted embedding API. See [09_embedding_service.md](./09_embedding_service.md).

## Tech stack (locked)

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | Best DuckDB binding, best embedding libs, fast iteration |
| Package manager | `uv` | 10×-100× faster than pip, proper lockfiles |
| Build backend | `hatchling` | Minimal, modern, works with uv |
| Database | DuckDB (single file) | Columnar, embedded, FTS + HNSW extensions, small |
| Embedding model | Qwen3-Embedding-4B (2560-dim) | 2025 state-of-the-art multilingual, fits 1080 Ti in fp16 |
| Embedding service | FastAPI + sentence-transformers, OpenAI-compatible | Isolated process; swappable for Ollama/LM Studio/vLLM later |
| HTTP client | httpx (async) | Proper async/retry support |
| MCP framework | `fastmcp>=3.2` (standalone) | 70% market share; bearer auth + streamable HTTP first-class |
| Scheduler | APScheduler | In-process cron |
| Process supervisor | s6-overlay | Lightweight, handles signals properly |
| Logging | structlog | Structured JSON + tee to files |
| Config | pydantic-settings | Env-var driven, typed |
| Container base | `pytorch/pytorch:2.5-cuda12.4-cudnn9-runtime` | Pre-built CUDA stack |
| Deployment | Docker on TrueNAS Scale | User's home server with 1080 Ti |

## Why Python over Go (locked)

Python won on the decisive factor: **embedding inference**. Qwen3-Embedding-4B in Python is a 5-line `sentence-transformers` call; in Go it would require ONNX conversion or a separate Python sidecar, defeating Go's "single binary" advantage.

Secondary Python wins:
- DuckDB's Python binding is the reference implementation (Go binding lags features).
- 170-field JSON schemas handle gracefully with Pydantic's `extra='allow'`; Go needs explicit struct definitions for every field, painful for schema evolution.
- The pairing engine has tunable weights and a cultural-pairings dictionary — REPL-driven iteration in Python beats compile cycles.
- A possible future analytics app would share the DuckDB file naturally via pandas/polars.

Go wins on container image size (~30 MB vs ~5 GB) and memory footprint, but neither matters for a dedicated home-server container.

## Non-goals

- **Account/cart/order/reservation features** — those need BankID; out of scope.
- **Public deployment / commercialization** — personal use only. Re-scraping-as-a-service has been contested by Systembolaget before; see `02_systembolaget_api.md` §ToS.
- **Multi-user / multi-tenancy** — single home user, single set of home stores.
- **Live stock (sub-daily freshness)** — Systembolaget updates stock nightly; finer granularity adds complexity with no upside.
- **Non-Swedish markets** — assumes Systembolaget catalog; extensible to Alko/Vinmonopolet later if ever wanted.

## Deployment target

- **Host**: TrueNAS Scale server with an NVIDIA 1080 Ti (11 GB VRAM).
- **Container**: single image, single persistent volume mounted at `/data`.
- **GPU passthrough** via `nvidia-container-toolkit`.
- **MCP port**: 8000 (HTTP, bearer-token-authenticated).
- **Sync schedule**: 04:00 Europe/Stockholm (after Systembolaget's own overnight refresh).

## High-level process topology

| Process | Role | Lifetime |
|---|---|---|
| `sb-mcp` | MCP HTTP server, reads DuckDB | long-running |
| `sb-sync-scheduler` | triggers `sb-stack sync` on cron | long-running |
| `sb-embed` | OpenAI-compatible embedding server, holds Qwen3 model in VRAM | long-running |
| `sb-stack sync` | one run of the nightly pipeline | invoked by scheduler, ~5 min daily / ~50 min first run |

Schema migrations and home-store seeding happen at container startup, before either long-running process accepts work.

## Where decisions are traced

- Endpoint / key / CDN discoveries — [02_systembolaget_api.md](./02_systembolaget_api.md)
- Tables, columns, indexes — [03_data_schema.md](./03_data_schema.md)
- MCP tools — [04_mcp_surface.md](./04_mcp_surface.md)
- Sync pipeline phases — [05_sync_pipeline.md](./05_sync_pipeline.md)
- Python packages, files — [06_module_layout.md](./06_module_layout.md)
- Container and bootstrap — [07_deployment.md](./07_deployment.md)
- Pairing engine — [../DISH_PAIRING_DESIGN.md](../DISH_PAIRING_DESIGN.md)
