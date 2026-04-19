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
    _not_implemented("migrate")


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
    _not_implemented("sync")


@app.command("sync-scheduler")
def sync_scheduler() -> None:
    """Long-running; fires `sync` on SB_SYNC_CRON."""
    _not_implemented("sync-scheduler")


@app.command()
def runs(
    limit: Annotated[int, typer.Option("--limit", help="How many to show.")] = 20,
) -> None:
    """List recent sync runs."""
    _not_implemented("runs")


@app.command("run-info")
def run_info(run_id: Annotated[str, typer.Argument(help="Sync run id.")]) -> None:
    """Show full details (incl. phase breakdown) for one run."""
    _not_implemented("run-info")


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
    _not_implemented("mcp")


# ── Embedding service ──────────────────────────────────────────────────────


@app.command("embed-server")
def embed_server() -> None:
    """Long-running; serves the embedding model on SB_EMBED_PORT."""
    _not_implemented("embed-server")


# ── Bootstrap ──────────────────────────────────────────────────────────────


@app.command()
def bootstrap() -> None:
    """Seed home stores from SB_STORE_SUBSET (idempotent)."""
    _not_implemented("bootstrap")


# ── Diagnostics ────────────────────────────────────────────────────────────


@app.command("extract-key")
def extract_key() -> None:
    """Debug: print current NEXT_PUBLIC_* config extracted from the frontend."""
    _not_implemented("extract-key")


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
    _not_implemented("doctor")


@app.command()
def shell() -> None:
    """Open a read-only DuckDB shell against /data/sb.duckdb."""
    _not_implemented("shell")


if __name__ == "__main__":
    app()
