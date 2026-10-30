"""Tests for indicator calculations (ATR, RSI, Supertrend, Bollinger, resample)."""

from __future__ import annotations

import pandas as pd

from app.strategies.indicators import (
    atr,
    bollinger,
    closed_candles,
    resample,
    rsi,
    sma,
    supertrend,
    true_range,
    volume_ratio,
)


def _df(closes, highs=None, lows=None, opens=None, volumes=None):
    n = len(closes)
    return pd.DataFrame(
        {
            "open": opens if opens is not None else [c - 1 for c in closes],
            "high": highs if highs is not None else [c + 1 for c in closes],
            "low": lows if lows is not None else [c - 2 for c in closes],
            "close": closes,
            "volume": volumes if volumes is not None else [100.0] * n,
        }
    )


def test_true_range_basic() -> None:
    df = _df([100.0, 102.0, 101.0], highs=[103.0, 104.0, 102.0], lows=[99.0, 100.0, 99.5])
    tr = true_range(df)
    # bar0: high-low = 4; bar1: max(4, |104-100|=4, |100-102|=2) = 4
    assert tr.iloc[0] == 4.0
    assert tr.iloc[1] == 4.0


def test_atr_positive_and_ordered() -> None:
    df = _df([float(i) for i in range(1, 30)])
    series = atr(df, 14)
    assert (series.dropna() > 0).all()
    assert series.iloc[-1] > 0


def test_rsi_bounds() -> None:
    df = _df([100.0 + (i % 7) - 3 for i in range(40)])
    values = rsi(df, 14).dropna()
    assert ((values >= 0) & (values <= 100)).all()


def test_rsi_strong_uptrend_near_100() -> None:
    df = _df([float(100 + i) for i in range(40)])
    values = rsi(df, 14).dropna()
    assert values.iloc[-1] > 90


def test_sma() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert sma(series, 2).iloc[-1] == 3.5


def test_resample_daily_to_weekly() -> None:
    idx = pd.date_range("2026-01-01", periods=10, freq="D", tz="Asia/Kolkata")
    df = pd.DataFrame(
        {
            "open": [100.0] * 10,
            "high": [101.0] * 10,
            "low": [99.0] * 10,
            "close": [100.5] * 10,
            "volume": [10.0] * 10,
        },
        index=idx,
    )
    weekly = resample(df, "W")
    assert len(weekly) >= 1
    assert weekly["close"].iloc[-1] == 100.5
    # Last (partial) week holds 6 of the 10 days -> summed volume 60.
    assert weekly["volume"].iloc[-1] == 60.0


def test_volume_ratio() -> None:
    volumes = [100.0] * 20 + [300.0]  # last is 3x the 20-bar average
    df = _df([100.0] * 21, volumes=volumes)
    assert volume_ratio(df, 20) >= 1.5


def test_bollinger_constant_series_has_zero_bandwidth() -> None:
    df = _df([100.0] * 30)
    bands = bollinger(df, 20, 2.0)
    assert bands["middle"].iloc[-1] == 100.0
    assert bands["bandwidth"].iloc[-1] == 0.0
    assert bands["upper"].iloc[-1] == bands["lower"].iloc[-1] == 100.0


def test_bollinger_bands_wrap_the_mean() -> None:
    closes = [100.0 + (i % 3) for i in range(30)]
    df = _df(closes)
    bands = bollinger(df, 20, 2.0)
    last = bands.iloc[-1]
    assert last["upper"] > last["middle"] > last["lower"]
    assert last["bandwidth"] > 0


def test_bollinger_warmup_is_nan() -> None:
    df = _df([100.0 + i for i in range(30)])
    bands = bollinger(df, 20, 2.0)
    assert pd.isna(bands["bandwidth"].iloc[10])
    assert not pd.isna(bands["bandwidth"].iloc[-1])


def test_supertrend_uptrend_is_bullish() -> None:
    closes = [100.0 + i for i in range(40)]
    result = supertrend(_df(closes), 10, 3.0)
    assert result["direction"].iloc[-1] == 1.0
    # In an uptrend the trailing stop sits below the price.
    assert result["supertrend"].iloc[-1] < closes[-1]


def test_supertrend_downtrend_is_bearish() -> None:
    closes = [140.0 - i for i in range(40)]
    result = supertrend(_df(closes), 10, 3.0)
    assert result["direction"].iloc[-1] == -1.0
    assert result["supertrend"].iloc[-1] > closes[-1]


