"""yfinance-backed market-data provider (Yahoo Finance) for off-hours use.

FYERS stays the live NSE source; this provider serves after-market-closed
scans, manual reruns and development so broker API quota is only spent when
real-time data is actually needed. It needs no API key and has no daily quota.

Interval support:

* ``daily`` / ``1d`` - native Yahoo daily bars (indexed at IST midnight).
* ``60m`` / ``1h``  - native Yahoo hourly bars.
* ``10m``           - Yahoo carries no 10-minute bars, so 5-minute bars are
  fetched and resampled on the NSE session grid (09:15, 09:25, ...).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from app.core.logging import get_logger
from app.data.provider import (
    OHLCV_COLUMNS,
    MarketDataProvider,
    Quote,
    chunked,
    normalize_symbol,
)

logger = get_logger(__name__)

IST = "Asia/Kolkata"

#: Nifty index symbols that map to Yahoo Finance index tickers.
INDEX_TO_YAHOO = {
    "NSE:NIFTY 50-EQ": "^NSEI",
    "NSE:NIFTY 50-INDEX": "^NSEI",
    "NSE:NIFTY50-EQ": "^NSEI",
}

#: This provider's interval name -> Yahoo interval.
INTERVAL_TO_YAHOO = {
    "10m": "5m",
    "10min": "5m",
    "10-minute": "5m",
    "60m": "60m",
    "1h": "60m",
    "1hour": "60m",
    "daily": "1d",
    "1d": "1d",
    "day": "1d",
}

#: Yahoo refuses intraday history older than ~60 days.
_INTRADAY_MAX_DAYS = 59

#: Yahoo price fields, used to flatten single-ticker MultiIndex columns.
_YAHOO_FIELDS = {"open", "high", "low", "close", "adj close", "volume"}


def to_yahoo_symbol(symbol: str) -> str:
    """Map an FYERS-style symbol (``NSE:SBIN-EQ``) to a Yahoo ticker."""
    norm = normalize_symbol(symbol)
    if norm in INDEX_TO_YAHOO:
        return INDEX_TO_YAHOO[norm]
    if norm.startswith("NSE:") and norm.endswith("-EQ"):
        return f"{norm[4:-3]}.NS"
    return norm


def _default_yfinance():
    import yfinance as yf

    return yf


def _to_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns, ensure a sorted DatetimeIndex, keep OHLCV."""
    frame = df.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [
            next((part for part in col if str(part).lower() in _YAHOO_FIELDS), col[0])
            for col in frame.columns
        ]
    frame.columns = [str(c).lower() for c in frame.columns]
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    if frame.index.tz is not None:
        frame.index = frame.index.tz_convert(IST)
    else:
        frame.index = frame.index.tz_localize(IST)
    keep = [c for c in OHLCV_COLUMNS if c in frame.columns]
    frame = frame[keep].sort_index()
    if "volume" in frame.columns:
        frame["volume"] = frame["volume"].fillna(0.0)
    return frame


def _resample_10m(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 5-minute bars onto the NSE 10-minute grid (09:15, 09:25, ...)."""
    if df.empty:
        return df
    agg: dict[str, str] = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    out = df.resample(
        "10min",
        origin="start_day",
        offset=timedelta(minutes=15),
        label="left",
        closed="left",
    ).agg(agg)
    return out.dropna(subset=["open", "close"])


class YFinanceProvider(MarketDataProvider):
    """Market-data provider backed by Yahoo Finance via ``yfinance``.

    ``yfinance_model`` may be injected for testing; it defaults to the real
    ``yfinance`` module.
    """

    QUOTE_BATCH_SIZE = 50

    def __init__(self, yfinance_model=None) -> None:
        self._yf = yfinance_model

    def _yf_module(self):
        if self._yf is None:
            self._yf = _default_yfinance()
        return self._yf

    def _download(self, **kwargs) -> pd.DataFrame:
        module = self._yf_module()
        try:
            return module.download(**kwargs)
        except TypeError:
            if "multi_level_index" in kwargs:
                # Older yfinance without the ``multi_level_index`` option.
                kwargs.pop("multi_level_index")
                return module.download(**kwargs)
            raise

    # -- interface ----------------------------------------------------------
    def get_candles(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles from Yahoo, resampling 5m -> 10m when needed."""
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()

        yahoo_interval = INTERVAL_TO_YAHOO.get(interval)
        if yahoo_interval is None:
            logger.warning("yfinance provider has no interval mapping for %r", interval)
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        fetch_start = start
        if yahoo_interval in {"5m", "60m"}:
            earliest = date.today() - timedelta(days=_INTRADAY_MAX_DAYS)
            if fetch_start < earliest:
                fetch_start = earliest

        ticker = to_yahoo_symbol(symbol)
        try:
            df = self._download(
                tickers=ticker,
                start=fetch_start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                interval=yahoo_interval,
                auto_adjust=False,
                progress=False,
                threads=False,
                multi_level_index=False,
            )
        except Exception as exc:  # noqa: BLE001 - Yahoo outages must not crash a run
            logger.warning("yfinance candles failed for %s: %s", ticker, exc)
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        if df is None or df.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        frame = _clean_frame(df)
        if interval in {"10m", "10min", "10-minute"}:
            frame = _resample_10m(frame)
        return frame.reindex(columns=OHLCV_COLUMNS)

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Last-session quotes derived from daily bars (off-hours fallback)."""
        result: dict[str, Quote] = {}
        mapping: dict[str, str] = {}
        for symbol in symbols:
            mapping.setdefault(to_yahoo_symbol(symbol), normalize_symbol(symbol))

        for batch in chunked(sorted(mapping), self.QUOTE_BATCH_SIZE):
            try:
                df = self._download(
                    tickers=batch,
                    period="5d",
                    interval="1d",
                    auto_adjust=False,
                    progress=False,
                    group_by="ticker",
                    threads=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("yfinance quotes failed: %s", exc)
                continue
            if df is None or df.empty:
                continue
            for ticker in batch:
                frame = self._frame_for(df, ticker, len(batch))
                if frame is None:
                    continue
                frame = _clean_frame(frame).dropna(subset=["close"])
                if frame.empty:
                    continue
                last = frame.iloc[-1]
                previous = frame.iloc[-2] if len(frame) > 1 else None
                result[mapping[ticker]] = Quote(
                    symbol=mapping[ticker],
                    ltp=float(last["close"]),
                    open=_to_float(last.get("open")),
                    high=_to_float(last.get("high")),
                    low=_to_float(last.get("low")),
                    prev_close=_to_float(previous["close"]) if previous is not None else None,
                    volume=_to_float(last.get("volume")),
                )
        return result

    def health_check(self) -> bool:
        """Return True when Yahoo Finance responds with recent Nifty data."""
        try:
            df = self._download(
                tickers="^NSEI",
                period="5d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
                multi_level_index=False,
            )
            return df is not None and not df.empty
        except Exception as exc:  # noqa: BLE001 - network failures are expected
            logger.warning("yfinance health check failed: %s", exc)
            return False

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _frame_for(df: pd.DataFrame, ticker: str, batch_size: int) -> pd.DataFrame | None:
        """Extract one ticker's columns from a batched download frame."""
        if isinstance(df.columns, pd.MultiIndex):
            level0 = df.columns.get_level_values(0)
            level1 = df.columns.get_level_values(1)
            if ticker in level0:
                return df[ticker]
            if ticker in level1:
                return df.xs(ticker, axis=1, level=1)
            return None
        return df if batch_size == 1 else None
