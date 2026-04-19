# 09 — Embedding service (`sb-embed`)

A dedicated HTTP service that owns the Qwen3-Embedding-4B model and serves an **OpenAI-compatible `/v1/embeddings`** endpoint. Both sb-sync (Phase D) and sb-mcp (`semantic_search`, `find_similar_products`, `pair_with_dish`) call this service instead of loading the model themselves.

## Why a separate service

- **One GPU, one model load.** The 1080 Ti has 11 GB VRAM. Qwen3-Embedding-4B in fp16 uses ~8 GB. Loading it in two processes (sync + MCP) would exceed VRAM. A single resident service solves this cleanly.
- **Swappability.** The OpenAI-compatible protocol is the de facto standard. If the user later runs Ollama, LM Studio, llama.cpp server, vLLM, or a hosted API for other LLM needs, they point `SB_EMBED_URL` at it and disable our service. No code changes.
- **Isolation.** Model crashes don't take down the MCP server. GPU memory fragmentation is bounded to one process.
- **Future-proof.** If the user ever wants to share inference infra across multiple home-server apps (RAG server, coding assistant, whatever), sb-embed can run detached outside the sb-stack container.

## Protocol

**OpenAI-compatible embeddings endpoint.** Exact same request/response shape as `https://api.openai.com/v1/embeddings`, so any OpenAI-compatible server drops in.

### Request

```
POST <SB_EMBED_URL>
Content-Type: application/json

{
  "model": "Qwen/Qwen3-Embedding-4B",
  "input": ["text one", "text two", ...]
}
```

- `input` accepts a single string OR a list of strings.
- `model` is informational; a given server only serves the model it was started with. Sent mostly for future router-style deployments.

### Response

```json
{
  "object": "list",
  "data": [
    {"object": "embedding", "embedding": [0.01, -0.23, ...], "index": 0},
    {"object": "embedding", "embedding": [0.04, -0.11, ...], "index": 1}
  ],
  "model": "Qwen/Qwen3-Embedding-4B",
  "usage": {"prompt_tokens": 42, "total_tokens": 42}
}
```

`index` preserves input order. Clients must sort by `index` before use (not assume array order).

### Auxiliary endpoints

- `GET /health` → `{"status": "ok", "model": "..."}` once the model is fully loaded; `{"status": "loading"}` during startup.
- `GET /v1/models` → OpenAI-compatible model list (single entry).

### Errors

Standard HTTP codes with JSON body:
- `503 Service Unavailable` during model load
- `400 Bad Request` for malformed input (empty, too large)
- `413 Payload Too Large` if the input array exceeds `SB_EMBED_MAX_BATCH`
- `500 Internal Server Error` for inference failures; message sanitized, full trace in server log

Compatible with OpenAI error shape:
```json
{"error": {"type": "...", "message": "...", "code": "..."}}
```

## Server implementation (`sb-embed`)

```
src/sb_stack/embed_server/
├── __init__.py
├── server.py         FastAPI app
├── models.py         Pydantic request/response (OpenAI-compatible)
├── loader.py         SentenceTransformer loader with GPU preflight
└── cli.py            `sb-stack embed-server` subcommand
```

### FastAPI app (sketch)

```python
# server.py
from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager

_model = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    log.info("embedding_model_loading", name=settings.embed_model,
             device=settings.embed_device)
    _model = SentenceTransformer(
        settings.embed_model,
        device=settings.embed_device,
        cache_folder=str(settings.models_cache_dir),
    )
    _ = _model.encode(["warmup"], convert_to_numpy=True)  # trigger CUDA kernels
    log.info("embedding_model_loaded",
             dim=_model.get_sentence_embedding_dimension())
    yield
    # On shutdown — no explicit unload needed; process exit releases VRAM

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    if _model is None:
        raise HTTPException(503, detail="model loading")
    return {"status": "ok", "model": settings.embed_model}

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": settings.embed_model, "object": "model",
            "created": 0, "owned_by": "sb-stack",
        }],
    }

@app.post("/v1/embeddings")
async def embeddings(req: EmbedRequest):
    if _model is None:
        raise HTTPException(503, detail="model loading")

    inputs = req.input if isinstance(req.input, list) else [req.input]
    if len(inputs) > settings.embed_max_batch:
        raise HTTPException(413, detail=f"input too large, max={settings.embed_max_batch}")
    if not inputs:
        raise HTTPException(400, detail="input is empty")

    vectors = await asyncio.to_thread(
        _model.encode,
        inputs,
        batch_size=settings.embed_gpu_batch_size,
        convert_to_numpy=True,
        normalize_embeddings=False,
    )
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": v.tolist(), "index": i}
            for i, v in enumerate(vectors)
        ],
        "model": settings.embed_model,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }
```

