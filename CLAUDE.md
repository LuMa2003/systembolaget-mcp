# CLAUDE.md — continuation context for sb-stack

Project: **sb-stack** — a local MCP server on top of Systembolaget's product catalog + a sommelier-grade dish-pairing engine. Runs in a single Docker container on a TrueNAS Scale box with a 1080 Ti. Dev happens in WSL with a 4090.

**Design phase is complete. Implementation has not started.**

## First, read these (in order)

1. `README.md` — project identity, status, index of docs
2. `docs/README.md` — index of 13 design docs (~7,230 lines)
3. The doc relevant to your current implementation step (see §"Implementation order")

Don't skip the design docs. Every non-obvious decision is pinned there with rationale, so you're almost always better served by reading the doc than re-deriving.

## Where things live

```
/home/luma/systembolaget/         (WSL ext4; repo root; git branch: main)
├── README.md                     project intro + docs index
├── CLAUDE.md                     this file
├── LICENSE                       MIT
├── .gitignore
├── docs/                         13 design docs (01–12 + README)
└── DISH_PAIRING_DESIGN.md        standalone pairing engine spec
```

Remote: `https://github.com/LuMa2003/systembolaget-mcp` (public, authenticated via `gh`).

## Working rules

### Autonomy
Work without checking in for every step. Run tests, iterate, commit. Only stop to involve the user when:
- A design decision would conflict with what's in `docs/` (flag it, discuss)
- Something requires sudo or interactive auth they haven't prepared
- You would be committing a secret
- A genuinely destructive operation is warranted (force-push, drop a table, rm -rf)

### Notifications — ntfy channel `https://ntfy.sh/luma-claude-code`
**Only notify when user action is actually required.** Don't send milestone updates, "done with step 2" pings, or heartbeats. The user's explicit rule.

Send with:
```bash
curl -H "Title: <short title>" -H "Priority: <1-5>" -H "Tags: <tag>" \
     -d "<message>" https://ntfy.sh/luma-claude-code
```

Use priority 5 for blockers, 3 for "please decide X when convenient", 2 for benign info (rarely — usually skip).

