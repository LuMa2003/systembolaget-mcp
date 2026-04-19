"""Unified `sb-stack` CLI.

Each subcommand is a thin shell that will be filled in by later
implementation steps. At this stage (Step 1 — Foundations) they exist
so `sb-stack --help` renders the full surface and downstream modules
can target concrete entry points.

See docs/06_module_layout.md §CLI entrypoint for the authoritative list.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sb_stack import __version__

app = typer.Typer(
    name="sb-stack",
    help="Systembolaget assortment + pairing MCP server.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_NOT_IMPLEMENTED_EXIT = 2


def _not_implemented(name: str) -> None:
    """Stub used until a subcommand is wired up in a later step."""
    typer.echo(f"{name}: not implemented yet", err=True)
    raise typer.Exit(_NOT_IMPLEMENTED_EXIT)


# ── Top-level options ──────────────────────────────────────────────────────


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sb-stack {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[  # noqa: ARG001 — consumed by the eager callback
        bool,
        typer.Option(
            "--version",
            help="Show the sb-stack version and exit.",
            is_eager=True,
            callback=_version_callback,
        ),
    ] = False,
) -> None:
    """sb-stack root callback. All real work lives in subcommands."""


# ── Schema migrations ──────────────────────────────────────────────────────


@app.command()
def migrate() -> None:
    """Apply pending schema migrations (idempotent)."""
    # Deferred imports keep `sb-stack --help` snappy by avoiding DuckDB /
    # structlog / pydantic-settings imports until a real command needs them.
    from sb_stack.db import DB, MigrationRunner  # noqa: PLC0415
    from sb_stack.logging import configure_logging, get_logger  # noqa: PLC0415
    from sb_stack.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings, process_name="sb-migrate")
    log = get_logger("sb_stack.migrate")
    db = DB(settings)
    runner = MigrationRunner(db, settings, log)
    applied = runner.run()
    typer.echo(f"applied {applied} migration(s)")


# ── Sync ───────────────────────────────────────────────────────────────────


@app.command()
def sync(
    full_refresh: Annotated[
        bool,
        typer.Option("--full-refresh", help="Re-fetch details + re-embed all products."),
    ] = False,
    from_raw: Annotated[
        str | None,
        typer.Option("--from-raw", metavar="DATE", help="Replay a previous day's raw/."),
    ] = None,
    phase: Annotated[
        str | None,
        typer.Option(
            "--phase",
            metavar="PHASE",
            help="Only run one phase: fetch|persist|details|embed|index|finalize.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Fetch + diff report, no writes."),
    ] = False,
) -> None:
    """Trigger a sync run now."""
    if from_raw or phase or dry_run:
        typer.echo(
            "--from-raw, --phase, and --dry-run are not wired up in this "
            "scaffold yet; re-run without them.",
            err=True,
        )
        raise typer.Exit(2)
    from sb_stack.sync.cli import cli_sync  # noqa: PLC0415

    result = cli_sync(full_refresh=full_refresh, reason="manual")
    typer.echo(f"run {result.run_id}: {result.status} ({result.duration_ms} ms)")


@app.command("sync-scheduler")
def sync_scheduler() -> None:
    """Long-running; fires `sync` on SB_SYNC_CRON."""
    import asyncio  # noqa: PLC0415

    from sb_stack.db import DB  # noqa: PLC0415
    from sb_stack.logging import configure_logging, get_logger  # noqa: PLC0415
    from sb_stack.settings import get_settings  # noqa: PLC0415
    from sb_stack.sync.scheduler import run_scheduler  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings, process_name="sb-sync-scheduler")
    log = get_logger("sb_stack.sync_scheduler")
    asyncio.run(run_scheduler(settings=settings, db=DB(settings), logger=log))


@app.command()
def runs(
    limit: Annotated[int, typer.Option("--limit", help="How many to show.")] = 20,
) -> None:
    """List recent sync runs."""
    from sb_stack.sync.cli import cli_runs  # noqa: PLC0415

    rows = cli_runs(limit=limit)
    if not rows:
        typer.echo("no runs recorded yet")
        return
    typer.echo(f"{'run_id':>8}  {'started_at':<26}  {'status':<10}  added  updated  discont")
    for r in rows:
        typer.echo(
            f"{r['run_id']:>8}  {str(r['started_at'] or ''):<26}  "
            f"{(r['status'] or '-'):<10}  "
            f"{r['products_added'] or 0:>5}  "
            f"{r['products_updated'] or 0:>7}  "
            f"{r['products_discontinued'] or 0:>7}"
        )


@app.command("run-info")
def run_info(run_id: Annotated[int, typer.Argument(help="Sync run id.")]) -> None:
    """Show full details (incl. phase breakdown) for one run."""
    import json  # noqa: PLC0415

    from sb_stack.sync.cli import cli_run_info  # noqa: PLC0415

    info = cli_run_info(run_id)
    if info is None:
        typer.echo(f"no run with id={run_id}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(info, indent=2, default=str))


# ── MCP server ─────────────────────────────────────────────────────────────


@app.command()
def mcp(
    transport: Annotated[
        str | None,
        typer.Option("--transport", help="http|stdio (default: from SB_MCP_TRANSPORT)."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", help="Override SB_MCP_PORT."),
    ] = None,
) -> None:
    """Run the MCP server (long-running)."""
    import os  # noqa: PLC0415

    # CLI flags win over env vars; plumb via the settings env prefix so the
    # Settings singleton picks them up uniformly.
    if transport:
        os.environ["SB_MCP_TRANSPORT"] = transport
    if port is not None:
        os.environ["SB_MCP_PORT"] = str(port)

    from sb_stack.mcp_server.server import run  # noqa: PLC0415

    run()


# ── Embedding service ──────────────────────────────────────────────────────


@app.command("embed-server")
def embed_server() -> None:
    """Long-running; serves the embedding model on SB_EMBED_PORT."""
    from sb_stack.embed_server.cli import run  # noqa: PLC0415

    run()


# ── Bootstrap ──────────────────────────────────────────────────────────────


@app.command()
def bootstrap() -> None:
    """Seed home stores from SB_STORE_SUBSET (idempotent)."""
    from sb_stack.bootstrap import bootstrap_home_stores  # noqa: PLC0415
    from sb_stack.logging import configure_logging, get_logger  # noqa: PLC0415
    from sb_stack.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings, process_name="sb-bootstrap")
    log = get_logger("sb_stack.bootstrap")
    counts = bootstrap_home_stores(settings, logger=log)
    typer.echo(
        f"flagged {counts['home_stores_flagged']} home store(s); "
        f"main={counts['main_store_flagged']}"
    )


# ── Diagnostics ────────────────────────────────────────────────────────────


@app.command("extract-key")
def extract_key() -> None:
    """Debug: print current NEXT_PUBLIC_* config extracted from the frontend."""
    import asyncio  # noqa: PLC0415
    import json  # noqa: PLC0415

    from sb_stack.api_client import extract_config  # noqa: PLC0415
    from sb_stack.logging import configure_logging, get_logger  # noqa: PLC0415
    from sb_stack.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings, process_name="sb-extract-key")
    log = get_logger("sb_stack.extract_key")

    cfg = asyncio.run(extract_config(app_base_url=settings.app_base_url, logger=log))
    typer.echo(
        json.dumps(
            {
                "api_key": cfg.api_key,
                "api_management_url": cfg.api_management_url,
                "app_image_storage_url": cfg.app_image_storage_url,
                "cms_url": cfg.cms_url,
                "app_base_url": cfg.app_base_url,
            },
            indent=2,
        )
    )


@app.command()
def doctor(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Machine-readable output."),
    ] = False,
    only: Annotated[
        str | None,
        typer.Option("--only", metavar="NAMES", help="Run specific checks (comma-separated)."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Details + optional checks."),
    ] = False,
    exit_on_warn: Annotated[
        bool,
        typer.Option("--exit-on-warn", help="Treat warn as fail."),
    ] = False,
) -> None:
    """Run healthchecks."""
    import json  # noqa: PLC0415

    from sb_stack.doctor import run_all  # noqa: PLC0415
    from sb_stack.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    names = [n.strip() for n in only.split(",")] if only else None
    result = run_all(settings, only=names, include_optional=verbose)

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "results": [
                        {
                            "name": r.name,
                            "status": r.status,
                            "duration_ms": r.duration_ms,
                            "summary": r.summary,
                            "details": r.details,
                        }
                        for r in result.results
                    ],
                    "summary": {
                        "pass": result.passed,
                        "warn": result.warned,
                        "fail": result.failed,
                    },
                },
                indent=2,
                default=str,
            )
        )
    else:
        icon = {"pass": "OK ", "warn": "WRN", "fail": "ERR"}
        for r in result.results:
            line = f"{icon[r.status]}  {r.name:<24}  {r.summary}"
            typer.echo(line)
            if verbose and r.details:
                typer.echo(f"     └ {r.details}")
        typer.echo(f"\n{result.passed} pass · {result.warned} warn · {result.failed} fail")

    raise typer.Exit(result.exit_code(exit_on_warn=exit_on_warn))


@app.command()
def shell() -> None:
    """Open a read-only DuckDB shell against /data/sb.duckdb."""
    _not_implemented("shell")


if __name__ == "__main__":
    app()