### Networking

- **Binds to `0.0.0.0:9000` inside the container**, not exposed to host by default. Other container processes reach it via `http://localhost:9000`.
- **No authentication** — it's only reachable from inside the container.
- If the user later wants to share the service across containers/hosts, they:
  - Map port 9000 in `docker-compose.yaml` (commented-out by default).
  - Add a reverse proxy with bearer-token auth if exposed off-host.
  - Or deploy the service externally entirely and update `SB_EMBED_URL`.

### Why FastAPI and not `mcp`/bare uvicorn

- Small surface, clear protocol semantics, auto-validates via Pydantic.
- Handles the OpenAI-compatible JSON shape in ~100 lines.
- Separate ASGI app from MCP, so crashes are isolated.

### Resource profile

| State | VRAM | RAM | CPU |
|---|---|---|---|
| Model loading (first run, downloading weights) | up to 12 GB transient | ~4 GB | 1 core |
| Warmup complete, idle | ~8 GB | ~2 GB | ~0% |
| Serving one request of 32 texts | ~8.5 GB (activations) | ~2.5 GB | 1 core + GPU busy ~200 ms |

Startup cost to "ready": ~60–90 s cold (download), ~15 s warm.

## Client (`embedding_client`)

Used by sb-sync and sb-mcp; single async HTTP client.

```
src/sb_stack/embed/
├── __init__.py
├── client.py       EmbeddingClient: async, retry, batching
├── templates.py    category-specific embedding text templates
└── hashing.py      source_hash computation
```

### Client interface

```python
# embed/client.py

class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.base_url = settings.embed_url                 # e.g. "http://localhost:9000"
        self.model = settings.embed_model
        self.max_batch = settings.embed_client_batch_size  # for splitting large jobs
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0),
        )

    async def ready(self) -> bool:
        """Wait-for-ready probe, used by callers before big jobs."""
        try:
            r = await self.http.get(f"{self.base_url}/health")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Returns one vector per input, in input order."""
        if not texts:
            return []

        all_vectors: list[list[float]] = []
        for batch_start in range(0, len(texts), self.max_batch):
            batch = texts[batch_start : batch_start + self.max_batch]
            vectors = await self._embed_batch_with_retry(batch)
            all_vectors.extend(vectors)
        return all_vectors

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadError, ServerError)),
    )
    async def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        r = await self.http.post(
            f"{self.base_url}/v1/embeddings",
            json={"model": self.model, "input": batch},
        )
        if r.status_code == 503:
            raise ServerError("embedding service still loading")
        r.raise_for_status()
        data = r.json()["data"]
        data.sort(key=lambda d: d["index"])
        return [d["embedding"] for d in data]
```

### Template + hashing

Category-specific templates from [`DISH_PAIRING_DESIGN.md`](../DISH_PAIRING_DESIGN.md) §"Category-specific embedding templates":

```python
# embed/templates.py

@dataclass
class TemplateVersion:
    version: str  # e.g. "wine_v1"

TEMPLATE_WINE = """{name_bold} {name_thin}
{producer_name}
{country} {origin_level_1}
{category_level_2} / {category_level_3}
{grapes}
Årgång: {vintage}
{color}
{taste}
{aroma}
{usage}
Passar till: {taste_symbols}"""

# (similar for beer / spirit / cider / alkoholfritt)

TEMPLATES = {
    "Vin":                        ("wine_v1",         TEMPLATE_WINE),
    "Öl":                         ("beer_v1",         TEMPLATE_BEER),
    "Sprit":                      ("spirit_v1",       TEMPLATE_SPIRIT),
    "Cider & blanddrycker":       ("cider_v1",        TEMPLATE_CIDER),
    "Alkoholfritt":               ("alcoholfree_v1",  TEMPLATE_ALCOHOLFREE),
}

def render(product: dict) -> tuple[str, str] | None:
    """Returns (text, template_version) or None for skipped categories."""
    entry = TEMPLATES.get(product["category_level_1"])
    if not entry:
        return None  # Presentartiklar skipped
    version, template = entry
    text = template.format_map(defaultdict(str, **product))
    return text, version

def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

`source_hash` is what ends up in `product_embeddings.source_hash` — only re-embed when it changes.

## Startup ordering

s6 dependency graph:

```
base
 ├── sb-embed                  (must come up first; others depend on it)
 │
 ├── sb-mcp                    depends on sb-embed
 │
 └── sb-sync-scheduler         depends on sb-embed
