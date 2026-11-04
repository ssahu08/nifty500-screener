"""NSE market-hours and trading-calendar helpers.

Phase 1 provides the interface and a basic implementation. The holiday
calendar is loaded from ``app/core/nse_holidays.json`` when present and is
expected to be completed in the scheduler phase.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import get_settings

_HOLIDAYS_FILE = Path(__file__).with_name("nse_holidays.json")

# Regular NSE equity session (IST).
MARKET_OPEN = time(9, 15)
#: Continuous trading ends here; the closing auction session (CAS) runs
#: 15:15-15:30 and produces the official closing price. FYERS still returns
#: "10-minute" bars for the auction window (a flat, ~zero-volume bar) and the
#: 5-minute auction match, so candle strategies must not treat those as real
#: continuous-session candles.
CONTINUOUS_CLOSE = time(15, 15)
MARKET_CLOSE = time(15, 30)


def _ist() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def load_holidays() -> set[date]:
    """Return the set of NSE holidays, or an empty set if none configured."""
    if not _HOLIDAYS_FILE.exists():
        return set()
    try:
        raw = json.loads(_HOLIDAYS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    days: set[date] = set()
    for value in raw if isinstance(raw, list) else []:
        try:
            days.add(date.fromisoformat(value))
        except (TypeError, ValueError):
            continue
    return days


def is_trading_day(day: date) -> bool:
    """True when ``day`` is a weekday and not an NSE holiday."""
    return day.weekday() < 5 and day not in load_holidays()


def is_market_open(now: datetime | None = None) -> bool:
    """True when the NSE equity session is currently open."""
    now = now.astimezone(_ist()) if now else datetime.now(_ist())
    return is_trading_day(now.date()) and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def next_trading_day(day: date) -> date:
    """The first NSE trading day strictly after ``day``."""
    cursor = day + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def is_last_trading_day_of_week(day: date) -> bool:
    """True when ``day`` is the last NSE trading day of its ISO week.

    Compares against the following days (Fri/Sat/Sun) to see whether any later
    trading day exists in the same week, so holidays are handled correctly.
    """
    if not is_trading_day(day):
        return False
    # Only the remaining days of THIS ISO week (Saturday/Sunday when the day
    # is Friday). Using ``8 - weekday`` would incorrectly include next Monday
    # and make Friday never look like a week-end.
    for offset in range(1, 7 - day.weekday()):
        candidate = day + timedelta(days=offset)
        if is_trading_day(candidate):
            return False
    return True


def is_last_trading_day_of_month(day: date) -> bool:
    """True when ``day`` is the last NSE trading day of its calendar month."""
    if not is_trading_day(day):
        return False
    nxt = day + timedelta(days=1)
    # Walk forward to the end of the month looking for a later trading day.
    while nxt.month == day.month:
        if is_trading_day(nxt):
            return False
        nxt += timedelta(days=1)
    return True


def last_trading_days(count: int, up_to: date | None = None) -> list[date]:
    """Return the last ``count`` NSE trading days ending on ``up_to``.

    ``up_to`` defaults to today; today is included only when it is a trading
    day. The result is ordered oldest -> newest.
    """
    days: list[date] = []
    cursor = up_to or date.today()
    while len(days) < count:
        if is_trading_day(cursor):
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))
