"""Nifty option helpers: weekly expiry dates and FYERS option-symbol construction.

Nifty weekly options expire every Tuesday. FYERS option symbols follow the
pattern ``NSE:NIFTY{DD}{MMM}{STRIKE}{CE|PE}`` (e.g. ``NSE:NIFTY27OCT22700CE``).

FYERS spot symbol for Nifty 50 index: ``NSE:NIFTY50-INDEX``.
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

#: Nifty weekly options expire every Tuesday (ISO weekday 1).
#: SEBI directive (Oct 2024): NSE kept Nifty weekly on Tuesday from 1 Sep 2025.
TUESDAY = 1

#: FYERS spot symbol for the Nifty 50 index.
NIFTY_SPOT_SYMBOL = "NSE:NIFTY50-INDEX"


def next_weekly_expiry(on: date | None = None) -> date:
    """Return the next Nifty weekly expiry (Tuesday) strictly on/after ``on``.

    If ``on`` is already a Tuesday, it is returned (the current weekly
    contract is still live). Otherwise the coming Tuesday is returned.
    """
    on = on or date.today()
    days_ahead = TUESDAY - on.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return on + timedelta(days=days_ahead)


def expiry_to_fyers_format(expiry: date) -> str:
    """Convert a date to the FYERS option expiry fragment: ``DDMMM``.

    FYERS option symbols do NOT include the year. The day+month combination
    uniquely identifies the nearest expiry.

    Example: ``date(2026, 10, 27)`` -> ``"27OCT"``.
    """
    return expiry.strftime("%d%b").upper()


def nifty_option_symbol(strike: int, option_type: str, expiry: date) -> str:
    """Build a FYERS Nifty option symbol.

    Format: ``NSE:NIFTY{DD}{MMM}{STRIKE}{CE|PE}``

    Example: ``nifty_option_symbol(25000, "CE", date(2026, 6, 25))`` ->
    ``"NSE:NIFTY25JUN25000CE"``.
    """
    option_type = option_type.upper()
    if option_type not in {"CE", "PE"}:
        raise ValueError(f"Invalid option type: {option_type!r}")
    return f"NSE:NIFTY{expiry_to_fyers_format(expiry)}{strike}{option_type}"


def round_to_nifty_strike(spot: float, step: int = 50) -> int:
    """Round a Nifty spot price to the nearest strike (default 50-point step)."""
    return int(round(spot / step) * step)
