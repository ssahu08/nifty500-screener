"""Groww Trade API market-data provider for off-hours use.

FYERS stays the live NSE source; this provider serves after-market-closed
scans and development so broker API quota is only spent when it is actually
needed. Requires a Groww Trade API access token (``GROWW_API_KEY``). Historical
data is a paid Groww add-on (~₹499/month early bird); orders remain free.

Interval support:

* ``daily`` / ``1d`` - native Yahoo-style daily bars (1080 days max per call).
* ``1h`` / ``60m``  - native hourly bars (150 days max per call).
* ``10m``           - native 10-minute bars (30 days max per request; wider
  ranges are split into multiple calls and merged).

References: https://groww.in/trade-api/docs/python-sdk/historical-data
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from app.core.logging import get_logger
from app.data.provider import (
    OHLCV_COLUMNS,
    MarketDataProvider,
    Quote,
    chunked,
    normalize_symbol,
)

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

#: Candle timestamp comes back as epoch seconds; Groww's docs do not state the
#: timezone. Treat as UTC and convert to IST - verify against the first live
#: response and flip ``_epoch_to_ist`` if bars land 5:30 off.
_EPOCH_IS_UTC = True

#: interval_in_minutes -> max days per request.
_MAX_DAYS = {1: 7, 5: 15, 10: 30, 60: 150, 240: 365, 1440: 1080, 10080: None}

#: This provider's interval name -> Groww interval_in_minutes.
INTERVAL_TO_GROWW = {
    "10m": 10,
    "10min": 10,
    "10-minute": 10,
    "60m": 60,
    "1h": 60,
    "1hour": 60,
    "daily": 1440,
    "1d": 1440,
    "day": 1440,
}

#: Session bounds used to clamp intraday request windows.
_SESSION_START = "09:15:00"
_SESSION_END = "15:30:00"


def to_groww_params(symbol: str) -> tuple[str, str, str]:
    """Map an FYERS-style symbol (``NSE:SBIN-EQ``) to Groww exchange/segment/symbol."""
    norm = normalize_symbol(symbol)
    if norm in {"NSE:NIFTY 50-EQ", "NSE:NIFTY50-EQ", "NSE:NIFTY 50-INDEX"}:
        return "NSE", "CASH", "NIFTY"
    if norm.startswith("NSE:") and norm.endswith("-EQ"):
        return "NSE", "CASH", norm[4:-3]
    raise ValueError(f"unsupported symbol for Groww: {symbol}")


def _epoch_to_ist(series: pd.Series) -> pd.DatetimeIndex:
    stamps = pd.to_datetime(series, unit="s", utc=True)
    return stamps.dt.tz_convert(IST)


def _to_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iter_ranges(start: date, end: date, max_days: int | None):
    """Yield (chunk_start, chunk_end) date pairs covering ``[start, end]``."""
    if max_days is None:
        yield start, end
        return
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=max_days - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


class GrowwProvider(MarketDataProvider):
    """Market-data provider backed by the Groww Trade API."""

    QUOTE_BATCH_SIZE = 50

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or ""
        self.base_url = (base_url or "https://api.groww.in").rstrip("/")
        self.session = session or requests.Session()

    # -- interface ----------------------------------------------------------
    def get_candles(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles from Groww, chunking when a range is too wide."""
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()

        minutes = INTERVAL_TO_GROWW.get(interval)
        if minutes is None:
            logger.warning("Groww provider has no interval mapping for %r", interval)
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        exchange, segment, trading_symbol = to_groww_params(symbol)
        frames: list[pd.DataFrame] = []
        for chunk_start, chunk_end in _iter_ranges(start, end, _MAX_DAYS.get(minutes)):
            frame = self._fetch_candles(
                exchange, segment, trading_symbol, minutes, chunk_start, chunk_end
            )
            if frame is not None and not frame.empty:
                frames.append(frame)

        if not frames:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        out = pd.concat(frames)
        out = out[~out.index.duplicated(keep="last")].sort_index()
        return out.reindex(columns=OHLCV_COLUMNS)

    def _fetch_candles(
        self,
        exchange: str,
        segment: str,
        trading_symbol: str,
        minutes: int,
        start: date,
        end: date,
    ) -> pd.DataFrame | None:
        if minutes in {10, 60}:
            start_time = f"{start} {_SESSION_START}"
            end_time = f"{end} {_SESSION_END}"
        else:
            start_time = f"{start} 00:00:00"
            end_time = f"{end} 23:59:59"

        params = {
            "exchange": exchange,
            "segment": segment,
            "trading_symbol": trading_symbol,
            "start_time": start_time,
            "end_time": end_time,
            "interval_in_minutes": minutes,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-API-VERSION": "1.0",
            "Accept": "application/json",
        }
        try:
            response = self.session.get(
                f"{self.base_url}/v1/historical/candle/range",
                params=params,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - network failures are expected
            logger.warning("Groww candles failed for %s: %s", trading_symbol, exc)
            return None

        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            logger.warning(
                "Groww candles rejected for %s: %s", trading_symbol, payload
            )
            return None

        candles = payload.get("candles") if isinstance(payload, dict) else None
        if not candles:
            return None

        records = [
            {
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "time": c[0],
            }
            for c in candles
            if len(c) >= 6
        ]
        if not records:
            return None
        df = pd.DataFrame(records)
        df["time"] = _epoch_to_ist(pd.Series(df["time"]))
        df = df.set_index("time").sort_index()
        keep = [c for c in OHLCV_COLUMNS if c in df.columns]
        return df[keep]

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Last-session quotes derived from daily bars (off-hours fallback)."""
        result: dict[str, Quote] = {}
        for batch in chunked(symbols, self.QUOTE_BATCH_SIZE):
            for symbol in batch:
                try:
                    exchange, segment, trading_symbol = to_groww_params(symbol)
                except ValueError:
                    continue
                end = date.today()
                df = self._fetch_candles(
                    exchange, segment, trading_symbol, 1440, end - timedelta(days=5), end
                )
                if df is None or df.empty or "close" not in df.columns:
                    continue
                frame = df.dropna(subset=["close"])
                if frame.empty:
                    continue
                last = frame.iloc[-1]
                previous = frame.iloc[-2] if len(frame) > 1 else None
                result[symbol] = Quote(
                    symbol=symbol,
                    ltp=float(last["close"]),
                    open=_to_float(last.get("open")),
                    high=_to_float(last.get("high")),
                    low=_to_float(last.get("low")),
                    prev_close=_to_float(previous["close"]) if previous is not None else None,
                    volume=_to_float(last.get("volume")),
                )
        return result

    def health_check(self) -> bool:
        """Return True when Groww responds with recent Nifty daily data."""
        try:
            df = self._fetch_candles(
                "NSE", "CASH", "NIFTY", 1440, date.today() - timedelta(days=3), date.today()
            )
            return df is not None and not df.empty
        except Exception as exc:  # noqa: BLE001 - network failures are expected
            logger.warning("Groww health check failed: %s", exc)
            return False
