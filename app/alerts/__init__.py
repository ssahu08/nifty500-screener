"""Telegram alert integration (Phase 5)."""

from __future__ import annotations

from app.alerts.telegram import TelegramAlerter, test_alert
from app.config import Settings

__all__ = ["TelegramAlerter", "send_alert", "test_alert"]


def send_alert(settings: Settings, strategy_name: str, signals: list) -> bool:
    """Convenience helper used by the scheduler and API."""
    return TelegramAlerter(settings).send_strategy_results(strategy_name, signals)


def send_test_alert(settings: Settings) -> bool:
    """Send a Telegram test message to verify configuration."""
    return test_alert(settings)
