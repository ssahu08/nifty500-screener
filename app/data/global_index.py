"""Global index data via yfinance (Yahoo Finance) for the Market Overview.

NSE data comes from FYERS; this provider supplies the non-NSE indices
(Nasdaq, Dow, S&P, NYSE, Shanghai, Nikkei, Shenzhen, KOSPI, Euronext 100).
All requests are free and open-source (NFR-3).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Row label -> Yahoo Finance ticker symbol (BRD Section 4.6 mapping).
INDEX_TICKERS: dict[str, str] = {
    "Nifty": "^NSEI",
    "Nasdaq": "^IXIC",
    "DOW": "^DJI",
    "S&P": "^GSPC",
    "NYSE": "^NYA",
    "SSE": "000001.SS",
    "JPX": "^N225",
    "SZSE": "399001.SZ",
    "KRX": "^KS11",
    "Euronext": "^N100",
}

#: Fallback tickers used when a primary symbol returns no data.
FALLBACK_TICKERS: dict[str, list[str]] = {
    "SSE": ["000001.SS"],
    "SZSE": ["399001.SZ"],
    "KRX": ["^KS11"],
}


class GlobalIndexProvider:
    """Fetch daily closing prices for global indices."""

    def __init__(self, cache: object | None = None) -> None:
        self.cache = cache

    def get_close(self, index: str, day: date, yfinance=None) -> float | None:
        """Closing price of ``index`` on ``day`` exactly, or ``None`` if no session.

        No forward-fill: a market closed on ``day`` returns ``None`` so the UI
        can render "Closed".
        """
        start = day - timedelta(days=10)
        end = day + timedelta(days=2)
        df = self.get_history(index, start, end, yfinance=yfinance)
        if df is None or df.empty:
            return None
        rows = df["close"][df["close"].index.date == day]
        return float(rows.iloc[-1]) if not rows.empty else None

    def get_history(
        self,
        index: str,
        start: date,
        end: date,
        yfinance=None,
    ) -> pd.DataFrame | None:
        """Daily OHLCV for ``index`` between ``start`` and ``end``.

        ``yfinance`` may be injected for testing; defaults to the yfinance API.
        """
        yf = yfinance or _default_yfinance()
        ticker = INDEX_TICKERS.get(index)
        if ticker is None:
            raise KeyError(f"Unknown index {index!r}; choose from {sorted(INDEX_TICKERS)}")

        for symbol in [ticker, *FALLBACK_TICKERS.get(index, [])]:
            try:
                data = yf.Ticker(symbol).history(start=str(start), end=str(end), timeout=15)
            except Exception as exc:  # noqa: BLE001 - network/region failures are common
                logger.warning("yfinance fetch failed for %s: %s", symbol, exc)
                continue
            if data is not None and not data.empty:
                return self._normalise(data)
        return None

    def get_overview(
        self,
        trading_days: list[date],
        yfinance=None,
    ) -> dict[str, dict[date, float | None]]:
        """Return ``{index: {day: close_or_None}}`` for the given trading days.

        A day with no session for an index (weekend or holiday) maps to
        ``None`` so the UI can render "Closed". No forward-fill is applied.
        """
        if not trading_days:
            return {name: {} for name in INDEX_TICKERS}

        start = min(trading_days) - timedelta(days=10)
        end = max(trading_days) + timedelta(days=2)
        overview: dict[str, dict[date, float | None]] = {}

        for index in INDEX_TICKERS:
            df = self.get_history(index, start, end, yfinance=yfinance)
            series: dict[date, float | None] = {day: None for day in trading_days}
            if df is not None and not df.empty:
                closes = df["close"]
                present = closes.index.date
                for day in trading_days:
                    if day in present:
                        day_rows = closes[closes.index.date == day]
                        value = float(day_rows.iloc[-1])
                        # yfinance reports missing sessions as NaN; normalise
                        # to None so the API emits strict JSON (browsers reject
                        # bare NaN).
                        series[day] = None if value != value else value
            overview[index] = series

        return overview

    @staticmethod
    def _normalise(data: pd.DataFrame) -> pd.DataFrame:
        df = data.copy()
        # yfinance returns capitalized names (Open, High, ...); normalise to
        # lowercase so downstream code has a single convention.
        df.columns = [str(c).lower() for c in df.columns]
        if isinstance(df.index, pd.DatetimeIndex):
            df.index = df.index.tz_localize(None) if df.index.tz else df.index
        df.index = pd.to_datetime(df.index)
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        return df[keep].sort_index()


def _default_yfinance():
    import yfinance as yf

    return yf
