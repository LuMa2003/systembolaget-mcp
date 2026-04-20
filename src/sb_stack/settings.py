"""Runtime settings for sb-stack.

Single `Settings` class, env-var-driven, lazy singleton via `get_settings()`.
All tunables live here — no scattered module-level constants.

See docs/06_module_layout.md §Settings + docs/07_deployment.md §Environment
variables for the authoritative schema.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["debug", "info", "warning", "error", "critical"]
LogFormat = Literal["json", "text"]
MCPTransport = Literal["http", "stdio"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SB_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Storage ──────────────────────────────────────────────────────────
    data_dir: Path = Path("/data")

    # ── Home stores (siteIds) ────────────────────────────────────────────
    # NoDecode disables pydantic-settings' default JSON parsing of list-typed
    # env vars, letting our `_parse_store_subset` validator split CSV input.
    store_subset: Annotated[list[str], NoDecode] = ["1701", "1702", "1716", "1718"]
    main_store: str = "1701"

    # ── Systembolaget API ────────────────────────────────────────────────
    # Ecommerce APIM key — extracted from the public frontend JS at runtime
    # when unset. Covers /sb-api-ecommerce/v1/*. See api_client.config_extractor.
    api_key: str | None = None
    api_key_cache_ttl_days: int = 7
    # Mobile APIM key — required for /sb-api-mobile/v1/* (stock + taxonomy).
    # Default is a long-lived key discovered by C4illin/systembolaget-data and
    # still accepted server-side; it is NOT in the web frontend, so if
    # Systembolaget revokes it we'll start 401'ing on mobile endpoints only.
    # Replace with a fresh key captured from the mobile app if that happens —
    # the orchestrator fires a one-shot ntfy alert when it does.
    api_key_mobile: str = "cfc702aed3094c86b92d6d4ff7a54c84"
    api_base_url: str = "https://api-extern.systembolaget.se"
    app_base_url: str = "https://www.systembolaget.se"

    # ── Embedding ────────────────────────────────────────────────────────
    embed_url: str = "http://localhost:9000/v1/embeddings"
    embed_model: str = "Qwen/Qwen3-Embedding-4B"
    embed_dim: int = 2560
    embed_device: str = "cuda:0"  # embed_server only
    embed_port: int = 9000  # embed_server only
    embed_max_batch: int = 2048  # server hard limit per request
    embed_gpu_batch_size: int = 32  # server internal GPU batching
    embed_client_batch_size: int = 128  # client-side chunking

    # ── MCP ──────────────────────────────────────────────────────────────
    mcp_port: int = 8000
    mcp_transport: MCPTransport = "http"
    mcp_token: str | None = None

    # ── Sync ─────────────────────────────────────────────────────────────
    sync_cron: str = "0 4 * * *"
    sync_timezone: str = "Europe/Stockholm"
    first_run_on_bootstrap: bool = True
    sync_concurrency: int = 5

    # ── Retention ────────────────────────────────────────────────────────
    backup_retention_days: int = 7
    raw_retention_days: int = 365

    # ── Phase timeouts (minutes) ─────────────────────────────────────────
    phase_fetch_timeout_minutes: int = 20
    phase_persist_timeout_minutes: int = 5
    phase_details_timeout_minutes: int = 60
    phase_embed_timeout_minutes: int = 60
    phase_index_timeout_minutes: int = 2
    phase_finalize_timeout_minutes: int = 2

    # ── Ntfy (optional) ──────────────────────────────────────────────────
    ntfy_url: str | None = None
    ntfy_token: str | None = None
    ntfy_cooldown_hours: int = 6
    ntfy_min_priority: int = 3

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: LogLevel = "info"
    log_format: LogFormat = "json"
    log_to_file: bool = True
    log_to_stdout: bool = True

    # ── Validators ───────────────────────────────────────────────────────
    @field_validator("store_subset", mode="before")
    @classmethod
    def _parse_store_subset(cls, v: object) -> object:
        # Accept comma-separated strings from env, pass through lists unchanged.
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, v: object) -> object:
        if isinstance(v, str):
            return v.lower()
        return v

    # ── Derived paths ────────────────────────────────────────────────────
    @property
    def db_path(self) -> Path:
        return self.data_dir / "sb.duckdb"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"

    @property
    def pre_migration_backup_dir(self) -> Path:
        return self.backup_dir / "pre-migration"

    @property
    def models_cache_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def duckdb_ext_dir(self) -> Path:
        return self.data_dir / "duckdb_extensions"


@functools.cache
def get_settings() -> Settings:
    """Return the process-wide Settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached Settings. Test-only — lets a fixture rebuild after env edits."""
    get_settings.cache_clear()
