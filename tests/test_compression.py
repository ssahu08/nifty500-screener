"""Tests for the EOD Compression Breakout strategy (Strategy 5, v3)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.config import Settings
from app.core.market import next_trading_day
from app.data.provider import Quote
from app.strategies.compression import (
    BAND_BUY_MIN,
    BAND_HOLD_MIN,
    BOX_BARS,
    C_MAX_POINTS,
    MTF_CONFLUENCE_BONUS,
    SQUEEZE_MAX_RATIO,
    EodCompressionBreakoutStrategy,
    _daily_context,
    hourly_from_10m,
)
from app.strategies.indicators import closed_candles

IST = ZoneInfo("Asia/Kolkata")
#: Continuous NSE 10-minute bars per session (09:15..15:05 labels, ending 15:15).
BARS_PER_SESSION = 36


def _settings() -> Settings:
    return Settings(_env_file=None, database_path="data/test.db")


def _next_day(t: datetime) -> datetime:
    t = (t + timedelta(days=1)).replace(hour=9, minute=15)
    while t.weekday() > 4:
        t += timedelta(days=1)
    return t


def _to_df(rows: list[tuple]) -> pd.DataFrame:
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
        },
        index=idx,
    )


def compression_10m(seed: int = 1, wide: bool = False) -> pd.DataFrame:
    """Deterministic 10-minute series: volatile phase, compression, ramp, doji.

    ``wide`` inflates all ranges so the box/squeeze gates can never pass.
    """
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    t = datetime(2025, 1, 6, 9, 15, tzinfo=IST)
    price = 100.0

    # Six volatile sessions (inflate the historical bandwidth).
    for s in range(6):
        for _ in range(BARS_PER_SESSION):
            drift = 0.002 if s % 2 == 0 else -0.001
            move = rng.normal(drift, 0.02 if wide else 0.006)
            o = price
            c = max(1.0, price * (1 + move))
            h = max(o, c) * (1 + abs(rng.normal(0, 0.002)))
            low = min(o, c) * (1 - abs(rng.normal(0, 0.002)))
            rows.append((t, o, h, low, c, 1000))
            price = c
            t += timedelta(minutes=10)
        t = _next_day(t)

    # Three full sessions of mean-reverting compression around an anchor.
    anchor = price
    for _ in range(3):
        for _ in range(BARS_PER_SESSION):
            o = price
            c = anchor * (1 + rng.normal(0, 0.01 if wide else 0.0003))
            h = max(o, c) * (1 + abs(rng.normal(0, 0.0004)))
            low = min(o, c) * (1 - abs(rng.normal(0, 0.0004)))
            rows.append((t, o, h, low, c, 300))
            price = c
            t += timedelta(minutes=10)
        t = _next_day(t)

    # Final session: flat, then a gentle ramp, then the closing near-doji.
    # In ``wide`` mode even the closing stretch is volatile: nothing is coiled.
    for _ in range(BARS_PER_SESSION - 11):
        o = price
        c = anchor * (1 + rng.normal(0, 0.01 if wide else 0.0003))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.0004)))
        low = min(o, c) * (1 - abs(rng.normal(0, 0.0004)))
        rows.append((t, o, h, low, c, 300))
        price = c
        t += timedelta(minutes=10)
    for _ in range(10):
        o = price
        c = price * (1 + (0.002 if wide else 0.0003))
        rows.append((t, o, c * (1 + 0.0005), o * (1 - 0.0004), c, 300))
        price = c
        t += timedelta(minutes=10)
    o = price
    c = price * (1 + 0.00001)
    rows.append((t, o, price * (1 + 0.0004), price * (1 - 0.0004), c, 300))

    return _to_df(rows)


def compression_1h(seed: int = 2) -> pd.DataFrame:
    """Deterministic hourly series: volatility, tight coil, gentle drift up."""
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    t = datetime(2025, 1, 6, 9, 15, tzinfo=IST)
    price = 100.0

    def roll(t: datetime) -> datetime:
        t += timedelta(hours=1)
        while t.hour >= 16 or t.hour < 9:
            t = (t + timedelta(days=1)).replace(hour=9, minute=15)
            while t.weekday() > 4:
                t += timedelta(days=1)
        return t

    for _ in range(40):
        o = price
        c = price * (1 + rng.normal(0.001, 0.009))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.004)))
        low = min(o, c) * (1 - abs(rng.normal(0, 0.004)))
        rows.append((t, o, h, low, c, 1000))
        price = c
        t = roll(t)

    anchor = price
    for _ in range(30):
        o = price
        c = anchor * (1 + rng.normal(0, 0.0003))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.0004)))
        low = min(o, c) * (1 - abs(rng.normal(0, 0.0004)))
        rows.append((t, o, h, low, c, 300))
        price = c
        t = roll(t)

    for _ in range(15):
        o = price
        c = price * 1.0002
        rows.append((t, o, c * 1.0003, o * 0.9998, c, 300))
        price = c
        t = roll(t)

    o = price
    c = price * 1.00001
    rows.append((t, o, price * 1.0004, price * 0.9996, c, 300))

    return _to_df(rows)


def daily_uptrend(days: int = 260, tight_base: int = 6) -> pd.DataFrame:
    """Steady daily uptrend finishing in a tight base near the 52-week high."""
    idx = pd.bdate_range("2025-01-06", periods=days)
    rng = np.random.default_rng(11)
    closes = np.linspace(80.0, 128.0, days) + rng.normal(0.0, 0.5, days)
    highs = closes * 1.011
    lows = closes * 0.989
    for i in range(days - tight_base, days):
        highs[i] = closes[i] * 1.0008
        lows[i] = closes[i] * 0.9992
    volumes = np.full(days, 2_000_000.0)
    return _to_df(
        [
            (idx[i], closes[i], highs[i], lows[i], closes[i], volumes[i])
            for i in range(days)
        ]
    )


# -- scan -------------------------------------------------------------------
def test_scan_emits_scored_signal_with_metadata() -> None:
    strategy = EodCompressionBreakoutStrategy()
    df = compression_10m(seed=1)
    signals = strategy.scan(
        {"NSE:TEST-EQ": {"10m": df, "daily": daily_uptrend()}},
        _settings(),
    )
    assert signals, "expected at least one signal on compressed data"
    # v3 dedup: one signal per symbol (best of 10m / derived 1h).
    assert len(signals) == 1
    signal = signals[0]

    assert signal.symbol == "NSE:TEST-EQ"
    assert signal.strategy_name == "EOD Compression Breakout"
    assert signal.price == float(df["close"].iloc[-1])

    meta = signal.metadata
    assert meta["timeframe"] in {"10m", "1h"}
    assert meta["setup_type"] in {"EOD_COIL", "BOX_IGNITION", "SPRING_RECLAIM"}
    assert meta["compression_bars"] == BOX_BARS[meta["timeframe"]]
    assert meta["expected_move_date"] == next_trading_day(datetime.now(IST).date()).isoformat()
    assert 0.0 <= meta["score"] <= 100.0
    assert meta["band"] in {"BUY", "HOLD", "SKIP"}
    assert meta["entry"] > signal.price > meta["stop"]
    assert meta["target_1"] > meta["entry"] > meta["stop"]
    assert meta["target_2"] > meta["target_1"]
    assert meta["box_range_pct"] <= 3.0
    assert meta["squeeze_ratio"] <= SQUEEZE_MAX_RATIO
    assert meta["daily_data"] is True
    assert meta["mtf_confluence"] is True  # both timeframes fired on this series
    for key in ("rsi9", "par20", "wick_ratio", "base_days", "turnover_cr", "dist_52w_pct",
                "vol_mult", "score_bonus"):
        assert key in meta


def test_score_components_add_up_and_bands_match() -> None:
    strategy = EodCompressionBreakoutStrategy()
    signals = strategy.scan(
        {"NSE:TEST-EQ": {"10m": compression_10m(seed=3), "daily": daily_uptrend()}},
        _settings(),
    )
    assert signals
    for signal in signals:
        meta = signal.metadata
        parts = [
            meta["score_compression"],
            meta["score_absorption"],
            meta["score_timing"],
            meta["score_daily"],
            meta.get("score_bonus", 0.0),
        ]
        assert 0.0 <= parts[0] <= 24.0
        assert 0.0 <= parts[1] <= 26.0
        assert 0.0 <= parts[2] <= C_MAX_POINTS
        assert 0.0 <= parts[3] <= 24.0
        assert 0.0 <= parts[4] <= MTF_CONFLUENCE_BONUS
        assert abs(sum(parts) - meta["score"]) < 0.3
        expected = (
            "BUY" if meta["score"] >= BAND_BUY_MIN
            else "HOLD" if meta["score"] >= BAND_HOLD_MIN
            else "SKIP"
        )
        assert meta["band"] == expected


def test_signals_sorted_by_score_desc() -> None:
    strategy = EodCompressionBreakoutStrategy()
    daily = daily_uptrend()
    signals = strategy.scan(
        {
            "NSE:A-EQ": {"10m": compression_10m(seed=1), "daily": daily},
            "NSE:B-EQ": {"10m": compression_10m(seed=2), "daily": daily},
        },
        _settings(),
    )
    assert len(signals) >= 2
    scores = [s.metadata["score"] for s in signals]
    assert scores == sorted(scores, reverse=True)


def test_scan_is_robust_across_seeds() -> None:
    strategy = EodCompressionBreakoutStrategy()
    for seed in (1, 2, 3, 4, 5):
        signals = strategy.scan(
            {"NSE:TEST-EQ": {"10m": compression_10m(seed=seed, wide=False)}},
            _settings(),
        )
        assert len(signals) == 1, f"seed {seed}"  # dedup: one signal per symbol


def test_no_signal_when_range_is_wide() -> None:
    strategy = EodCompressionBreakoutStrategy()
    signals = strategy.scan({"NSE:TEST-EQ": {"10m": compression_10m(wide=True)}}, _settings())
    assert signals == []


def test_scan_handles_empty_and_missing_frames() -> None:
    strategy = EodCompressionBreakoutStrategy()
    assert strategy.scan({}, _settings()) == []
    assert strategy.scan({"NSE:X-EQ": {}}, _settings()) == []
    assert strategy.scan({"NSE:X-EQ": {"10m": pd.DataFrame()}}, _settings()) == []


def test_mtf_dedup_emits_single_signal_with_tracked_bonus() -> None:
    """Both 10m and derived 1h fire -> one signal, +5 tracked as score_bonus."""
    strategy = EodCompressionBreakoutStrategy()
    signals = strategy.scan(
        {"NSE:TEST-EQ": {"10m": compression_10m(seed=1), "daily": daily_uptrend()}},
        _settings(),
    )
    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["mtf_confluence"] is True
    assert meta["score_bonus"] == MTF_CONFLUENCE_BONUS
    # The bonus is attributed, so components still add up to the total.
    parts = sum(
        meta[k]
        for k in (
            "score_compression",
            "score_absorption",
            "score_timing",
            "score_daily",
        )
    )
    assert abs(parts + meta["score_bonus"] - meta["score"]) < 0.3
    expected = (
        "BUY" if meta["score"] >= BAND_BUY_MIN
        else "HOLD" if meta["score"] >= BAND_HOLD_MIN
        else "SKIP"
    )
    assert meta["band"] == expected


def test_single_timeframe_has_no_bonus(monkeypatch) -> None:
    """When the hourly path is unavailable there is no dedup and no bonus."""
    import app.strategies.compression as compression_module

    monkeypatch.setattr(compression_module, "hourly_from_10m", lambda df: None)
    strategy = EodCompressionBreakoutStrategy()
    signals = strategy.scan({"NSE:TEST-EQ": {"10m": compression_10m(seed=1)}}, _settings())
    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["mtf_confluence"] is False
    assert meta["score_bonus"] == 0.0


# -- daily context ----------------------------------------------------------
def test_daily_context_boosts_score() -> None:
    strategy = EodCompressionBreakoutStrategy()
    df = compression_10m(seed=3)
    with_daily = strategy.scan({"NSE:A-EQ": {"10m": df, "daily": daily_uptrend()}}, _settings())
    without = strategy.scan({"NSE:A-EQ": {"10m": df}}, _settings())
    ten_with = with_daily[0]
    ten_without = without[0]

    assert ten_with.metadata["daily_data"] is True
    assert ten_without.metadata["daily_data"] is False
    assert ten_without.metadata["score_daily"] == 0.0
    assert ten_with.metadata["score_daily"] > 12.0
    assert ten_with.metadata["score"] > ten_without.metadata["score"]


def test_daily_context_drops_today_partial_bar() -> None:
    today = date.today()
    business = pd.bdate_range(end=pd.Timestamp(today) - pd.Timedelta(days=1), periods=70)
    idx = business.append(pd.DatetimeIndex([pd.Timestamp(today)]))
    closes = np.linspace(90.0, 100.0, len(idx))
    frame = _to_df([(idx[i], closes[i], closes[i] * 1.01, closes[i] * 0.99, closes[i], 1e6)
                    for i in range(len(idx))])

    ctx = _daily_context(frame, today)
    assert ctx is not None
    assert len(ctx) == 70
    assert all(d < today for d in ctx.index.date)
    assert _daily_context(None, today) is None
    assert _daily_context(frame.iloc[:30], today) is None


# -- evaluation and helpers -------------------------------------------------
def test_evaluate_1h_timeframe_fires() -> None:
    strategy = EodCompressionBreakoutStrategy()
    hourly = compression_1h(seed=2)
    signal = strategy._evaluate(
        "NSE:X-EQ", "1h", hourly, None, datetime.now(IST), "2026-01-02"
    )
    assert signal is not None
    assert signal.metadata["timeframe"] == "1h"
    assert signal.metadata["compression_bars"] == 3


def test_closed_candles_excludes_auction_bars() -> None:
    now = datetime(2026, 1, 2, 15, 20, tzinfo=IST)
    starts = [
        datetime(2026, 1, 2, 15, 5, tzinfo=IST),  # continuous, ends 15:15 -> keep
        datetime(2026, 1, 2, 15, 15, tzinfo=IST),  # auction window -> drop
    ]
    df = pd.DataFrame(
        {
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [1.0, 1.0],
        },
        index=pd.DatetimeIndex(starts),
    )
    trimmed = closed_candles(df, "10m", now)
    assert len(trimmed) == 1
    assert trimmed.index[0] == starts[0]


def test_hourly_from_10m_uses_full_nse_hours_only() -> None:
    df = compression_10m(seed=1)
    hourly = hourly_from_10m(df)
    assert hourly is not None and not hourly.empty
    counts = df.resample("1h", offset="15min")["close"].count()
    assert (counts.reindex(hourly.index) == 6).all()
    # Bins are NSE-aligned (09:15, 10:15, ...) and never include the close stub.
    assert set(hourly.index.minute) == {15}
    assert hourly.index.max().time() == datetime(2026, 1, 1, 14, 15).time()


def test_hourly_from_10m_returns_none_for_empty() -> None:
    assert hourly_from_10m(pd.DataFrame()) is None


# -- prefilter --------------------------------------------------------------
def test_prefilter_keeps_tight_and_missing_symbols(monkeypatch) -> None:
    strategy = EodCompressionBreakoutStrategy()
    monkeypatch.setattr(
        strategy, "get_universe", lambda settings: ["AAA", "BBB", "CCC"]
    )
    quotes = {
        "NSE:AAA-EQ": Quote("NSE:AAA-EQ", ltp=100.0, high=101.0, low=99.5),  # 1.5%
        "NSE:BBB-EQ": Quote("NSE:BBB-EQ", ltp=100.0, high=105.0, low=97.0),  # 8%
        # CCC has no quote -> kept so the candle pass decides.
    }
    kept = strategy.prefilter(quotes, _settings())
    assert kept == ["AAA", "CCC"]


def test_prefilter_uses_normalised_symbols(monkeypatch) -> None:
    strategy = EodCompressionBreakoutStrategy()
    monkeypatch.setattr(strategy, "get_universe", lambda settings: ["SBIN"])
    quotes = {"NSE:SBIN-EQ": Quote("NSE:SBIN-EQ", ltp=800.0, high=804.0, low=798.0)}
    assert strategy.prefilter(quotes, _settings()) == ["SBIN"]
