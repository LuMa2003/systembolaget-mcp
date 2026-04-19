"""Exception hierarchy for sb-stack.

Every exception carries structured context in `__str__` (product_number,
site_id, url, etc.) so logs and tracebacks are actionable without having to
crack open a debugger.
"""

from __future__ import annotations


class SBError(Exception):
    """Base for all sb-stack errors."""


# ── Migrations ───────────────────────────────────────────────────────────────


class MigrationError(SBError):
    """Raised when a schema migration fails to apply or verify."""


class ChecksumMismatchError(MigrationError):
    """An applied migration's on-disk sha256 does not match the recorded value."""

    def __init__(self, version: int, expected: str, got: str) -> None:
        self.version = version
        self.expected = expected
        self.got = got
        super().__init__(
            f"migration {version:03d}: sha256 mismatch (expected={expected[:12]}…, got={got[:12]}…)"
        )


# ── API client ───────────────────────────────────────────────────────────────


class ConfigExtractionError(SBError):
    """Failed to extract NEXT_PUBLIC_* config (subscription key) from the frontend."""


class SystembolagetAPIError(SBError):
    """Base for any HTTP error from the Systembolaget public API."""

    def __init__(self, message: str, *, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"{message} [status={status_code} url={url}]")


class RateLimitedError(SystembolagetAPIError):
    """429 or observed throttling."""


class AuthenticationError(SystembolagetAPIError):
    """401/403 — subscription key invalid or revoked."""


class NotFoundError(SystembolagetAPIError):
    """404 — resource does not exist."""


class ServerError(SystembolagetAPIError):
    """5xx — upstream failure."""


# ── Sync ─────────────────────────────────────────────────────────────────────


class SyncError(SBError):
    """Base for sync-pipeline errors."""


class PartialSyncError(SyncError):
    """Sync completed with one or more non-fatal phase outcomes."""


class EmbeddingError(SyncError):
    """Embedding generation failed (client or server side)."""


class PhaseTimeoutError(SyncError):
    """A phase exceeded its configured budget."""

    def __init__(self, phase: str, budget_s: float) -> None:
        self.phase = phase
        self.budget_s = budget_s
        super().__init__(f"phase {phase!r} exceeded {budget_s:.0f}s budget")


# ── MCP ──────────────────────────────────────────────────────────────────────


class MCPError(SBError):
    """Base for MCP-tool errors."""


class ProductNotFoundError(MCPError):
    """No product matches the provided productNumber / nr."""

    def __init__(self, product_number: str) -> None:
        self.product_number = product_number
        super().__init__(f"product not found: {product_number}")


class InvalidInputError(MCPError):
    """Tool input failed validation beyond what Pydantic catches."""


class DataStalenessError(MCPError):
    """Data is past the freshness SLA and the tool refused to answer."""

    def __init__(self, hours_since_sync: float) -> None:
        self.hours_since_sync = hours_since_sync
        super().__init__(
            f"data is stale (last sync {hours_since_sync:.1f} h ago); "
            "run `sb-stack sync` or wait for the scheduler"
        )
