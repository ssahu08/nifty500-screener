"""Tests for NSE trading-calendar helpers."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.market import is_market_open, next_trading_day

IST = ZoneInfo("Asia/Kolkata")


def test_next_trading_day_midweek() -> None:
    # Wednesday -> Thursday.
    assert next_trading_day(date(2026, 10, 14)) == date(2026, 10, 15)


def test_next_trading_day_skips_weekend() -> None:
    # Friday 2026-10-16 -> Monday 2026-10-19 (weekend skipped).
    assert next_trading_day(date(2026, 10, 16)) == date(2026, 10, 19)


def test_is_market_open_time_boundaries() -> None:
    day = date(2026, 10, 14)  # a Wednesday
    assert is_market_open(datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST))
    assert is_market_open(datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST))
    assert not is_market_open(datetime(day.year, day.month, day.day, 9, 14, tzinfo=IST))
    assert not is_market_open(datetime(day.year, day.month, day.day, 15, 31, tzinfo=IST))


def test_is_market_open_closed_on_weekends() -> None:
    saturday = date(2026, 10, 17)
    close_time = datetime(saturday.year, saturday.month, saturday.day, 11, 0, tzinfo=IST)
    assert not is_market_open(close_time)