def test_supertrend_flips_on_reversal() -> None:
    closes = [100.0 + i for i in range(30)] + [130.0 - 3 * i for i in range(1, 10)]
    result = supertrend(_df(closes), 10, 3.0)
    assert result["direction"].iloc[20] == 1.0
    assert result["direction"].iloc[-1] == -1.0


def test_supertrend_warmup_is_nan() -> None:
    closes = [100.0 + i for i in range(40)]
    result = supertrend(_df(closes), 10, 3.0)
    assert result["supertrend"].isna().iloc[:10].all()
    assert not pd.isna(result["supertrend"].iloc[-1])


def test_resample_supports_offset() -> None:
    idx = pd.date_range("2026-01-02 09:15", periods=12, freq="10min", tz="Asia/Kolkata")
    df = pd.DataFrame(
        {
            "open": [100.0] * 12,
            "high": [101.0] * 12,
            "low": [99.0] * 12,
            "close": [100.5] * 12,
            "volume": [10.0] * 12,
        },
        index=idx,
    )
    hourly = resample(df, "1h", offset="15min")
    # 09:15-10:15 and 10:15-11:15 bins.
    assert list(hourly.index) == [
        pd.Timestamp("2026-01-02 09:15", tz="Asia/Kolkata"),
        pd.Timestamp("2026-01-02 10:15", tz="Asia/Kolkata"),
    ]


def _candles(idx, n=None):
    n = n or len(idx)
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [10.0] * n,
        },
        index=idx,
    )


def test_closed_candles_drops_in_progress_bar() -> None:
    # Bar labelled 11:15 (11:15-11:25) is still in progress at 11:20.
    idx = pd.date_range("2026-10-15 10:55", periods=3, freq="10min", tz="Asia/Kolkata")
    now = pd.Timestamp("2026-10-15 11:20", tz="Asia/Kolkata")
    trimmed = closed_candles(_candles(idx), "10m", now)
    assert len(trimmed) == 2
    assert trimmed.index[-1] == idx[1]  # 11:05 bar closed at 11:15


def test_closed_candles_skips_auction_bars() -> None:
    # Auction window (15:15) and auction match (15:25) are not continuous
    # session candles; 15:05 is the last real bar of the day.
    idx = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-10-15 15:05", tz="Asia/Kolkata"),
            pd.Timestamp("2026-10-15 15:15", tz="Asia/Kolkata"),
            pd.Timestamp("2026-10-15 15:25", tz="Asia/Kolkata"),
        ]
    )
    now = pd.Timestamp("2026-10-15 15:32", tz="Asia/Kolkata")
    trimmed = closed_candles(_candles(idx), "10m", now)
    assert trimmed.index[-1] == idx[0]
    assert len(trimmed) == 1


def test_closed_candles_daily_drops_today_partial() -> None:
    idx = pd.date_range("2026-10-13", periods=3, freq="D", tz="Asia/Kolkata")
    now = pd.Timestamp("2026-10-15 11:00", tz="Asia/Kolkata")
    trimmed = closed_candles(_candles(idx), "daily", now)
    assert len(trimmed) == 2  # 13th and 14th only


def test_closed_candles_daily_after_session_close() -> None:
    idx = pd.date_range("2026-10-13", periods=3, freq="D", tz="Asia/Kolkata")
    now = pd.Timestamp("2026-10-15 15:45", tz="Asia/Kolkata")
    trimmed = closed_candles(_candles(idx), "daily", now)
    assert len(trimmed) == 3


def test_closed_candles_weekly_passthrough() -> None:
    idx = pd.date_range("2026-09-01", periods=6, freq="W", tz="Asia/Kolkata")
    df = _candles(idx)
    now = pd.Timestamp("2026-10-15 11:00", tz="Asia/Kolkata")
    assert closed_candles(df, "weekly", now) is df


def test_closed_candles_accepts_utc_now() -> None:
    idx = pd.date_range("2026-10-15 10:55", periods=3, freq="10min", tz="Asia/Kolkata")
    now = pd.Timestamp("2026-10-15 05:50", tz="UTC")  # 11:20 IST
    trimmed = closed_candles(_candles(idx), "10m", now)
    assert len(trimmed) == 2