### Commit style
Small, focused, logical commits as you go. Never a huge commit at the end.
- Conventional prefixes: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`, `perf:`, `ci:`.
- Prefer adding specific files (`git add path`) over `git add -A`.
- Commit messages in English.

### Communication style
- Direct, no padding.
- Honest about tradeoffs; push back when you disagree.
- Swedish for user-facing text that surfaces to the end user (MCP tool descriptions, pairing explanations, error messages). English for code, logs, comments.
- Code: default to no comments; comment only when the *why* is non-obvious.

## Environment state (pre-installed, on this WSL host)

| Tool | Version | Location |
|---|---|---|
| git | 2.34 | system |
| gh CLI | 2.90.0 | ~/.local/bin/gh (authenticated as `LuMa2003`) |
| uv | 0.11.7 | ~/.local/bin/uv (creates Python 3.12 venvs on demand) |
| Python | 3.10 (system) | system; 3.12 will be managed by uv |
| Docker | 28.0.4 | system (with `--gpus all` working) |
| NVIDIA driver | 591.86 | Windows host (RTX 4090, 24 GB VRAM) |

Git identity configured: `Lucas Mårtensson <39569742+LuMa2003@users.noreply.github.com>`, default branch `main`.

No sudo required for anything in the implementation. Everything lives under `~/.local/bin` or `~/systembolaget/`.

## Implementation order

Follow `docs/06_module_layout.md` §"Module dependency graph" for the strict order. High-level:

1. **Foundations**
   - `pyproject.toml`, `.env.example`, `.python-version`
   - `src/sb_stack/__init__.py`, `__main__.py`
   - `settings.py` (pydantic-settings), `errors.py`, `logging.py`
   - `cli/main.py` skeleton (typer app with placeholder subcommands)
   - Test: `uv run sb-stack --help` returns the subcommand list
2. **DB foundations**
   - `db/connection.py`, `db/migrations.py`
   - `db/schema/001_initial.sql` — compiled from the DDL in `docs/03_data_schema.md` (every table + index + extension + sequence + FTS)
   - `sb-stack migrate` subcommand
   - Unit tests for migration runner (integrity, ordering, rollback)
3. **API client**
   - `api_client/client.py` (httpx async with tenacity retry)
   - `api_client/config_extractor.py` (NEXT_PUBLIC_* scraper + validator)
   - `api_client/paths.py` (URL builders)
   - Integration tests via `respx` mocks (no live calls yet)
4. **Embedding service + client**
   - `embed_server/` (FastAPI app, OpenAI-compatible)
   - `embed/client.py`, `embed/templates.py`, `embed/hashing.py`
   - `sb-stack embed-server` subcommand
   - Integration test with a tiny model (`all-MiniLM-L6-v2`) in CI profile
5. **Sync**
   - `raw_archive/writer.py`, `reader.py`, `retention.py`
   - `sync/phases/{fetch_*, diff, persist, fetch_details, embed, index, finalize}.py`
   - `sync/orchestrator.py` + `scheduler.py` + `cli.py`
   - `sb-stack sync`, `sync-scheduler`, `runs`, `run-info` subcommands
6. **MCP server**
   - `mcp_server/server.py`, `mcp_server/auth.py`, `mcp_server/responses.py`
   - Each of the 10 tools as its own module under `mcp_server/tools/`
   - One commit per tool is fine; all-tools-in-one-commit is also fine if they're small
7. **Pairing engine**
   - `pairing/{scorer,cultural,diversity,confidence,engine}.py`
   - `pairing/data/cultural_pairings.yaml`
   - Golden scenario YAML + pytest runner
   - Can parallel with MCP step
8. **Doctor** (`doctor/runner.py`, `doctor/checks.py`) + any CLI polish
9. **Notifications** (`notifications/ntfy.py` — AlertManager)
10. **Packaging**: `Dockerfile`, `.dockerignore`, `deploy/s6-rc.d/*`, `deploy/scripts/*`, `docker-compose.yaml`, `docker-compose.dev.yaml`
11. **Tests interleaved** throughout. Don't defer to the end.

### Coding conventions to honor

- src-layout (`src/sb_stack/`), not flat.
- Python 3.12. Type-hint everything in `src/`; `mypy --strict` on `src/`.
- `ruff check` + `ruff format` configured in `pyproject.toml`; set up pre-commit in step 1.
- Every log line uses structlog's `event="snake_case_name"` + typed kwargs (taxonomy in `docs/11_observability_and_testing.md`).
- Async where useful; wrap blocking DuckDB/embedding calls in `asyncio.to_thread()`.
- Pydantic v2 for MCP tool input/output.
- Tests use `duckdb.connect(":memory:")` for unit, sample DB for integration, VCR cassettes for real HTTP shape.

### Critical cross-references (shortcut paths)

- All 10 MCP tool SQL queries → `docs/08_mcp_implementation.md`
- Sync phase behavior + retry policy → `docs/10_sync_orchestration.md`
- Embedding protocol + Ollama swap path → `docs/09_embedding_service.md`
- Pairing engine scoring formula → `DISH_PAIRING_DESIGN.md` §7
- Settings env vars (authoritative list) → `docs/06_module_layout.md` §"Settings"
- First-run timing expectations → `docs/12_packaging.md` §"First-run flow"
- Log event catalog → `docs/11_observability_and_testing.md` §"Full catalog"

## User preferences (persistent)

- **Home stores**: siteId 1701 `Duvan` (main, walkable), 1702 `Bergvik-Karlstad`, 1718 `Välsviken`, 1716 `Skoghall`.
- **Repo visibility**: public by explicit choice. The API subscription keys in docs are extractable from Systembolaget's public frontend (viewable with F12); including them in a public repo adds no real exposure over prior public projects.
- **Primary use case**: natural-language Swedish pairing recommendations ("hitta ett vin som passar till fläskfile och är kraftigt"). Design reflects this as the differentiator.
- **Analytics out of scope for MCP** — history tables populated by sync, consumed only by future external analytics app (if ever).
- **Production runs on TrueNAS Scale** (separate network from dev), single-container deploy, GPU passthrough.

## First-run expectations (when you eventually deploy)

- Container cold boot: ~2 s
- Qwen3 weights download (one-time): 5–10 min
- First full sync: ~50 min (27k products × detail fetch + embed)
- Daily sync thereafter: ~3 min
- Docker image size: ~5.5 GB uncompressed

## Non-goals (don't add these without asking)

- BankID auth, cart, order, reservation — out of Systembolaget public API surface
- Multi-user accounts, tenancy — single home user
- Public SaaS / commercialization — personal only, no re-hosting
- Sub-daily stock updates — Systembolaget updates nightly; finer granularity wastes calls
- Non-Swedish markets — catalog is Systembolaget-specific
- Image baking (Qwen3 in Docker image) — keep image light, model lives on the volume

## What to do if a design doc turns out to be wrong

Fix it. Small commit titled `docs: correct <specific thing>` with a one-sentence explanation. Design docs are not frozen; they're our source of truth.

If the fix materially changes the design (not just a typo/consistency tweak), stop and check with the user via ntfy before committing. The user gave explicit leans on dozens of decisions; don't silently reverse them.

## Git history context

```
e0e4354  docs: reconcile bootstrap semantics and embed cache dir
5f6a303  docs: observability, testing, and dev workflow
0693b16  docs: module layout, deployment, and packaging
e09da56  docs: embedding service and dish pairing engine
2be97ce  docs: sync pipeline and orchestration
d31ce40  docs: data schema, MCP surface, and MCP tool implementation
7d53ab7  docs: Systembolaget API research and project overview
c5ce976  chore: project scaffolding
```

All pushed to `origin/main`.
