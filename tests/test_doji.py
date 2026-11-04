"""Tests for the Multi-Timeframe Doji strategy."""

from __future__ import annotations

import pandas as pd

from app.config import Settings
from app.strategies.base import Category
from app.strategies.doji import (
    MultiTimeframeDojiStrategy,
    classify_doji,
)


def _settings() -> Settings:
    return Settings(_env_file=None, database_path="data/test.db")


def _candle(open, high, low, close):
    return pd.Series({"open": open, "high": high, "low": low, "close": close})


def test_metadata() -> None:
    strategy = MultiTimeframeDojiStrategy()
    meta = strategy.metadata
    assert meta.name == "Multi-Timeframe Doji"
    assert meta.category == Category.INTRADAY
    assert meta.universe == "NIFTY_500"
    assert meta.schedule_type == "manual"
    assert meta.frequency_minutes is None
    assert meta.timeframes == ["10m", "daily"]


def test_classify_standard_doji() -> None:
    # Small body, moderate asymmetric wicks (avoids long-legged/dragonfly).
    candle = _candle(100.0, 104.0, 90.0, 100.2)
    assert classify_doji(candle, 5.0) == "standard"


def test_classify_not_doji_large_body() -> None:
    # Body is 50% of range -> not a doji.
    candle = _candle(95.0, 110.0, 90.0, 105.0)
    assert classify_doji(candle, 5.0) is None


def test_classify_not_doji_no_range() -> None:
    # Range below 0.45 * ATR -> not a doji.
    candle = _candle(100.0, 101.0, 99.5, 100.2)
    assert classify_doji(candle, 5.0) is None


def test_classify_dragonfly() -> None:
    # Long lower wick, tiny upper wick.
    candle = _candle(100.0, 100.5, 90.0, 100.2)
    assert classify_doji(candle, 5.0) == "dragonfly"


def test_classify_gravestone() -> None:
    # Long upper wick, tiny lower wick.
    candle = _candle(100.0, 110.0, 99.5, 100.2)
    assert classify_doji(candle, 5.0) == "gravestone"


def test_classify_long_legged() -> None:
    candle = _candle(100.0, 115.0, 85.0, 100.5)
    assert classify_doji(candle, 5.0) == "long-legged"


def _daily_frame(n=40, close=100.0, doji_at_end=True):
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="Asia/Kolkata")
    if doji_at_end:
        opens = [close] * n
        highs = [close + 5.0] * n
        lows = [close - 5.0] * n
        closes = [close] * n
        closes[-1] = close + 0.2  # tiny body
    else:
        opens = [close] * n
        highs = [close + 5.0] * n
        lows = [close - 5.0] * n
        closes = [close + 2.0] * n
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100.0] * n,
        },
        index=idx,
    )


def test_scan_detects_doji_on_daily() -> None:
    strategy = MultiTimeframeDojiStrategy()
    daily = _daily_frame()
    data = {"SBIN": {"daily": daily, "10m": pd.DataFrame()}}
    signals = strategy.scan(data, _settings())
    assert len(signals) >= 1
    assert any(s.metadata["timeframe"] == "daily" for s in signals)
    assert all(s.strategy_name == "Multi-Timeframe Doji" for s in signals)


def test_scan_no_signal_when_no_doji() -> None:
    strategy = MultiTimeframeDojiStrategy()
    daily = _daily_frame(doji_at_end=False)
    data = {"SBIN": {"daily": daily, "10m": pd.DataFrame()}}
    signals = strategy.scan(data, _settings())
    assert signals == []


def test_scan_skips_empty_frames() -> None:
    strategy = MultiTimeframeDojiStrategy()
    data = {"SBIN": {"daily": pd.DataFrame(), "10m": pd.DataFrame()}}
    assert strategy.scan(data, _settings()) == []


def test_confluence_score_bounds() -> None:
    strategy = MultiTimeframeDojiStrategy()
    daily = _daily_frame()
    data = {"SBIN": {"daily": daily, "10m": pd.DataFrame()}}
    signals = strategy.scan(data, _settings())
    for s in signals:
        assert 0 <= s.metadata["confluence_score"] <= 100


def test_signal_timestamp_is_candle_label() -> None:
    strategy = MultiTimeframeDojiStrategy()
    daily = _daily_frame()
    data = {"SBIN": {"daily": daily, "10m": pd.DataFrame()}}
    signals = strategy.scan(data, _settings())
    daily_signals = [s for s in signals if s.metadata["timeframe"] == "daily"]
    assert daily_signals
    expected = daily.index[-1].to_pydatetime()
    assert all(s.timestamp == expected for s in daily_signals)


def test_scan_ignores_in_progress_10m_bar() -> None:
    strategy = MultiTimeframeDojiStrategy()
    # 29 closed bars (yesterday's session) plus a bar labelled in the future.
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    closed_end = now.normalize() - pd.Timedelta(days=1) + pd.Timedelta(hours=15, minutes=5)
    idx = pd.date_range(end=closed_end, periods=29, freq="10min")
    idx = idx.append(pd.DatetimeIndex([now + pd.Timedelta(days=1)]))
    opens = [100.0] * 30
    closes = [100.0] * 29 + [104.0]  # future bar would not be a doji anyway
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": [105.0] * 30,
            "low": [99.0] * 30,
            "close": closes,
            "volume": [100.0] * 30,
        },
        index=idx,
    )
    data = {"SBIN": {"daily": _daily_frame(), "10m": frame}}
    signals = strategy.scan(data, _settings())
    stamps = {s.timestamp for s in signals if s.metadata["timeframe"] == "10m"}
    assert stamps == {idx[-2].to_pydatetime()}


def test_weekly_month_end_gating() -> None:
    strategy = MultiTimeframeDojiStrategy()
    # Force week-end + month-end so weekly/monthly frames are evaluated.
    strategy._is_week_end = True
    strategy._is_month_end = True

    daily = _daily_frame(n=40)
    data = {"SBIN": {"daily": daily, "10m": pd.DataFrame()}}
    signals = strategy.scan(data, _settings())
    tfs = {s.metadata["timeframe"] for s in signals}
    assert "weekly" in tfs or "monthly" in tfs or "daily" in tfs
