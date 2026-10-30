"""Tests for the strategy base classes and metadata."""

from __future__ import annotations

from datetime import UTC, datetime

from app.strategies.base import Category, Signal, StrategyMetadata


def test_category_values() -> None:
    assert Category.INTRADAY == "intraday"
    assert Category.SHORT_TERM == "short-term"
    assert Category.LONG_TERM == "long-term"
    assert Category.EVENT_DRIVEN == "event-driven"


def test_metadata_fields() -> None:
    meta = StrategyMetadata(
        name="Test",
        description="desc",
        category=Category.INTRADAY,
        universe="NIFTY_500",
        schedule_type="auto",
        timeframes=["daily"],
        frequency_minutes=10,
        candle_interval="10m",
    )
    assert meta.name == "Test"
    assert meta.frequency_minutes == 10


def test_signal_defaults() -> None:
    signal = Signal(
        symbol="NSE:SBIN-EQ",
        strategy_name="Test",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        price=100.0,
    )
    assert signal.metadata == {}
    assert signal.price == 100.0
