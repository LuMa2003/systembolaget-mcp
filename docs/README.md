# sb-stack — design documents

All design decisions captured here before any code is written. Each doc is self-contained; read in numerical order for the full picture, or jump to a specific concern.

Top-level docs outside this folder:
- [`../DISH_PAIRING_DESIGN.md`](../DISH_PAIRING_DESIGN.md) — the pairing engine in detail (deliberately standalone, since it may become its own app)

In this folder:

| # | Document | Covers |
|---|---|---|
| [01](./01_project_overview.md) | Project overview | Vision, tech stack, architecture at a glance, non-goals, Python vs Go rationale |
| [02](./02_systembolaget_api.md) | Systembolaget API reverse-engineered | Endpoint map, auth, key extraction, image CDN, rate limits |
| [03](./03_data_schema.md) | Data schema | Full DuckDB DDL, design rationale, index strategy |
| [04](./04_mcp_surface.md) | MCP tool surface | 10 tools (Swedish descriptions), auth, transport, SDK choice |
| [05](./05_sync_pipeline.md) | Sync pipeline | 6 phases, change detection, failure handling, scheduling |
| [06](./06_module_layout.md) | Module layout & foundations | Python package structure, settings, logging, errors, DB connection, CLI, migrations, doctor |
| [07](./07_deployment.md) | Deployment | Docker container, s6, TrueNAS, bootstrap flow, env vars, volume layout |
| [08](./08_mcp_implementation.md) | MCP tool implementation | SQL per tool, Pydantic responses, sugar-param resolution, error mapping, freshness meta |
| [09](./09_embedding_service.md) | Embedding service (sb-embed) | OpenAI-compatible inference server, client, Ollama swappability |
| [10](./10_sync_orchestration.md) | Sync orchestration | Phase wiring, retry semantics, lockfile, scheduler, replay, dry-run, metrics |
| [11](./11_observability_and_testing.md) | Observability + testing + dev workflow | Log event taxonomy, metrics file, test pyramid, VCR, golden pairings, dev compose |
| [12](./12_packaging.md) | Packaging & deployment | Dockerfile, s6 services, entrypoint, first-run flow, TrueNAS walkthrough |

## Conventions in these docs

- "sb-stack" is the full application name (the single binary).
- "sb-sync" and "sb-mcp" refer to the two logical processes inside the container (not separate binaries — same `sb-stack` with different subcommands).
- SQL uses DuckDB's dialect.
- Swedish text appears verbatim where it's user-facing (tool descriptions, pairing reasoning).
- Code snippets are for illustration; actual implementation lives in `src/sb_stack/`.

## Status

All documents below reflect the design as of **2026-04-19**. Before any coding begins, every decision in these docs should be ratified.