```

Mechanism: each dependent service has a `dependencies.d/sb-embed` entry. s6 brings up `sb-embed` first, runs its service in background, then starts the dependents only after sb-embed's `run` script is executing (which doesn't mean the model is loaded — see next).

**"Executing" vs "model loaded"** are different states:
- s6 starts dependents as soon as `sb-embed` is in the RUNNING state (its run script is executing).
- But model load takes 60–90 s. During this window, `/health` returns 503.
- sb-mcp and sb-sync MUST wait for `/health` to return 200 before doing real work.

Pattern used by both:

```python
async def wait_for_embed_ready(client: EmbeddingClient, timeout_s: int = 300):
    start = time.time()
    while time.time() - start < timeout_s:
        if await client.ready():
            log.info("embedding_service_ready")
            return
        log.info("embedding_service_not_ready", waited_s=int(time.time() - start))
        await asyncio.sleep(5)
    raise TimeoutError(
        f"embedding service not ready after {timeout_s}s; "
        f"check sb-embed logs"
    )
```

- **sb-sync** calls `wait_for_embed_ready()` before Phase D. Other phases don't need embed.
- **sb-mcp** calls it at server startup and refuses to serve `semantic_search` / `find_similar_products` / `pair_with_dish` requests until ready. Other tools work immediately. Returns a clear error: *"Semantisk sökning är inte tillgänglig just nu (embedding-tjänsten startar fortfarande). Försök igen om ~1 minut."*

## Swapping to Ollama (future)

User-visible steps only:

1. Install Ollama on the host (or in a sidecar container) and pull an embedding model (`ollama pull mxbai-embed-large` or similar).
2. Disable `sb-embed` in the container — either scale it to zero in the compose file, or remove its s6 service dir.
3. Update `SB_EMBED_URL=http://host.docker.internal:11434/v1/embeddings` (Ollama's OpenAI-compatible endpoint).
4. Update `SB_EMBED_MODEL=mxbai-embed-large` (whatever Ollama model you pulled).
5. Update `SB_EMBED_DIM` if the new model has a different dimension (affects schema! see next).
6. Re-run `sb-stack sync --full-refresh` to re-embed all products with the new model.

**Note on dim change:** `product_embeddings.embedding` is declared `FLOAT[2560]` for Qwen3. Changing models to a different dim requires a migration (new column or new table). Keep this in mind; not zero-cost to swap.

Possible future refinement: store model+dim metadata in a `model_registry` table and support multiple co-resident embedding models (e.g., one for semantic search, a smaller one for clustering). Out of scope for v1.

## Configuration

Env vars added:

```
SB_EMBED_URL=http://localhost:9000/v1/embeddings
SB_EMBED_MODEL=Qwen/Qwen3-Embedding-4B
SB_EMBED_DIM=2560
SB_EMBED_DEVICE=cuda:0
SB_EMBED_PORT=9000                    (server-only)
SB_EMBED_MAX_BATCH=2048               (server-side hard limit)
SB_EMBED_GPU_BATCH_SIZE=32            (server internal batching on GPU)
SB_EMBED_CLIENT_BATCH_SIZE=128        (client-side chunking before calling server)
(HF cache path is derived from SB_DATA_DIR — not a separate env var)
```

`SB_EMBED_URL` is the pivot: point it anywhere OpenAI-compatible.

## Health & observability

- Docker healthcheck includes `curl -f http://localhost:9000/health` as part of the doctor check battery.
- `sb-embed` logs one line per request (latency, batch size, total tokens).
- First successful model load is a visible log event: `embedding_model_loaded name=... dim=... load_time_s=...`.
- The `doctor` subcommand gains a check `embed_service_reachable` (active) and the existing `gpu_available` check (passive — GPU may be owned by sb-embed process).

## Testing

- **Unit**: mock the `EmbeddingClient` using `httpx.MockTransport`. Return deterministic fake vectors (e.g., `[0.1 * i for i in range(2560)]` per text) so downstream logic is exercisable without a GPU.
- **Integration**: spin up a real `sb-embed` process in Docker during CI with a tiny model (e.g., `sentence-transformers/all-MiniLM-L6-v2`, 384-dim, ~90 MB) and hit it end-to-end. Avoids shipping the 8 GB Qwen3 weights in CI.
- **Contract test**: recorded response-shape test pinning the OpenAI-compatible JSON structure, so swapping to Ollama later won't silently break parsing.

## Failure modes

| Situation | Behavior |
|---|---|
| `sb-embed` crashes mid-sync | Phase D fails; sync marks `partial`; retry next night. Other phases unaffected. |
| Client gets 503 (model still loading) | Retry up to 5× with exponential backoff |
| Model weights corrupted in `/data/models/` | Delete the cache dir; sb-embed re-downloads on next start |
| `SB_EMBED_URL` misconfigured | sb-sync hard-fails Phase D; sb-mcp returns clear errors to the three semantic tools |
| Ollama-side dimension change | sb-sync logs dimension mismatch; refuses to write rows into `FLOAT[2560]` column |
