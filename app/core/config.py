"""Settings for the FastAPI service.

`pydantic-settings` reads environment variables with the
`FC_INVENTORY_` prefix and falls back to the documented defaults.
The two env vars from v1.0.0 (`FC_INVENTORY_BIND`, `FC_INVENTORY_PORT`)
are preserved for backward compatibility.

All collection behaviour (host, port, creds, page size, etc.) comes
from per-request JSON, NOT from settings — this matches the v1.0.0
single-user localhost tool's design.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the FastAPI service."""

    model_config = SettingsConfigDict(
        env_prefix="FC_INVENTORY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Network bind — kept for v1.0.0 backward compatibility.
    bind: str = Field(
        default="127.0.0.1",
        description="Bind address. Use 0.0.0.0 to expose on the LAN; bind behind a reverse proxy in production.",
    )
    port: int = Field(
        default=5000,
        ge=1,
        le=65535,
        description="TCP port for the FastAPI/uvicorn server.",
    )

    # Logging.
    log_file: Path = Field(
        default=Path("fc_inventory.log"),
        description="Rotating log file path.",
    )
    log_level: str = Field(
        default="INFO",
        description="Root log level: DEBUG, INFO, WARNING, ERROR.",
    )
    log_max_bytes: int = Field(
        default=5 * 1024 * 1024,
        description="Maximum size of a single log file before rotation.",
    )
    log_backup_count: int = Field(
        default=3,
        description="Number of rotated log backups to keep.",
    )

    # CORS — disabled by default; populate for browser-based UIs served
    # from a different origin (e.g. a SPA on a CDN).
    cors_origins: list[str] = Field(
        default_factory=list,
        description="Allowed CORS origins. Empty list disables CORS.",
    )

    # Output.
    output_dir: Path = Field(
        default=Path("."),
        description="Directory in which to write the .xlsx file.",
    )
    request_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Default httpx request timeout (seconds).",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance.

    `@lru_cache` makes this safe to call from any number of FastAPI
    `Depends` resolvers without re-parsing the environment on each
    request.
    """
    return Settings()
