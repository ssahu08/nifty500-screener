"""Tests for the unified EOD compression / Guppy / RSI(9) strategy (Strategy 5)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.config import Settings
from app.core.market import next_trading_day
from app.data.provider import Quote
from app.strategies import compression as compression_module
from app.strategies.compression import (
    BAND_BUY_MIN,
    BOLLINGER_NUM_STD,
    BOLLINGER_PERIOD,
    BOX_BARS,
    GMMA_FAST_PERIODS,
    GMMA_SLOW_PERIODS,
    MAX_SIGNALS,
    MIN_BARS,
    EodCompressionBreakoutStrategy,
    _box_ignition,
    _Candidate,
    _closing_frame,
    _daily_context,
    _flat_coil,
    _midline_reclaim,
    _score,
    _short_breakdown,
    _snapshot,
    _staircase,
)
from app.strategies.indicators import closed_candles

IST = ZoneInfo("Asia/Kolkata")
#: Continuous NSE 10-minute bars per session (09:15..15:05 labels, ending 15:15).
BARS_PER_SESSION = 36
#: Fixed replay date for the synthetic fixtures.
SCAN_DATE = date(2025, 1, 8)
SCAN_NOW = datetime(2025, 1, 8, 15, 35, tzinfo=IST)


def _settings() -> Settings:
    return Settings(_env_file=None, database_path="data/test.db")


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


def _next_bar(t: datetime) -> datetime:
    """Next 10-minute label, rolling over to the next NSE session."""
    t += timedelta(minutes=10)
    if t.hour > 15 or (t.hour == 15 and t.minute > 5):
        t = (t + timedelta(days=1)).replace(hour=9, minute=15)
        while t.weekday() > 4:
            t += timedelta(days=1)
    return t


def flat_coil_10m(seed: int = 7) -> pd.DataFrame:
    """Two volatile sessions, then one tight rising session ending in a coil.

    The volatile phase inflates the rolling Bollinger-bandwidth median, while
    the last 15 bars form a sub-0.3% box above the 21-period mid-line.
    """
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    t = datetime(2025, 1, 6, 9, 15, tzinfo=IST)
    anchor = 100.0
    price = anchor

    # Two volatile sessions around the anchor -> high rolling median bandwidth.
    for _ in range(2 * BARS_PER_SESSION):
        o = price
        c = anchor * (1.0 + rng.normal(0.0, 0.006))
        h = max(o, c) * (1.0 + abs(rng.normal(0.0, 0.002)))
        low = min(o, c) * (1.0 - abs(rng.normal(0.0, 0.002)))
        rows.append((t, o, h, low, c, 1000.0))
        price = c
        t = _next_bar(t)

    # One tight, gently rising session; the last 15 bars are the coil box.
    price = anchor
    for i in range(BARS_PER_SESSION):
        target = anchor * (1.000 + 0.00012 * i)
        o = price
        c = target + rng.normal(0.0, 0.00015)
        h = max(o, c) * (1.0 + 0.0002)
        low = min(o, c) * (1.0 - 0.0002)
        if i == BARS_PER_SESSION - 1:
            o = target * 0.9998
            c = target
            h = target * 1.0001
            low = target * 0.9996
        rows.append((t, o, h, low, c, 500.0))
        price = c
        t = _next_bar(t)

    return _to_df(rows)


def short_breakdown_10m() -> pd.DataFrame:
    """The flat-coil series plus a high-volume 15:25 closing-auction break."""
    frame = flat_coil_10m()
    timestamp = pd.Timestamp(datetime(2025, 1, 8, 15, 25, tzinfo=IST))
    frame.loc[timestamp, ["open", "high", "low", "close", "volume"]] = [
        100.42,
        100.45,
        98.95,
        99.00,
        6000.0,
    ]
    return frame.sort_index()


def volatile_10m(seed: int = 5) -> pd.DataFrame:
    """A wide, trendless series with no afternoon compression box."""
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    t = datetime(2025, 1, 6, 9, 15, tzinfo=IST)
    price = 100.0
    for _ in range(3 * BARS_PER_SESSION):
        o = price
        c = max(1.0, price * (1.0 + rng.normal(0.0, 0.02)))
        h = max(o, c) * (1.0 + abs(rng.normal(0.0, 0.005)))
        low = min(o, c) * (1.0 - abs(rng.normal(0.0, 0.005)))
        rows.append((t, o, h, low, c, 1000.0))
        price = c
        t = _next_bar(t)
    return _to_df(rows)


def daily_uptrend(days: int = 260, tight_base: int = 6) -> pd.DataFrame:
    """Steady daily uptrend finishing in a tight base near the 52-week high."""
    idx = pd.bdate_range(end="2025-01-07", periods=days)
    rng = np.random.default_rng(11)
    closes = np.linspace(80.0, 100.5, days) + rng.normal(0.0, 0.4, days)
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


def _flat_snapshot():
    base = closed_candles(flat_coil_10m(), "10m", SCAN_NOW)
    snapshot = _snapshot(base)
    assert snapshot is not None
    return snapshot


# -- end-to-end scan --------------------------------------------------------
def test_scan_emits_buy_only_flat_coil() -> None:
    strategy = EodCompressionBreakoutStrategy()
    signals = strategy._scan(
        {"NSE:TEST-EQ": {"10m": flat_coil_10m(), "daily": daily_uptrend()}},
        _settings(),
        SCAN_NOW,
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.symbol == "NSE:TEST-EQ"
    assert signal.strategy_name == "EOD Compression Breakout"
    assert signal.price == float(flat_coil_10m()["close"].iloc[-1])

    meta = signal.metadata
    assert meta["band"] == "BUY"
    assert meta["score"] >= BAND_BUY_MIN
    assert meta["direction"] == "LONG"
    assert meta["setup_type"] == "FLAT_COIL"
    assert meta["timeframe"] == "10m"
    assert meta["compression_bars"] == BOX_BARS
    assert meta["trigger_bar"] == "15:05"
    assert meta["dual_bar"] is False
    assert meta["daily_data"] is True


def test_signal_timestamp_is_candle_not_scan_time() -> None:
    strategy = EodCompressionBreakoutStrategy()
    signal = strategy._scan(
        {"NSE:TEST-EQ": {"10m": flat_coil_10m(), "daily": daily_uptrend()}},
        _settings(),
        SCAN_NOW,
    )[0]

    candle_time = datetime(2025, 1, 8, 15, 5, tzinfo=IST)
    assert signal.timestamp == candle_time
    assert signal.timestamp != SCAN_NOW
    assert signal.metadata["candle_time"] == candle_time.isoformat()
    assert signal.metadata["expected_move_date"] == next_trading_day(SCAN_DATE).isoformat()


def test_score_components_add_up_and_band_is_buy() -> None:
    strategy = EodCompressionBreakoutStrategy()
    signal = strategy._scan(
        {"NSE:TEST-EQ": {"10m": flat_coil_10m(), "daily": daily_uptrend()}},
        _settings(),
        SCAN_NOW,
    )[0]
    meta = signal.metadata

    parts = (
        meta["score_compression"]
        + meta["score_trigger"]
        + meta["score_participation"]
        + meta["score_daily"]
        + meta["score_bonus"]
    )
    assert abs(parts - meta["score"]) < 0.2
    assert 0.0 <= meta["score_compression"] <= 32.0
    assert 0.0 <= meta["score_trigger"] <= 43.0
    assert 0.0 <= meta["score_participation"] <= 12.0
    assert 0.0 <= meta["score_daily"] <= 18.0
    assert meta["score_bonus"] == 0.0
    assert meta["band"] == "BUY"
    assert meta["score"] >= BAND_BUY_MIN


def test_long_execution_levels() -> None:
    strategy = EodCompressionBreakoutStrategy()
    signal = strategy._scan(
        {"NSE:TEST-EQ": {"10m": flat_coil_10m(), "daily": daily_uptrend()}},
        _settings(),
        SCAN_NOW,
    )[0]
    meta = signal.metadata

    assert meta["entry"] > signal.price > meta["stop"]
    assert meta["target_1"] > meta["entry"] > meta["stop"]
    assert meta["target_2"] > meta["target_1"]
    assert meta["limit_price"] > meta["entry"]
    assert meta["risk_pct"] >= 1.2


def test_short_breakdown_emits_short_at_15_25() -> None:
    strategy = EodCompressionBreakoutStrategy()
    signals = strategy._scan(
        {"NSE:SHORT-EQ": {"10m": short_breakdown_10m(), "daily": daily_uptrend()}},
        _settings(),
        SCAN_NOW,
    )

    assert len(signals) == 1
    signal = signals[0]
    meta = signal.metadata
    assert meta["band"] == "BUY"
    assert meta["score"] >= BAND_BUY_MIN
    assert meta["direction"] == "SHORT"
    assert meta["setup_type"] == "SHORT_BREAKDOWN"
    assert meta["trigger_bar"] == "15:25"
    assert meta["dual_bar"] is True
    assert signal.timestamp == datetime(2025, 1, 8, 15, 25, tzinfo=IST)
    assert signal.timestamp != SCAN_NOW

    assert meta["stop"] > meta["entry"]
    assert meta["target_1"] < meta["entry"]
    assert meta["target_2"] < meta["target_1"]
    assert meta["limit_price"] < meta["entry"]
    assert meta["risk_pct"] >= 1.2


def test_dual_bar_breakdown_invalidates_earlier_long_coil() -> None:
    """Before 15:35 the 15:25 auction bar is invisible; the coil stands."""
    strategy = EodCompressionBreakoutStrategy()
    data = {"NSE:SHORT-EQ": {"10m": short_breakdown_10m(), "daily": daily_uptrend()}}

    early = strategy._scan(data, _settings(), datetime(2025, 1, 8, 15, 30, tzinfo=IST))
    assert len(early) == 1
    assert early[0].metadata["direction"] == "LONG"
    assert early[0].metadata["setup_type"] == "FLAT_COIL"
    assert early[0].timestamp == datetime(2025, 1, 8, 15, 5, tzinfo=IST)
    assert early[0].metadata["dual_bar"] is False

    confirmed = strategy._scan(data, _settings(), SCAN_NOW)
    assert len(confirmed) == 1
    assert confirmed[0].metadata["direction"] == "SHORT"
    assert confirmed[0].metadata["setup_type"] == "SHORT_BREAKDOWN"
    assert confirmed[0].timestamp == datetime(2025, 1, 8, 15, 25, tzinfo=IST)


def test_closing_frame_waits_for_auction_bar_to_close() -> None:
    raw = short_breakdown_10m()
    base = closed_candles(raw, "10m", SCAN_NOW)

    assert _closing_frame(raw, base, datetime(2025, 1, 8, 15, 30, tzinfo=IST)) is None
    assert _closing_frame(raw, base, datetime(2025, 1, 8, 15, 34, tzinfo=IST)) is None

    closing = _closing_frame(raw, base, SCAN_NOW)
    assert closing is not None
    assert closing.index[-1] == pd.Timestamp(datetime(2025, 1, 8, 15, 25, tzinfo=IST))
    assert len(closing) == len(base) + 1


def test_top_15_cap_and_score_sort() -> None:
    strategy = EodCompressionBreakoutStrategy()
    frame = flat_coil_10m()
    daily = daily_uptrend()
    data = {
        f"NSE:S{i:02d}-EQ": {"10m": frame, "daily": daily}
        for i in range(MAX_SIGNALS + 5)
    }
    signals = strategy._scan(data, _settings(), SCAN_NOW)

    assert len(signals) == MAX_SIGNALS
    scores = [signal.metadata["score"] for signal in signals]
    assert scores == sorted(scores, reverse=True)
    assert all(signal.metadata["band"] == "BUY" for signal in signals)


def test_volatile_data_emits_nothing() -> None:
    strategy = EodCompressionBreakoutStrategy()
    signals = strategy._scan(
        {"NSE:VOL-EQ": {"10m": volatile_10m(), "daily": daily_uptrend()}},
        _settings(),
        SCAN_NOW,
    )
    assert signals == []


def test_short_history_emits_nothing() -> None:
    strategy = EodCompressionBreakoutStrategy()
    short_frame = flat_coil_10m().tail(MIN_BARS - 1)
    assert strategy._scan(
        {"NSE:X-EQ": {"10m": short_frame, "daily": daily_uptrend()}},
        _settings(),
        SCAN_NOW,
    ) == []


def test_weak_setup_below_buy_cutoff() -> None:
    """A marginal box scores below the BUY floor and must never be emitted."""
    snapshot = _flat_snapshot()
    weak = replace(
        snapshot,
        box_range_pct=0.009,
        squeeze=0.49,
        nozzle=0.34,
        thin_density=0.0,
    )
    candidate = _Candidate(
        setup_type="FLAT_COIL",
        direction="LONG",
        snapshot=weak,
        trigger_bar="15:05",
        box_high=weak.box_high,
        box_low=weak.box_low,
        box_range_pct=weak.box_range_pct,
        box_position=weak.box_position,
        squeeze=weak.squeeze,
        nozzle=weak.nozzle,
        thin_density=weak.thin_density,
    )
    score, _ = _score(candidate, None)
    assert score < BAND_BUY_MIN


def test_no_absolute_squeeze_free_pass() -> None:
    """The old absolute-bandwidth loophole and hourly path are gone."""
    assert not hasattr(compression_module, "ABS_SQUEEZE_MAX_BW")
    assert not hasattr(compression_module, "hourly_from_10m")
    assert BOLLINGER_PERIOD == 21
    assert BOLLINGER_NUM_STD == 2.0
    assert GMMA_FAST_PERIODS == (3, 5, 8, 10, 12, 15)
    assert GMMA_SLOW_PERIODS == (30, 35, 40, 45, 50, 60)


# -- archetype gates --------------------------------------------------------
def test_flat_coil_rejects_loose_or_impulsive_setups() -> None:
    snapshot = _flat_snapshot()
    assert _flat_coil(snapshot)
    assert not _flat_coil(replace(snapshot, squeeze=0.51))
    assert not _flat_coil(replace(snapshot, day_impulse=0.02))


def test_staircase_requires_impulse_and_rejects_1525_continuation() -> None:
    snapshot = _flat_snapshot()
    impulsive = replace(snapshot, day_impulse=0.02)
    assert _staircase(impulsive, None)
    # A 15:25 snapshot may only confirm a reclaim when 15:05 was below the mid.
    assert not _staircase(impulsive, snapshot)


def test_midline_reclaim_requires_prior_close_below_mid() -> None:
    snapshot = _flat_snapshot()
    below = replace(snapshot, last=snapshot.mid * 0.99)
    assert _midline_reclaim(snapshot, below)
    assert not _midline_reclaim(below, below)


def test_box_ignition_requires_volume_expansion() -> None:
    snapshot = _flat_snapshot()
    ignition = replace(snapshot, up_break=0.005, vol_ratio=2.0)
    assert _box_ignition(ignition)
    assert not _box_ignition(replace(ignition, vol_ratio=1.0))


def test_short_breakdown_rejects_weak_volume_and_no_break() -> None:
    raw = short_breakdown_10m()
    base = closed_candles(raw, "10m", SCAN_NOW)
    m05 = _snapshot(base)
    m25 = _snapshot(_closing_frame(raw, base, SCAN_NOW))
    assert m05 is not None and m25 is not None

    assert _short_breakdown(m25, m05)
    assert not _short_breakdown(replace(m25, vol_ratio=1.0), m05)
    assert not _short_breakdown(replace(m25, dn_break=0.0), m05)


# -- daily context ----------------------------------------------------------
def test_daily_context_drops_today_and_requires_enough_bars() -> None:
    daily = daily_uptrend()
    ctx = _daily_context(daily, SCAN_DATE)
    assert ctx is not None
    assert len(ctx) == 260
    assert all(day < SCAN_DATE for day in ctx.index.date)

    assert _daily_context(None, SCAN_DATE) is None
    assert _daily_context(daily.tail(30), SCAN_DATE) is None


def test_scan_handles_empty_and_missing_frames() -> None:
    strategy = EodCompressionBreakoutStrategy()
    assert strategy._scan({}, _settings(), SCAN_NOW) == []
    assert strategy._scan({"NSE:X-EQ": {}}, _settings(), SCAN_NOW) == []
    assert strategy._scan(
        {"NSE:X-EQ": {"10m": pd.DataFrame()}}, _settings(), SCAN_NOW
    ) == []


# -- shared candle helper ---------------------------------------------------
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
