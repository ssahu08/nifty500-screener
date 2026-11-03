"""Nifty option helpers: expiry dates and FYERS option-symbol construction.

FYERS uses **two** Nifty option-symbol formats:

* **weekly** - ``NSE:NIFTY{YY}{M}{DD}{STRIKE}{CE|PE}``
  (e.g. ``NSE:NIFTY26N0326000CE`` for the 3 Nov 2026 weekly).
* **monthly** - ``NSE:NIFTY{YY}{MMM}{STRIKE}{CE|PE}``
  (e.g. ``NSE:NIFTY26NOV26000CE`` for the 23 Nov 2026 monthly).

``M`` is the FYERS single-letter month code (``1``-``9``, ``O``, ``N``,
``D``) and ``MMM`` is the three-letter month (``JAN``-``DEC``). The **last**
weekly expiry of a calendar month is the monthly contract and uses the
``MMM`` form; every other weekly expiry uses the single-letter form.

Expiries are Tuesdays, but an NSE holiday on a Tuesday shifts the expiry to
the previous trading day (usually Monday). The shared NSE holiday calendar in
:mod:`app.core.market` is used for that shift.

FYERS spot symbol for the Nifty 50 index: ``NSE:NIFTY50-INDEX``.
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from app.core.market import is_trading_day

IST = ZoneInfo("Asia/Kolkata")

#: Nifty weekly options expire every Tuesday (ISO weekday 1).
#: SEBI directive (Oct 2024): NSE kept Nifty weekly on Tuesday from 1 Sep 2025.
TUESDAY = 1

#: FYERS spot symbol for the Nifty 50 index.
NIFTY_SPOT_SYMBOL = "NSE:NIFTY50-INDEX"

#: FYERS single-letter month codes used in *weekly* option symbols.
_WEEKLY_MONTH_CODES = {
    1: "1",
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    8: "8",
    9: "9",
    10: "O",
    11: "N",
    12: "D",
}


def _last_tuesday(year: int, month: int) -> date:
    """Return the last Tuesday of ``year``/``month``."""
    if month == 12:
        first_of_next = date(year + 1, 1, 1)
    else:
        first_of_next = date(year, month + 1, 1)
    cursor = first_of_next - timedelta(days=1)
    while cursor.weekday() != TUESDAY:
        cursor -= timedelta(days=1)
    return cursor


def _shift_to_trading_day(day: date) -> date:
    """Move ``day`` back to the previous trading day when it is a holiday."""
    cursor = day
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def monthly_expiry(year: int, month: int) -> date:
    """The Nifty monthly expiry for ``year``/``month`` (holiday-shifted).

    The monthly contract expires on the last Tuesday of the month; if that
    Tuesday is an NSE holiday it expires on the previous trading day.
    """
    return _shift_to_trading_day(_last_tuesday(year, month))


def is_monthly_expiry(expiry: date) -> bool:
    """True when ``expiry`` is the monthly (last-of-month) contract."""
    return expiry == monthly_expiry(expiry.year, expiry.month)


def _candidate_expiries(on: date, weeks: int = 10) -> list[date]:
    """Expiry dates on/after ``on`` for the next ``weeks`` Tuesdays."""
    first = on + timedelta(days=(TUESDAY - on.weekday()) % 7)
    found: list[date] = []
    for index in range(weeks):
        tuesday = first + timedelta(weeks=index)
        expiry = _shift_to_trading_day(tuesday)
        if expiry >= on and expiry not in found:
            found.append(expiry)
    return found


def next_weekly_expiry(on: date | None = None) -> date:
    """Return the next Nifty expiry (weekly or monthly) on/after ``on``.

    If ``on`` is itself an expiry day (including a holiday-shifted Monday) it
    is returned. Holidays that fall on a Tuesday shift that week's expiry to
    the previous trading day.
    """
    on = on or date.today()
    candidates = _candidate_expiries(on)
    if not candidates:
        # Defensive: a 10-week window always contains a trading Tuesday.
        cursor = on
        while not is_trading_day(cursor):
            cursor += timedelta(days=1)
        return cursor
    return min(candidates)


def is_nifty_expiry_day(day: date) -> bool:
    """True when ``day`` is a Nifty weekly/monthly expiry day."""
    return is_trading_day(day) and next_weekly_expiry(day) == day


def expiry_kind(expiry: date) -> str:
    """Return ``"monthly"`` or ``"weekly"`` for the given expiry."""
    return "monthly" if is_monthly_expiry(expiry) else "weekly"


def expiry_to_fyers_format(expiry: date) -> str:
    """Return the FYERS expiry fragment (year + month/day code).

    Examples::

        date(2026, 11, 3)  -> "26N03"   (weekly)
        date(2026, 11, 23) -> "26NOV"   (monthly)
    """
    year = expiry.strftime("%y")
    if is_monthly_expiry(expiry):
        return f"{year}{expiry.strftime('%b').upper()}"
    month = _WEEKLY_MONTH_CODES[expiry.month]
    return f"{year}{month}{expiry.day:02d}"


def nifty_option_symbol(strike: int, option_type: str, expiry: date) -> str:
    """Build a FYERS Nifty option symbol.

    Example: ``nifty_option_symbol(26000, "CE", date(2026, 11, 3))`` ->
    ``"NSE:NIFTY26N0326000CE"``.
    """
    option_type = option_type.upper()
    if option_type not in {"CE", "PE"}:
        raise ValueError(f"Invalid option type: {option_type!r}")
    return f"NSE:NIFTY{expiry_to_fyers_format(expiry)}{strike}{option_type}"


def nifty_option_symbol_candidates(
    strike: int, option_type: str, expiry: date
) -> list[str]:
    """Return symbol candidates for ``expiry``, most likely first.

    The primary symbol is always the correctly formatted one. Safe fallbacks
    are added only when they cannot silently point at a *different* expiry:

    * a monthly expiry also tries the single-letter weekly form for the same
      date (harmless if the contract does not exist);
    * a holiday-shifted weekly expiry (a Monday) also tries the nominal
      Tuesday form.

    A weekly expiry never falls back to the monthly form, because that symbol
    would refer to a later expiry in the same month.
    """
    option_type = option_type.upper()
    candidates = [nifty_option_symbol(strike, option_type, expiry)]

    if is_monthly_expiry(expiry):
        year = expiry.strftime("%y")
        month = _WEEKLY_MONTH_CODES[expiry.month]
        candidates.append(f"NSE:NIFTY{year}{month}{expiry.day:02d}{strike}{option_type}")
    elif expiry.weekday() != TUESDAY:
        # Holiday-shifted expiry: also try the nominal Tuesday.
        nominal = expiry
        while nominal.weekday() != TUESDAY:
            nominal += timedelta(days=1)
        if nominal != expiry:
            candidates.append(nifty_option_symbol(strike, option_type, nominal))

    # De-duplicate while preserving order.
    return list(dict.fromkeys(candidates))


def round_to_nifty_strike(spot: float, step: int = 50) -> int:
    """Round a Nifty spot price to the nearest strike (default 50-point step)."""
    return int(round(spot / step) * step)
