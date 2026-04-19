"""`sb-stack embed-server` — run the FastAPI embedding service via uvicorn."""

from __future__ import annotations


def run() -> None:
    """Block-starting uvicorn entrypoint. Called by cli/main.py."""
    import uvicorn  # noqa: PLC0415

    from sb_stack.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    uvicorn.run(
        "sb_stack.embed_server.server:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 — by design; container-local trust
        port=settings.embed_port,
        log_level=settings.log_level,
        access_log=False,
    )
