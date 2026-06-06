"""Tests for pydantic-settings `Settings`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_settings_defaults():
    s = Settings(_env_file=None)  # bypass .env lookup
    assert s.bind == "127.0.0.1"
    assert s.port == 5000
    assert s.log_level == "INFO"
    assert s.log_max_bytes == 5 * 1024 * 1024
    assert s.log_backup_count == 3
    # output_dir defaults to Path('.') — its string form is ".".
    assert str(s.output_dir) == "."
    assert s.request_timeout_seconds == 60.0
    assert s.cors_origins == []


def test_settings_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC_INVENTORY_BIND", "0.0.0.0")
    monkeypatch.setenv("FC_INVENTORY_PORT", "8080")
    monkeypatch.setenv("FC_INVENTORY_LOG_LEVEL", "DEBUG")
    s = Settings(_env_file=None)
    assert s.bind == "0.0.0.0"
    assert s.port == 8080
    assert s.log_level == "DEBUG"


def test_settings_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC_INVENTORY_PORT", "99999")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_cors_origins_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    # pydantic-settings parses list[str] from a JSON-ish env value.
    monkeypatch.setenv("FC_INVENTORY_CORS_ORIGINS", '["http://a","http://b"]')
    s = Settings(_env_file=None)
    assert s.cors_origins == ["http://a", "http://b"]


def test_get_settings_returns_cached_instance():
    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b
