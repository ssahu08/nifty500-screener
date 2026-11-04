"""Tests for NSE trading-calendar helpers."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.market import (
    is_last_trading_day_of_month,
    is_last_trading_day_of_week,
    is_market_open,
    next_trading_day,
)

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


def test_last_trading_day_of_week_detects_friday() -> None:
    # Friday 2026-01-30 is the last trading day of its ISO week.
    assert is_last_trading_day_of_week(date(2026, 1, 30))
    assert not is_last_trading_day_of_week(date(2026, 1, 29))


def test_last_trading_day_of_week_handles_holiday_friday() -> None:
    # 2026-04-03 is a Friday holiday, so Thursday 2026-04-02 is the last
    # trading day of that week.
    assert is_last_trading_day_of_week(date(2026, 4, 2))
    assert not is_last_trading_day_of_week(date(2026, 4, 1))


def test_last_trading_day_of_month() -> None:
    # 2026-01-31 is a Saturday, so Friday 2026-01-30 is the month-end.
    assert is_last_trading_day_of_month(date(2026, 1, 30))
    assert not is_last_trading_day_of_month(date(2026, 1, 29))
    assert is_last_trading_day_of_month(date(2026, 11, 30))
