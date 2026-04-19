"""`sb-embed` — standalone FastAPI service that owns the embedding model.

Serves an OpenAI-compatible `/v1/embeddings` endpoint so downstream code
(sb-sync, sb-mcp, and later Ollama/vLLM swaps) share one interface.

This subpackage must NOT import from `sb_stack.db`, `sb_stack.sync`, or
`sb_stack.mcp_server` — it's intentionally isolated so the user can run
it detached or swap in an external provider. See
docs/06_module_layout.md §Module dependency graph.
"""

from sb_stack.embed_server.server import create_app

__all__ = ["create_app"]
