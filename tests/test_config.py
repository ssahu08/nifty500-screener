"""Tests for configuration loading."""

from __future__ import annotations

from app.config import Settings


def test_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_name == "N500 Stock Screener"
    assert settings.retention_days == 14
    assert settings.timezone == "Asia/Kolkata"
    assert settings.telegram_notify_empty is False
    assert settings.global_index_provider == "yfinance"


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("RETENTION_DAYS", "7")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    settings = Settings(_env_file=None)
    assert settings.retention_days == 7
    assert settings.telegram_configured is False


def test_configured_flags(monkeypatch) -> None:
    monkeypatch.setenv("FYERS_CLIENT_ID", "abc")
    monkeypatch.setenv("FYERS_SECRET_KEY", "def")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    settings = Settings(_env_file=None)
    assert settings.fyers_configured is True
    assert settings.telegram_configured is True
