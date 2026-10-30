"""Market-data provider interface and shared types.

All market data flows through :class:`MarketDataProvider` so a different
provider (a second broker, a different feed) can replace FYERS later without
touching strategy code (BRD Section 6).

Consumers receive:

* realtime quotes as :class:`Quote` objects (symbol -> quote), and
* OHLCV candles as a :class:`pandas.DataFrame` indexed by IST timestamp with
  columns ``open, high, low, close, volume``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

import pandas as pd

#: Columns every candle :class:`~pandas.DataFrame` exposes.
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class Quote:
    """A lightweight realtime quote for one symbol."""

    __slots__ = ("symbol", "ltp", "open", "high", "low", "prev_close", "volume", "timestamp")

    def __init__(
        self,
        symbol: str,
        ltp: float,
        *,
        open: float | None = None,
        high: float | None = None,
        low: float | None = None,
        prev_close: float | None = None,
        volume: float | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        self.symbol = symbol
        self.ltp = ltp
        self.open = open
        self.high = high
        self.low = low
        self.prev_close = prev_close
        self.volume = volume
        self.timestamp = timestamp

    @property
    def change_pct(self) -> float | None:
        if self.prev_close and self.prev_close != 0:
            return round((self.ltp - self.prev_close) / self.prev_close * 100, 2)
        return None


class MarketDataProvider(ABC):
    """Abstract contract for a market-data source."""

    #: Maximum symbols accepted by a single quotes request (provider-specific).
    QUOTE_BATCH_SIZE = 50

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch realtime quotes for ``symbols``.

        Must accept batches of at least ``QUOTE_BATCH_SIZE`` symbols and return
        a mapping symbol -> :class:`Quote` for every symbol it could resolve.
        """
        raise NotImplementedError

    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles for ``symbol`` between ``start`` and ``end``.

        ``interval`` uses the provider's own resolution naming. The returned
        frame is indexed by IST timestamp and has columns ``OHLCV_COLUMNS``.
        """
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        """Return ``True`` when the provider is reachable and authenticated."""
        raise NotImplementedError


def normalize_symbol(symbol: str) -> str:
    """Normalise a symbol to FYERS format (``NSE:SBIN-EQ``).

    A bare ticker (``SBIN``) is treated as an NSE equity. Symbols that already
    carry an exchange prefix are passed through unchanged.
    """
    symbol = symbol.strip().upper()
    if ":" in symbol:
        return symbol
    return f"NSE:{symbol}-EQ"


def chunked(symbols: list[str], size: int) -> list[list[str]]:
    """Split ``symbols`` into batches of at most ``size`` items."""
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]
