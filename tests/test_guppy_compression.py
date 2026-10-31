"""Tests for the Guppy Ribbon + Bollinger Compression strategy (v3.2)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.data.provider import Quote
from app.strategies.guppy_compression import (
    BAND_BUY_MIN,
    BAND_HOLD_MIN,
    C_MAX_POINTS,
    GMMA_FAST_PERIODS,
    GMMA_SLOW_PERIODS,
    GuppyCompressionStrategy,
    _compute_gmma_state,
    _daily_scores,
    hourly_from_10m,
)

IST = ZoneInfo("Asia/Kolkata")
BARS_PER_SESSION = 36


def _settings() -> Settings:
    return Settings(_env_file=None, database_path="data/test.db")


def _advance(t: datetime) -> datetime:
    """Next 10-minute label, rolling over to the next NSE session."""
    t += timedelta(minutes=10)
    if t.hour > 15 or (t.hour == 15 and t.minute > 5):
        t = (t + timedelta(days=1)).replace(hour=9, minute=15)
        while t.weekday() > 4:
            t += timedelta(days=1)
    return t


def _frame(rows: list[tuple]) -> pd.DataFrame:
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


def _flat(
    rows: list[tuple],
    t: datetime,
    price: float,
    n: int,
    *,
    volume: float = 300,
    jitter: float = 0.0001,
    drift: float = 0.0,
) -> tuple[datetime, float]:
    """Append ``n`` tight alternating bars; returns (next time, last close)."""
    for i in range(n):
        o = price
        c = price * (1.0 + drift + (jitter if i % 2 == 0 else -jitter))
        rows.append((t, o, max(o, c) * 1.0002, min(o, c) * 0.9998, c, volume))
        price = c
        t = _advance(t)
    return t, price


def _start() -> datetime:
    return datetime(2025, 1, 6, 9, 15, tzinfo=IST)


def eod_coil_10m() -> pd.DataFrame:
    """Flat afternoon pipe with no preceding impulse -> EOD_COIL."""
    rows: list[tuple] = []
    t, price = _start(), 100.0
    t, price = _flat(rows, t, price, 120)
    _flat(rows, t, price, 15, jitter=0.00005)
    return _frame(rows)


def staircase_10m() -> pd.DataFrame:
    """Morning +~8% impulse step followed by a tight high-level coil."""
    rows: list[tuple] = []
    t, price = _start(), 100.0
    t, price = _flat(rows, t, price, 120)
    for _ in range(20):
        o = price
        c = price * 1.004
        rows.append((t, o, c * 1.0004, o * 0.9996, c, 1000))
        price = c
        t = _advance(t)
    t, price = _flat(rows, t, price, 10)
    _flat(rows, t, price, 15, jitter=0.00005)
    return _frame(rows)


def box_ignition_10m() -> pd.DataFrame:
    """Tight box then a +2.5% volume breakout bar closing above the ceiling."""
    rows: list[tuple] = []
    t, price = _start(), 100.0
    t, price = _flat(rows, t, price, 120)
    t, price = _flat(rows, t, price, 15)
    o = price
    c = price * 1.025
    rows.append((t, o, c * 1.001, o * 0.999, c, 3000))
    return _frame(rows)


def short_dist_10m() -> pd.DataFrame:
    """Declining shelf: fast ribbon trapped under a falling slow ribbon."""
    rows: list[tuple] = []
    t, price = _start(), 100.0
    t, price = _flat(rows, t, price, 100)
    for _ in range(30):
        o = price
        c = price * 0.997
        rows.append((t, o, o * 1.0004, c * 0.9996, c, 1000))
        price = c
        t = _advance(t)
    t, price = _flat(rows, t, price, 20)
    _flat(rows, t, price, 15, jitter=0.00003, drift=-0.0003)
    return _frame(rows)


def short_breakdown_10m() -> pd.DataFrame:
    """Coiled distribution shelf then a -2.5% high-volume floor break."""
    rows: list[tuple] = []
    t, price = _start(), 100.0
    t, price = _flat(rows, t, price, 100)
    for _ in range(25):
        o = price
        c = price * 0.995
        rows.append((t, o, o * 1.0004, c * 0.9996, c, 1000))
        price = c
        t = _advance(t)
    t, price = _flat(rows, t, price, 10)
    t, price = _flat(rows, t, price, 15, jitter=0.00003)
    o = price
    c = price * 0.975
    rows.append((t, o, o * 1.0002, c * 0.999, c, 3500))
    return _frame(rows)


def daily_uptrend(days: int = 260, tight_base: int = 6) -> pd.DataFrame:
    """Steady daily uptrend finishing in a tight base near the 52-week high."""
    import numpy as np

    idx = pd.bdate_range("2025-01-06", periods=days)
    rng = np.random.default_rng(11)
    closes = np.linspace(80.0, 128.0, days) + rng.normal(0.0, 0.5, days)
    highs = closes * 1.011
    lows = closes * 0.989
    for i in range(days - tight_base, days):
        highs[i] = closes[i] * 1.0008
        lows[i] = closes[i] * 0.9992
    volumes = np.full(days, 2_000_000.0)
    return _frame(
        [
            (idx[i], closes[i], highs[i], lows[i], closes[i], volumes[i])
            for i in range(days)
        ]
    )


def daily_downtrend(days: int = 260) -> pd.DataFrame:
    """Steady daily downtrend finishing in a tight distribution shelf."""
    import numpy as np

    idx = pd.bdate_range("2025-01-06", periods=days)
    rng = np.random.default_rng(13)
    closes = np.linspace(140.0, 90.0, days) + rng.normal(0.0, 0.5, days)
    highs = closes * 1.008
    lows = closes * 0.992
    volumes = np.full(days, 2_000_000.0)
    return _frame(
        [
            (idx[i], closes[i], highs[i], lows[i], closes[i], volumes[i])
            for i in range(days)
        ]
    )


def _evaluate(df: pd.DataFrame, daily: pd.DataFrame | None = None):
    return GuppyCompressionStrategy()._evaluate(
        "NSE:TEST-EQ", "10m", df, daily, datetime.now(IST), "2026-01-02"
    )


# -- registry ---------------------------------------------------------------
def test_registry_contains_new_and_existing_strategies() -> None:
    from app.strategies.registry import get_registry

    registry = get_registry()
    assert "Guppy Ribbon Compression" in registry
    assert "EOD Compression Breakout" in registry
    assert len(registry) == 5


def test_guppy_is_manual_legacy() -> None:
    """The unified EOD strategy owns the 15:35 slot; Guppy must not auto-run."""
    meta = GuppyCompressionStrategy().metadata
    assert meta.schedule_type == "manual"
    assert meta.run_at is None


# -- setup classification ---------------------------------------------------
def test_eod_coil_without_staircase_step() -> None:
    signal = _evaluate(eod_coil_10m())
    assert signal is not None
    meta = signal.metadata
    assert meta["setup_type"] == "EOD_COIL"
    assert meta["direction"] == "LONG"
    assert abs(meta["morning_step_pct"]) < 1.2


def test_staircase_coil_after_impulse_leg() -> None:
    signal = _evaluate(staircase_10m())
    assert signal is not None
    meta = signal.metadata
    assert meta["setup_type"] == "STAIRCASE_COIL"
    assert meta["direction"] == "LONG"
    assert meta["morning_step_pct"] >= 1.2
    assert meta["gmma_fast_above_slow"] is True


def test_box_ignition_long_breakout() -> None:
    signal = _evaluate(box_ignition_10m())
    assert signal is not None
    meta = signal.metadata
    assert meta["setup_type"] == "BOX_IGNITION"
    assert meta["direction"] == "LONG"
    assert meta["vol_mult"] >= 1.5
    # BOX_IGNITION enters at market on the live breakout bar.
    assert meta["entry"] == round(signal.price, 2)


def test_short_distribution_coil() -> None:
    signal = _evaluate(short_dist_10m())
    assert signal is not None
    meta = signal.metadata
    assert meta["setup_type"] == "SHORT_DIST_COIL"
    assert meta["direction"] == "SHORT"
    assert meta["gmma_slow_slope_pct"] < 0


def test_short_breakdown_floor_break() -> None:
    signal = _evaluate(short_breakdown_10m())
    assert signal is not None
    meta = signal.metadata
    assert meta["setup_type"] == "SHORT_BREAKDOWN"
    assert meta["direction"] == "SHORT"
    assert meta["vol_mult"] >= 1.5


def test_no_signal_when_market_is_wide() -> None:
    import numpy as np

    rng = np.random.default_rng(7)
    rows: list[tuple] = []
    t, price = _start(), 100.0
    for _ in range(160):
        o = price
        c = price * (1 + rng.normal(0.0, 0.012))
        rows.append((t, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1000))
        price = c
        t = _advance(t)
    assert _evaluate(_frame(rows)) is None


# -- GMMA ribbon ------------------------------------------------------------
def test_gmma_periods_match_12_ema_reference() -> None:
    assert GMMA_FAST_PERIODS == (3, 5, 8, 10, 12, 15)
    assert GMMA_SLOW_PERIODS == (30, 35, 40, 45, 50, 60)


def test_gmma_state_is_tight_in_a_flat_coil() -> None:
    state = _compute_gmma_state(eod_coil_10m(), box_bars=15)
    assert state["fast_spread_pct"] < 0.005
    assert state["total_spread_pct"] < 0.01
    assert abs(state["slow_slope_pct"]) < 0.002


def test_gmma_state_detects_morning_step() -> None:
    flat = _compute_gmma_state(eod_coil_10m(), box_bars=15)
    stepped = _compute_gmma_state(staircase_10m(), box_bars=15)
    assert stepped["morning_step_pct"] > flat["morning_step_pct"]
    assert stepped["morning_step_pct"] >= 0.012


# -- scoring ----------------------------------------------------------------
def test_score_components_add_up_and_bands_match() -> None:
    for fixture in (
        eod_coil_10m(),
        staircase_10m(),
        box_ignition_10m(),
        short_dist_10m(),
        short_breakdown_10m(),
    ):
        signal = _evaluate(fixture, daily_uptrend())
        assert signal is not None
        meta = signal.metadata
        parts = [
            meta["score_compression"],
            meta["score_absorption"],
            meta["score_timing"],
            meta["score_daily"],
            meta.get("score_bonus", 0.0),
        ]
        assert 0.0 <= parts[0] <= 26.0
        assert 0.0 <= parts[1] <= 24.0
        assert 0.0 <= parts[2] <= C_MAX_POINTS
        assert 0.0 <= parts[3] <= 24.0
        assert 0.0 <= parts[4] <= 5.0
        assert abs(sum(parts) - meta["score"]) < 0.3
        expected = (
            "BUY" if meta["score"] >= BAND_BUY_MIN
            else "HOLD" if meta["score"] >= BAND_HOLD_MIN
            else "SKIP"
        )
        assert meta["band"] == expected


def test_daily_context_boosts_long_and_short_scores() -> None:
    long_signal = _evaluate(eod_coil_10m(), daily_uptrend())
    assert long_signal is not None
    assert long_signal.metadata["score_daily"] > 12.0

    short_points, _ = _daily_scores(
        daily_downtrend(), float(short_dist_10m()["close"].iloc[-1]), "SHORT"
    )
    assert short_points > 12.0


# -- risk metrics -----------------------------------------------------------
def test_long_risk_metrics_are_ordered_with_shakeout_buffer() -> None:
    signal = _evaluate(staircase_10m())
    assert signal is not None
    meta = signal.metadata
    assert meta["entry"] > signal.price > meta["stop"]
    assert meta["target_1"] > meta["entry"] > meta["stop"]
    assert meta["target_2"] > meta["target_1"]
    assert meta["stop"] <= meta["entry"] * (1 - 0.012) + 0.01
    assert meta["risk_pct"] >= 1.2
    assert meta["limit_price"] >= meta["entry"]


def test_short_risk_metrics_are_inverted() -> None:
    signal = _evaluate(short_breakdown_10m())
    assert signal is not None
    meta = signal.metadata
    # Live breakdown entries fill at market, so entry == the bar close (rounded).
    assert meta["entry"] <= signal.price + 0.01
    assert meta["entry"] < meta["stop"]
    assert meta["target_1"] < meta["entry"] < meta["stop"]
    assert meta["target_2"] < meta["target_1"]
    assert meta["stop"] >= meta["entry"] * (1 + 0.012) - 0.01
    assert meta["risk_pct"] >= 1.2
    assert meta["limit_price"] <= meta["entry"]


# -- metadata schema --------------------------------------------------------
def test_signal_metadata_schema_is_complete() -> None:
    signal = _evaluate(staircase_10m(), daily_uptrend())
    assert signal is not None
    meta = signal.metadata
    for key in (
        "timeframe",
        "direction",
        "setup_type",
        "score",
        "band",
        "score_compression",
        "score_absorption",
        "score_timing",
        "score_daily",
        "entry",
        "trigger_price",
        "limit_price",
        "stop",
        "target_1",
        "target_2",
        "risk_pct",
        "gmma_fast_above_slow",
        "gmma_fast_spread_pct",
        "gmma_total_spread_pct",
        "gmma_slow_slope_pct",
        "morning_step_pct",
        "rsi9",
        "rsi9_above_slow",
        "supertrend_dir",
        "above_vwap",
        "daily_data",
        "compression_bars",
        "expected_move_date",
    ):
        assert key in meta, key
    assert meta["compression_bars"] == 15
    assert meta["daily_data"] is True


# -- scan, dedup and prefilter ----------------------------------------------
def test_scan_dedups_timeframes_and_tracks_mtf_bonus() -> None:
    strategy = GuppyCompressionStrategy()
    signals = strategy.scan(
        {"NSE:TEST-EQ": {"10m": eod_coil_10m(), "daily": daily_uptrend()}},
        _settings(),
    )
    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["setup_type"] == "EOD_COIL"
    assert meta["mtf_confluence"] is True
    assert meta["score_bonus"] == 5.0
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


def test_scan_handles_empty_and_missing_frames() -> None:
    strategy = GuppyCompressionStrategy()
    assert strategy.scan({}, _settings()) == []
    assert strategy.scan({"NSE:X-EQ": {}}, _settings()) == []
    assert strategy.scan({"NSE:X-EQ": {"10m": pd.DataFrame()}}, _settings()) == []


def test_hourly_resample_is_nse_aligned() -> None:
    hourly = hourly_from_10m(eod_coil_10m())
    assert hourly is not None and not hourly.empty
    assert set(hourly.index.minute) == {15}


def test_prefilter_uses_65_percent_day_range(monkeypatch) -> None:
    strategy = GuppyCompressionStrategy()
    monkeypatch.setattr(
        strategy, "get_universe", lambda settings: ["AAA", "BBB", "CCC"]
    )
    quotes = {
        "NSE:AAA-EQ": Quote("NSE:AAA-EQ", ltp=100.0, high=103.0, low=97.0),  # 6% kept
        "NSE:BBB-EQ": Quote("NSE:BBB-EQ", ltp=100.0, high=105.0, low=97.0),  # 8% dropped
        # CCC has no quote -> kept so the candle pass decides.
    }
    assert strategy.prefilter(quotes, _settings()) == ["AAA", "CCC"]
