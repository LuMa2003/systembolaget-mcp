# sb-stack

A local, self-hosted MCP server on top of Systembolaget's product catalog — semantic search, structured filtering, and sommelier-grade food pairings, grounded in your home stores' live stock.

Runs in a single Docker container on a TrueNAS Scale home server with GPU passthrough. Nightly sync against Systembolaget's reverse-engineered APIs; no commercial usage.

## Status

**Design phase complete, implementation in progress.** See [`docs/README.md`](./docs/README.md) for the full design set (13 documents, every decision pinned before the first line of code).

Highlights:
- DuckDB single-file store with `vss` (HNSW) + `fts5`-style full-text search.
- Qwen3-Embedding-4B served locally via an OpenAI-compatible embedding service (swappable with Ollama or any `/v1/embeddings` endpoint).
- FastMCP v3 HTTP server exposing 10 read-only tools; bearer-token auth.
- Six-phase nightly sync pipeline with change-based history, 1-year raw-JSON archive, replayable from disk.
- State-transition ntfy alerts for severe failures (quiet by design).
- Swedish-language MCP tool descriptions; pairing engine anchored in Systembolaget's sommelier text for every curated product.

## Docs index

| | Topic |
|---|---|
| [01](./docs/01_project_overview.md) | Project overview, tech stack, architecture at a glance |
| [02](./docs/02_systembolaget_api.md) | Reverse-engineered API endpoints, auth key extraction |
| [03](./docs/03_data_schema.md) | Full DuckDB DDL + rationale |
| [04](./docs/04_mcp_surface.md) | The 10 MCP tools (Swedish descriptions) |
| [05](./docs/05_sync_pipeline.md) | Sync phases and data flow |
| [06](./docs/06_module_layout.md) | Python package structure, migrations, settings, doctor |
| [07](./docs/07_deployment.md) | Docker + s6 + TrueNAS deployment |
| [08](./docs/08_mcp_implementation.md) | SQL per tool, Pydantic responses, error mapping |
| [09](./docs/09_embedding_service.md) | sb-embed + OpenAI-compat client |
| [10](./docs/10_sync_orchestration.md) | Orchestrator, retries, lockfile, scheduler, ntfy |
| [11](./docs/11_observability_and_testing.md) | Logs, metrics, test pyramid, VCR, dev workflow |
| [12](./docs/12_packaging.md) | Dockerfile, s6 services, first-run flow |
| [pairing](./DISH_PAIRING_DESIGN.md) | Dish pairing engine (standalone-extractable) |

## Scope

Personal home use. No re-hosting of Systembolaget's data. No public deployment. See [02 §ToS note](./docs/02_systembolaget_api.md).

## License

MIT — see [LICENSE](./LICENSE).
