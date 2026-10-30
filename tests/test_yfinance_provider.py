"""Tests for the yfinance off-hours market-data provider (hermetic: fake yfinance)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.data.provider import OHLCV_COLUMNS
from app.data.yfinance_provider import YFinanceProvider, to_yahoo_symbol

IST = ZoneInfo("Asia/Kolkata")


class _FakeYF:
    """Minimal stand-in for the yfinance module."""

    def __init__(self, frames=None, batch_frame=None, error=None) -> None:
        self.frames = frames or {}
        self.batch_frame = batch_frame
        self.error = error
        self.calls: list[dict] = []

    def download(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        tickers = kwargs.get("tickers")
        if isinstance(tickers, list):
            return self.batch_frame if self.batch_frame is not None else pd.DataFrame()
        return self.frames.get(tickers, pd.DataFrame())


def _intraday_frame() -> pd.DataFrame:
    """5-minute bars in exchange tz (as yfinance returns them)."""
    times = pd.DatetimeIndex(
        [
            datetime(2026, 10, 16, 9, 15, tzinfo=IST),
            datetime(2026, 10, 16, 9, 20, tzinfo=IST),
            datetime(2026, 10, 16, 9, 25, tzinfo=IST),
            datetime(2026, 10, 16, 9, 30, tzinfo=IST),
            datetime(2026, 10, 16, 15, 25, tzinfo=IST),
        ]
    ).tz_convert("America/New_York")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0, 103.0, 110.0],
            "High": [101.0, 102.0, 103.0, 104.0, 111.0],
            "Low": [99.0, 100.0, 101.0, 102.0, 109.0],
            "Close": [100.5, 101.5, 102.5, 103.5, 110.5],
            "Volume": [100.0, 200.0, 300.0, 400.0, 500.0],
        },
        index=times,
    )


def _daily_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex(["2026-10-14", "2026-10-15", "2026-10-16"])
    return pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [11.0, 12.0, 13.0],
            "Low": [9.0, 10.0, 11.0],
            "Close": [10.5, 11.5, 12.5],
            "Volume": [1000.0, 1100.0, 1200.0],
        },
        index=index,
    )


def _quotes_frame() -> pd.DataFrame:
    columns = pd.MultiIndex.from_product(
        [["SBIN.NS", "TCS.NS"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    index = pd.DatetimeIndex(["2026-10-14", "2026-10-15", "2026-10-16"])
    rows = [
        [600.0, 610.0, 590.0, 605.0, 100.0, 4000.0, 4050.0, 3950.0, 4020.0, 200.0],
        [605.0, 615.0, 600.0, 612.0, 110.0, 4020.0, 4080.0, 4000.0, 4060.0, 210.0],
        [612.0, 620.0, 608.0, 618.0, 120.0, 4060.0, 4100.0, 4040.0, 4090.0, 220.0],
    ]
    return pd.DataFrame(rows, index=index, columns=columns)


def test_to_yahoo_symbol_mapping() -> None:
    assert to_yahoo_symbol("NSE:SBIN-EQ") == "SBIN.NS"
    assert to_yahoo_symbol("SBIN") == "SBIN.NS"
    assert to_yahoo_symbol("NSE:NIFTY 50-EQ") == "^NSEI"
    assert to_yahoo_symbol("BSE:ABC-EQ") == "BSE:ABC-EQ"


def test_10m_candles_resampled_to_session_grid() -> None:
    provider = YFinanceProvider(yfinance_model=_FakeYF(frames={"SBIN.NS": _intraday_frame()}))
    df = provider.get_candles("NSE:SBIN-EQ", "10m", date(2026, 10, 16), date(2026, 10, 16))

    assert list(df.columns) == OHLCV_COLUMNS
    assert [f"{t.hour:02d}:{t.minute:02d}" for t in df.index] == ["09:15", "09:25", "15:25"]
    assert str(df.index[0].tz) == "Asia/Kolkata"

    first = df.iloc[0]
    assert first["open"] == 100.0  # first of 09:15 + 09:20 bars
    assert first["high"] == 102.0  # max
    assert first["low"] == 99.0  # min
    assert first["close"] == 101.5  # last
    assert first["volume"] == 300.0  # sum

    # The lone auction-window bar still resamples, later dropped by closed_candles.
    assert df.iloc[2]["open"] == 110.0


def test_daily_candles_localized_to_ist_midnight() -> None:
    provider = YFinanceProvider(yfinance_model=_FakeYF(frames={"SBIN.NS": _daily_frame()}))
    df = provider.get_candles("NSE:SBIN-EQ", "daily", date(2026, 10, 14), date(2026, 10, 16))

    assert list(df.columns) == OHLCV_COLUMNS
    assert len(df) == 3
    assert str(df.index[0].tz) == "Asia/Kolkata"
    assert (df.index.hour == 0).all()
    assert df.iloc[-1]["close"] == 12.5


def test_unknown_interval_returns_empty_frame() -> None:
    provider = YFinanceProvider(yfinance_model=_FakeYF())
    df = provider.get_candles("NSE:SBIN-EQ", "3m", date(2026, 10, 16), date(2026, 10, 16))
    assert df.empty
    assert list(df.columns) == OHLCV_COLUMNS


def test_intraday_start_is_clamped_to_yahoo_window() -> None:
    fake = _FakeYF(frames={"SBIN.NS": _intraday_frame()})
    provider = YFinanceProvider(yfinance_model=fake)
    provider.get_candles("NSE:SBIN-EQ", "10m", date.today() - timedelta(days=120), date.today())

    fetch_start = fake.calls[0]["start"]
    assert fetch_start >= (date.today() - timedelta(days=60)).isoformat()


def test_provider_failure_returns_empty_frame() -> None:
    provider = YFinanceProvider(yfinance_model=_FakeYF(error=RuntimeError("yahoo down")))
    df = provider.get_candles("NSE:SBIN-EQ", "daily", date(2026, 10, 14), date(2026, 10, 16))
    assert df.empty
    assert list(df.columns) == OHLCV_COLUMNS


def test_get_quotes_from_daily_bars() -> None:
    provider = YFinanceProvider(yfinance_model=_FakeYF(batch_frame=_quotes_frame()))
    quotes = provider.get_quotes(["NSE:SBIN-EQ", "TCS"])

    assert set(quotes) == {"NSE:SBIN-EQ", "NSE:TCS-EQ"}
    sbin = quotes["NSE:SBIN-EQ"]
    assert sbin.ltp == 618.0
    assert sbin.prev_close == 612.0
    assert sbin.open == 612.0
    assert sbin.volume == 120.0
    assert quotes["NSE:TCS-EQ"].ltp == 4090.0


def test_get_quotes_skips_unknown_tickers() -> None:
    provider = YFinanceProvider(yfinance_model=_FakeYF(batch_frame=_quotes_frame()))
    quotes = provider.get_quotes(["NSE:UNKNOWN-EQ"])
    assert quotes == {}


def test_health_check_reflects_yahoo_availability() -> None:
    ok = YFinanceProvider(yfinance_model=_FakeYF(frames={"^NSEI": _daily_frame()}))
    assert ok.health_check() is True
    down = YFinanceProvider(yfinance_model=_FakeYF(error=RuntimeError("timeout")))
    assert down.health_check() is False
