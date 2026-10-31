"""Strategy 5: unified EOD compression, Guppy ribbon and RSI(9) trigger.

This replaces the earlier v3 compression engine with a single, dual-bar EOD
strategy built around:

* a **21-period Bollinger middle band** (the reference / trigger line);
* the **12-EMA Guppy ribbon** (fast 3/5/8/10/12/15, slow 30/35/40/45/50/60);
* **RSI(9)** with EMA(3) vs SMA(21) confirmation.

The scan runs at **15:35 IST** and evaluates two snapshots of the closing
session: the **15:05** continuous bar and the **15:25** closing-auction print.
The flat 15:15 auction-window artifact is skipped. The 15:05 snapshot is used
for flat coils, staircase coils and box ignitions; the 15:25 snapshot is used
only for midline reclaims, short breakdowns and staircase reclaims where the
15:05 close was below the 21-MA.

Only actionable **BUY-band** signals (score >= 72) are emitted, at most 15 per
run, stamped with the candle's own timestamp rather than the scan time.

Archetypes:
  * ``STAIRCASE_COIL``  - morning impulse then a tight afternoon pipe holding
    the 21-MA / fast ribbon.
  * ``FLAT_COIL``       - tight flat pipe at/above the 21-MA with a rising
    ribbon and no morning impulse.
  * ``BOX_IGNITION``    - close above a prior coiled box on >= 1.5x volume.
  * ``MIDLINE_RECLAIM`` - 15:05 below the 21-MA, 15:25 reclaims it green.
  * ``SHORT_BREAKDOWN`` - 15:05 above the 21-MA, 15:25 breaks a prior coiled
    box on >= 1.5x volume with RSI rolling over.

There is no absolute-bandwidth free pass: every setup must clear a relative
squeeze gate measured against the rolling median bandwidth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.core.logging import get_logger
from app.core.market import next_trading_day
from app.data.provider import Quote, normalize_symbol
from app.strategies.base import Category, Signal, Strategy, StrategyMetadata
from app.strategies.indicators import atr, bollinger, closed_candles, rsi

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# -- compression box --------------------------------------------------------
#: Bars in the standard afternoon box (15 x 10m = 2.5h).
BOX_BARS = 15
#: Per-archetype hard box gates (box height as a fraction of the last close).
STAIRCASE_BOX_MAX = 0.0100
FLAT_BOX_MAX = 0.0100
RECLAIM_BOX_MAX = 0.0105
IGNITION_PRIOR_BOX_MAX = 0.0120
SHORT_PRIOR_BOX_MAX = 0.0120

# -- Bollinger squeeze ------------------------------------------------------
BOLLINGER_PERIOD = 21
BOLLINGER_NUM_STD = 2.0
#: Rolling window for the median bandwidth (~10 sessions of 10m bars).
SQUEEZE_LOOKBACK = 370
#: Lowered min_periods so shorter intraday histories still return a median.
SQUEEZE_MIN_PERIODS = 40
#: Relative squeeze gates: current bandwidth / rolling median bandwidth.
STAIRCASE_SQUEEZE_MAX = 0.65
FLAT_SQUEEZE_MAX = 0.50
RECLAIM_SQUEEZE_MAX = 0.60
IGNITION_SQUEEZE_MAX = 0.65
SHORT_SQUEEZE_MAX = 0.65
#: Nozzle gates: current bandwidth / max bandwidth of the prior 50 bars.
STAIRCASE_NOZZLE_MAX = 0.35
FLAT_NOZZLE_MAX = 0.35
RECLAIM_NOZZLE_MAX = 0.35
IGNITION_NOZZLE_MAX = 0.30
SHORT_NOZZLE_MAX = 0.55

# -- trigger gates ----------------------------------------------------------
STAIRCASE_MIN_IMPULSE = 0.012
LONG_RSI_MIN = 48.0
LONG_RSI_MAX = 78.0
SHORT_RSI_MAX = 47.0
RSI9_PERIOD = 9
RSI_CROSS_TOLERANCE = 2.0
SHORT_RSI_CROSS_TOLERANCE = 1.0
IGNITION_MIN_BREAK = 0.001
SHORT_MIN_BREAK = -0.0015
IGNITION_MIN_VOL = 1.5
SHORT_MIN_VOL = 1.5

# -- scoring: weights (sum = 100 before the 100-point cap) ------------------
# A. compression quality (32)
A_BOX_POINTS = 12.0
A_BOX_WORST = 0.0110
A_BOX_BEST = 0.0050
A_SQUEEZE_POINTS = 10.0
A_SQUEEZE_WORST = 0.700
A_SQUEEZE_BEST = 0.250
A_NOZZLE_POINTS = 6.0
A_NOZZLE_WORST = 0.600
A_NOZZLE_BEST = 0.080
A_THIN_POINTS = 4.0
THIN_BODY_ATR = 0.42
THIN_BODY_PRICE_PCT = 0.0018
THIN_DOJI_RATIO = 0.22

# B. setup / trigger quality (up to 43)
B_SETUP_POINTS = {
    "STAIRCASE_COIL": 19.0,
    "FLAT_COIL": 15.0,
    "BOX_IGNITION": 15.0,
    "MIDLINE_RECLAIM": 20.0,
    "SHORT_BREAKDOWN": 21.0,
}
B_RSI_POINTS = 8.0
B_RSI_CROSS_POINTS = 6.0
B_RSI_PARTIAL_POINTS = 3.0
B_RSI_CROSS_WEAK_POINTS = 2.0
B_RIBBON_POINTS = 8.0
B_RIBBON_WEAK_POINTS = 3.0

# C. participation / absorption (12)
C_VOLUME_POINTS = 7.0
C_ABSORPTION_POINTS = 5.0

# D. daily context (18)
D_TREND_POINTS = 5.0
D_TREND_PARTIAL_POINTS = 2.5
D_52W_POINTS = 5.0
D_52W_BARS = 250
D_BASE_POINTS = 4.0
D_BASE_MAX_DAYS = 3
BASE_RANGE_ATR = 0.75
D_LIQ_POINTS = 4.0
LIQ_BEST_CR = 20.0
LIQ_WORST_CR = 1.5
LIQ_DAYS = 20

# -- output -----------------------------------------------------------------
BAND_BUY = "BUY"
BAND_BUY_MIN = 72.0
MAX_SIGNALS = 15

# -- prefilter / data -------------------------------------------------------
PREFILTER_DAY_RANGE_PCT = 6.5
MIN_BARS = 80
MIN_DAILY_BARS = 40

# -- execution levels -------------------------------------------------------
ENTRY_BUFFER = 0.0015
LIMIT_SLIPPAGE_CAP = 0.0100
STOP_BUFFER = 0.0030
MIN_BTST_STOP_PCT = 0.0120

GMMA_FAST_PERIODS = (3, 5, 8, 10, 12, 15)
GMMA_SLOW_PERIODS = (30, 35, 40, 45, 50, 60)


def _ramp(value: float, worst: float, best: float) -> float:
    """Linear 0..1 ramp from ``worst`` to ``best`` (either direction)."""
    if worst == best:
        return 0.0
    return max(0.0, min(1.0, (value - worst) / (best - worst)))


def _daily_context(daily: pd.DataFrame | None, today: date) -> pd.DataFrame | None:
    """Completed daily bars only (today's partial bar is never trusted)."""
    if daily is None or daily.empty:
        return None
    frame = daily[~daily.index.duplicated(keep="last")].sort_index()
    if isinstance(frame.index, pd.DatetimeIndex):
        dates = frame.index.tz_convert(IST).date if frame.index.tz else frame.index.date
    else:  # pragma: no cover - defensive for odd providers
        dates = pd.Index(frame.index).date
    frame = frame[[d < today for d in dates]]
    return frame if len(frame) >= MIN_DAILY_BARS else None


def _daily_scores(
    daily: pd.DataFrame,
    close: float,
    direction: str,
) -> tuple[float, dict]:
    """Daily-context score (18 pts max) plus the underlying measurements."""
    ema20 = float(daily["close"].ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(daily["close"].ewm(span=50, adjust=False).mean().iloc[-1])

    if direction == "LONG":
        if close > ema20 and ema20 > ema50:
            trend_points = D_TREND_POINTS
        elif close >= ema20 * 0.985:
            trend_points = D_TREND_PARTIAL_POINTS
        else:
            trend_points = 0.0
    else:
        if close < ema20 and ema20 < ema50:
            trend_points = D_TREND_POINTS
        elif close <= ema20 * 1.015:
            trend_points = D_TREND_PARTIAL_POINTS
        else:
            trend_points = 0.0

    high_52w = float(daily["high"].tail(D_52W_BARS).max())
    dist_52w = (high_52w - close) / high_52w if high_52w > 0 else 1.0
    if direction == "LONG":
        high_points = D_52W_POINTS * _ramp(dist_52w, 0.30, 0.03)
    else:
        # A fresh breakdown from near the 52-week high has the most room;
        # a stock already 20% off its high is late/extended.
        high_points = D_52W_POINTS * _ramp(dist_52w, 0.20, 0.02)

    ranges = daily["high"] - daily["low"]
    atr_daily = atr(daily, 14)
    tight = (ranges <= BASE_RANGE_ATR * atr_daily) & atr_daily.notna()
    base_days = 0
    for value in reversed(tight.tolist()):
        if not value:
            break
        base_days += 1
        if base_days >= 10:
            break

    recent_10d_rets = daily["close"].pct_change().tail(10)
    if direction == "LONG":
        pedestal = bool((recent_10d_rets >= 0.028).any())
    else:
        pedestal = bool((recent_10d_rets <= -0.022).any())

    effective_base_days = max(base_days, 2 if pedestal else 0)
    base_points = D_BASE_POINTS * min(effective_base_days, D_BASE_MAX_DAYS) / D_BASE_MAX_DAYS

    turnover_cr = float((daily["close"] * daily["volume"]).tail(LIQ_DAYS).mean() / 1e7)
    liq_points = D_LIQ_POINTS * _ramp(turnover_cr, LIQ_WORST_CR, LIQ_BEST_CR)

    detail = {
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "dist_52w_pct": round(dist_52w * 100.0, 1),
        "base_days": base_days,
        "pedestal_impulse": pedestal,
        "turnover_cr": round(turnover_cr, 1),
    }
    return trend_points + high_points + base_points + liq_points, detail


@dataclass(frozen=True)
class _Snapshot:
    """All measurements for one evaluation candle."""

    timestamp: datetime
    last: float
    open: float
    high: float
    low: float
    green: bool
    mid: float
    upper: float
    lower: float
    pct_b: float
    bandwidth: float
    squeeze: float
    nozzle: float
    box_high: float
    box_low: float
    box_range_pct: float
    box_position: float
    thin_density: float
    prior_high: float
    prior_low: float
    prior_box_pct: float
    prior_box_position: float
    prior_squeeze: float
    prior_nozzle: float
    prior_thin_density: float
    up_break: float
    dn_break: float
    fast_mean: float
    slow_mean: float
    fast_max: float
    fast_min: float
    slow_max: float
    slow_min: float
    total_spread: float
    slow_slope: float
    rsi: float
    rsi_ema3: float
    rsi_ema3_prev: float
    rsi_sma21: float
    vol_ratio: float
    par20_long: float
    par20_short: float
    pos: float
    day_open: float
    day_high: float
    day_low: float
    day_impulse: float
    day_high_dist: float
    day_low_dist: float
    wick_ratio_long: float
    wick_ratio_short: float


@dataclass(frozen=True)
class _Candidate:
    """A gate-passing setup, with the box used for scoring."""

    setup_type: str
    direction: str
    snapshot: _Snapshot
    trigger_bar: str
    box_high: float
    box_low: float
    box_range_pct: float
    box_position: float
    squeeze: float
    nozzle: float
    thin_density: float


def _nozzle(series: pd.Series, position: int) -> float:
    """Current bandwidth / max bandwidth of the prior 50 bars."""
    if position <= 0:
        return 9.9
    start = max(0, position - 50)
    peak = float(series.iloc[start:position].max())
    current = float(series.iloc[position])
    if pd.isna(peak) or peak <= 0 or pd.isna(current):
        return 9.9
    return current / peak


def _box_metrics(box: pd.DataFrame, close: float) -> tuple[float, float, float, float]:
    high = float(box["high"].max())
    low = float(box["low"].min())
    span = high - low
    if span <= 0 or close <= 0:
        return high, low, 999.0, 0.5
    position = max(0.0, min(1.0, (close - low) / span))
    return high, low, span / close, position


def _thin_density(box: pd.DataFrame, frame: pd.DataFrame) -> float:
    period = min(50, max(2, len(frame) - 2))
    atr_series = atr(frame, period).reindex(box.index)
    bodies = (box["close"] - box["open"]).abs()
    ranges = (box["high"] - box["low"]).replace(0.0, float("nan"))
    thin = (
        ((bodies <= THIN_BODY_ATR * atr_series) & atr_series.notna())
        | ((bodies / box["close"]) <= THIN_BODY_PRICE_PCT)
        | ((bodies / ranges).fillna(0.0) <= THIN_DOJI_RATIO)
    )
    return float(thin.mean()) if len(thin) else 0.0


def _as_datetime(value) -> datetime:
    timestamp = value.to_pydatetime() if isinstance(value, pd.Timestamp) else value
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=IST)
    return timestamp.astimezone(IST)


def _snapshot(df: pd.DataFrame | None) -> _Snapshot | None:
    """Compute every metric used by the archetype gates."""
    if df is None or len(df) < MIN_BARS:
        return None
    frame = df[~df.index.duplicated(keep="last")].sort_index()
    last_close = float(frame["close"].iloc[-1])
    last_open = float(frame["open"].iloc[-1])
    if last_close <= 0:
        return None

    bb = bollinger(frame, BOLLINGER_PERIOD, BOLLINGER_NUM_STD)
    mid = float(bb["middle"].iloc[-1])
    upper = float(bb["upper"].iloc[-1])
    lower = float(bb["lower"].iloc[-1])
    bandwidth = bb["bandwidth"]
    current_bw = float(bandwidth.iloc[-1])
    if any(pd.isna(value) for value in (mid, upper, lower, current_bw)):
        return None

    min_periods = min(SQUEEZE_MIN_PERIODS, max(10, len(frame) // 2))
    median_series = bandwidth.rolling(SQUEEZE_LOOKBACK, min_periods=min_periods).median()
    median = float(median_series.iloc[-1])
    if pd.isna(median) or median <= 0:
        median = float(bandwidth.tail(30).median())
    if pd.isna(median) or median <= 0:
        return None
    squeeze = current_bw / median

    prior_bw = float(bandwidth.iloc[-2]) if len(bandwidth) >= 2 else current_bw
    prior_median = float(median_series.iloc[-2]) if len(median_series) >= 2 else median
    if pd.isna(prior_median) or prior_median <= 0:
        prior_median = median
    prior_squeeze = prior_bw / prior_median
    nozzle = _nozzle(bandwidth, len(bandwidth) - 1)
    prior_nozzle = _nozzle(bandwidth, len(bandwidth) - 2)

    box = frame.iloc[-BOX_BARS:]
    prior_box = frame.iloc[-(BOX_BARS + 1) : -1]
    box_high, box_low, box_range_pct, box_position = _box_metrics(box, last_close)
    prior_high, prior_low, prior_box_pct, prior_box_position = _box_metrics(
        prior_box, last_close
    )
    thin_density = _thin_density(box, frame)
    prior_thin_density = _thin_density(prior_box, frame)

    close = frame["close"]
    fast_series = [close.ewm(span=period, adjust=False).mean() for period in GMMA_FAST_PERIODS]
    slow_series = [close.ewm(span=period, adjust=False).mean() for period in GMMA_SLOW_PERIODS]
    fast_values = [float(series.iloc[-1]) for series in fast_series]
    slow_values = [float(series.iloc[-1]) for series in slow_series]
    fast_mean = sum(fast_values) / len(fast_values)
    slow_mean = sum(slow_values) / len(slow_values)
    fast_max, fast_min = max(fast_values), min(fast_values)
    slow_max, slow_min = max(slow_values), min(slow_values)
    total_spread = (
        (max(fast_max, slow_max) - min(fast_min, slow_min)) / last_close
        if last_close > 0
        else 1.0
    )
    slow_prev_mean = (
        sum(float(series.iloc[-16]) for series in slow_series) / len(slow_series)
        if len(frame) >= 16
        else slow_mean
    )
    slow_slope = (
        (slow_mean - slow_prev_mean) / slow_prev_mean if slow_prev_mean > 0 else 0.0
    )

    rsi_series = rsi(frame, RSI9_PERIOD)
    rsi9 = float(rsi_series.iloc[-1])
    rsi_ema3_series = rsi_series.ewm(span=3, adjust=False).mean()
    rsi_ema3 = float(rsi_ema3_series.iloc[-1])
    rsi_ema3_prev = (
        float(rsi_ema3_series.iloc[-2]) if len(rsi_ema3_series) >= 2 else rsi_ema3
    )
    rsi_sma21 = float(rsi_series.rolling(21, min_periods=5).mean().iloc[-1])
    if pd.isna(rsi9) or pd.isna(rsi_sma21):
        return None

    vol_sma = float(frame["volume"].iloc[-21:-1].mean()) if len(frame) >= 21 else 0.0
    vol_ratio = float(frame["volume"].iloc[-1] / vol_sma) if vol_sma > 0 else 1.0

    last20 = frame.iloc[-20:]
    bar_mid = (last20["high"] + last20["low"]) / 2.0
    up_bars = (last20["close"] >= last20["open"]) | (last20["close"] >= bar_mid)
    up_volume = float(last20.loc[up_bars, "volume"].sum())
    down_volume = float(last20.loc[~up_bars, "volume"].sum())
    par20_long = up_volume / down_volume if down_volume > 0 else 3.0
    par20_short = down_volume / up_volume if up_volume > 0 else 3.0

    lower_wick = (box[["open", "close"]].min(axis=1) - box["low"]).mean()
    upper_wick = (box["high"] - box[["open", "close"]].max(axis=1)).mean()
    wick_ratio_long = float(lower_wick / upper_wick) if upper_wick > 0 else 3.0
    wick_ratio_short = float(upper_wick / lower_wick) if lower_wick > 0 else 3.0

    tail = frame.tail(108)
    tail_high = float(tail["high"].max())
    tail_low = float(tail["low"].min())
    pos = (
        max(0.0, min(1.0, (last_close - tail_low) / (tail_high - tail_low)))
        if tail_high > tail_low
        else 0.5
    )

    last_date = frame.index[-1].date()
    day = frame[frame.index.date == last_date]
    if day.empty:  # pragma: no cover - defensive
        day = frame.tail(1)
    day_open = float(day["open"].iloc[0])
    day_high = float(day["high"].max())
    day_low = float(day["low"].min())
    day_impulse = (day_high - day_open) / day_open if day_open > 0 else 0.0
    day_high_dist = (day_high - last_close) / day_high if day_high > 0 else 0.0
    day_low_dist = (last_close - day_low) / day_low if day_low > 0 else 0.0

    span = upper - lower
    pct_b = (last_close - lower) / span if span > 0 else 0.5

    return _Snapshot(
        timestamp=_as_datetime(frame.index[-1]),
        last=last_close,
        open=last_open,
        high=float(frame["high"].iloc[-1]),
        low=float(frame["low"].iloc[-1]),
        green=last_close >= last_open,
        mid=mid,
        upper=upper,
        lower=lower,
        pct_b=pct_b,
        bandwidth=current_bw,
        squeeze=squeeze,
        nozzle=nozzle,
        box_high=box_high,
        box_low=box_low,
        box_range_pct=box_range_pct,
        box_position=box_position,
        thin_density=thin_density,
        prior_high=prior_high,
        prior_low=prior_low,
        prior_box_pct=prior_box_pct,
        prior_box_position=prior_box_position,
        prior_squeeze=prior_squeeze,
        prior_nozzle=prior_nozzle,
        prior_thin_density=prior_thin_density,
        up_break=(last_close - prior_high) / prior_high if prior_high > 0 else 0.0,
        dn_break=(last_close - prior_low) / prior_low if prior_low > 0 else 0.0,
        fast_mean=fast_mean,
        slow_mean=slow_mean,
        fast_max=fast_max,
        fast_min=fast_min,
        slow_max=slow_max,
        slow_min=slow_min,
        total_spread=total_spread,
        slow_slope=slow_slope,
        rsi=rsi9,
        rsi_ema3=rsi_ema3,
        rsi_ema3_prev=rsi_ema3_prev,
        rsi_sma21=rsi_sma21,
        vol_ratio=vol_ratio,
        par20_long=par20_long,
        par20_short=par20_short,
        pos=pos,
        day_open=day_open,
        day_high=day_high,
        day_low=day_low,
        day_impulse=day_impulse,
        day_high_dist=day_high_dist,
        day_low_dist=day_low_dist,
        wick_ratio_long=wick_ratio_long,
        wick_ratio_short=wick_ratio_short,
    )


def _long_rsi(m: _Snapshot) -> bool:
    return (
        LONG_RSI_MIN <= m.rsi <= LONG_RSI_MAX
        and m.rsi_ema3 > m.rsi_ema3_prev
        and (m.rsi_ema3 >= m.rsi_sma21 - RSI_CROSS_TOLERANCE or m.rsi >= m.rsi_sma21)
    )


def _short_rsi(m: _Snapshot) -> bool:
    return (
        m.rsi <= SHORT_RSI_MAX
        and m.rsi_ema3 < m.rsi_ema3_prev
        and (
            m.rsi_ema3 <= m.rsi_sma21 + SHORT_RSI_CROSS_TOLERANCE
            or m.rsi <= m.rsi_sma21
        )
    )


def _staircase(m: _Snapshot, prev: _Snapshot | None) -> bool:
    """Morning impulse then a tight afternoon pipe holding the 21-MA."""
    # The 15:25 auction bar may only confirm a reclaim when 15:05 was below
    # the mid-line. It cannot turn a marginal coil into a fresh BUY.
    if prev is not None and prev.last >= prev.mid:
        return False
    return (
        m.day_impulse >= STAIRCASE_MIN_IMPULSE
        and m.box_range_pct <= STAIRCASE_BOX_MAX
        and m.squeeze <= STAIRCASE_SQUEEZE_MAX
        and m.nozzle <= STAIRCASE_NOZZLE_MAX
        and m.last >= m.mid * 0.9985
        and m.last >= m.fast_mean * 0.999
        and m.pct_b >= 0.30
        and _long_rsi(m)
        and m.fast_mean >= m.slow_mean * 0.999
        and m.slow_slope >= -0.0005
        and m.pos >= 0.55
        and m.day_high_dist <= 0.015
    )


def _flat_coil(m: _Snapshot) -> bool:
    """Flat afternoon pipe at/above the 21-MA with a rising ribbon."""
    return (
        m.day_impulse < STAIRCASE_MIN_IMPULSE
        and m.box_range_pct <= FLAT_BOX_MAX
        and m.squeeze <= FLAT_SQUEEZE_MAX
        and m.nozzle <= FLAT_NOZZLE_MAX
        and m.last >= m.mid * 0.999
        and m.pct_b >= 0.55
        and 52.0 <= m.rsi <= 75.0
        and m.rsi_ema3 >= m.rsi_sma21 - 1.5
        and m.rsi_ema3 > m.rsi_ema3_prev
        and m.fast_mean >= m.slow_mean
        and m.slow_slope >= 0.0
        and m.pos >= 0.65
        and m.day_high_dist <= 0.010
        and m.green
    )


def _midline_reclaim(m: _Snapshot, prev: _Snapshot) -> bool:
    """15:05 below the 21-MA, 15:25 reclaims it with a green bar."""
    return (
        prev.last < prev.mid
        and m.last >= m.mid * 0.999
        and m.green
        and m.box_range_pct <= RECLAIM_BOX_MAX
        and m.squeeze <= RECLAIM_SQUEEZE_MAX
        and m.nozzle <= RECLAIM_NOZZLE_MAX
        and m.pct_b >= 0.55
        and 50.0 <= m.rsi <= 78.0
        and m.rsi_ema3 > m.rsi_ema3_prev
        and (m.last >= m.fast_max * 0.999 or m.total_spread <= 0.008)
        and m.day_high_dist <= 0.015
    )


def _box_ignition(m: _Snapshot) -> bool:
    """Close above a prior coiled box on >= 1.5x volume."""
    return (
        m.prior_box_pct <= IGNITION_PRIOR_BOX_MAX
        and m.prior_squeeze <= IGNITION_SQUEEZE_MAX
        and m.prior_nozzle <= IGNITION_NOZZLE_MAX
        and m.up_break >= IGNITION_MIN_BREAK
        and m.vol_ratio >= IGNITION_MIN_VOL
        and 52.0 <= m.rsi <= 78.0
        and m.rsi_ema3 >= m.rsi_sma21 - 1.0
        and m.rsi_ema3 > m.rsi_ema3_prev
        and m.fast_mean >= m.slow_mean
        and m.slow_slope >= 0.0
        and m.pct_b >= 0.65
        and m.green
    )


def _short_breakdown(m: _Snapshot, prev: _Snapshot) -> bool:
    """15:05 above the 21-MA, 15:25 breaks a prior coiled box on volume."""
    return (
        prev.last >= prev.mid * 0.999
        and m.last < m.mid * 0.999
        and m.prior_box_pct <= SHORT_PRIOR_BOX_MAX
        and m.prior_squeeze <= SHORT_SQUEEZE_MAX
        and m.prior_nozzle <= SHORT_NOZZLE_MAX
        and m.dn_break <= SHORT_MIN_BREAK
        and m.vol_ratio >= SHORT_MIN_VOL
        and _short_rsi(m)
        and m.fast_mean <= m.slow_mean * 1.001
        and m.slow_slope <= 0.001
        and m.pct_b <= 0.35
        and not m.green
        and m.pos <= 0.45
    )


def _candidate(
    setup_type: str,
    direction: str,
    m: _Snapshot,
    *,
    prior_box: bool = False,
) -> _Candidate:
    trigger_bar = m.timestamp.strftime("%H:%M")
    if prior_box:
        return _Candidate(
            setup_type=setup_type,
            direction=direction,
            snapshot=m,
            trigger_bar=trigger_bar,
            box_high=m.prior_high,
            box_low=m.prior_low,
            box_range_pct=m.prior_box_pct,
            box_position=m.prior_box_position,
            squeeze=m.prior_squeeze,
            nozzle=m.prior_nozzle,
            thin_density=m.prior_thin_density,
        )
    return _Candidate(
        setup_type=setup_type,
        direction=direction,
        snapshot=m,
        trigger_bar=trigger_bar,
        box_high=m.box_high,
        box_low=m.box_low,
        box_range_pct=m.box_range_pct,
        box_position=m.box_position,
        squeeze=m.squeeze,
        nozzle=m.nozzle,
        thin_density=m.thin_density,
    )


def _match_candidates(m05: _Snapshot, m25: _Snapshot | None) -> list[_Candidate]:
    """Run the archetype gates on both EOD snapshots."""
    candidates: list[_Candidate] = []

    # 15:05 continuous bar: staircase, flat coil, or a live box breakout.
    # A stock already in a tight coil is reported as the coil (the earlier,
    # higher-quality setup) rather than as a same-bar ignition.
    if _staircase(m05, None):
        candidates.append(_candidate("STAIRCASE_COIL", "LONG", m05))
    elif _flat_coil(m05):
        candidates.append(_candidate("FLAT_COIL", "LONG", m05))
    elif _box_ignition(m05):
        candidates.append(_candidate("BOX_IGNITION", "LONG", m05, prior_box=True))

    # 15:25 closing print: only state transitions / reclaims, never a fresh coil.
    if m25 is not None:
        if _short_breakdown(m25, m05):
            candidates.append(_candidate("SHORT_BREAKDOWN", "SHORT", m25, prior_box=True))
        elif _staircase(m25, m05):
            candidates.append(_candidate("STAIRCASE_COIL", "LONG", m25))
        elif _midline_reclaim(m25, m05):
            candidates.append(_candidate("MIDLINE_RECLAIM", "LONG", m25))

    return candidates


def _long_rsi_points(m: _Snapshot) -> float:
    if 50.0 <= m.rsi <= 70.0:
        return B_RSI_POINTS
    if m.rsi < 50.0:
        return B_RSI_POINTS * _ramp(m.rsi, 48.0, 50.0)
    return B_RSI_POINTS * _ramp(m.rsi, 78.0, 70.0)


def _short_rsi_points(m: _Snapshot) -> float:
    if m.rsi <= SHORT_RSI_MAX:
        return B_RSI_POINTS
    return B_RSI_POINTS * _ramp(m.rsi, SHORT_RSI_MAX, 35.0)


def _long_cross_points(m: _Snapshot) -> float:
    if m.rsi_ema3 >= m.rsi_sma21:
        return B_RSI_CROSS_POINTS
    if m.rsi_ema3 >= m.rsi_sma21 - RSI_CROSS_TOLERANCE and m.rsi_ema3 > m.rsi_ema3_prev:
        return B_RSI_PARTIAL_POINTS
    if m.rsi >= m.rsi_sma21 and m.rsi_ema3 > m.rsi_ema3_prev:
        return B_RSI_CROSS_WEAK_POINTS
    return 0.0


def _short_cross_points(m: _Snapshot) -> float:
    if m.rsi_ema3 <= m.rsi_sma21:
        return B_RSI_CROSS_POINTS
    if (
        m.rsi_ema3 <= m.rsi_sma21 + SHORT_RSI_CROSS_TOLERANCE
        and m.rsi_ema3 < m.rsi_ema3_prev
    ):
        return B_RSI_PARTIAL_POINTS
    return 0.0


def _long_ribbon_points(m: _Snapshot) -> float:
    if m.fast_mean >= m.slow_mean and m.slow_slope >= 0.0:
        return B_RIBBON_POINTS
    if m.fast_mean >= m.slow_mean:
        return B_RIBBON_POINTS * 0.75
    if m.last >= m.fast_max and m.total_spread <= 0.008:
        return B_RIBBON_POINTS * 0.625
    if m.total_spread <= 0.012:
        return B_RIBBON_WEAK_POINTS
    return 0.0


def _short_ribbon_points(m: _Snapshot) -> float:
    if m.fast_mean <= m.slow_mean * 1.001 and m.slow_slope <= 0.001:
        return B_RIBBON_POINTS
    if m.fast_mean <= m.slow_mean * 1.005:
        return B_RIBBON_POINTS * 0.625
    return 0.0


def _score(candidate: _Candidate, daily: pd.DataFrame | None) -> tuple[float, dict]:
    """Compression-heavy 0-100 score for one gate-passing candidate."""
    m = candidate.snapshot
    score_a = (
        A_BOX_POINTS * _ramp(candidate.box_range_pct, A_BOX_WORST, A_BOX_BEST)
        + A_SQUEEZE_POINTS * _ramp(candidate.squeeze, A_SQUEEZE_WORST, A_SQUEEZE_BEST)
        + A_NOZZLE_POINTS * _ramp(candidate.nozzle, A_NOZZLE_WORST, A_NOZZLE_BEST)
        + A_THIN_POINTS * candidate.thin_density
    )

    setup_points = B_SETUP_POINTS[candidate.setup_type]
    if candidate.direction == "LONG":
        rsi_points = _long_rsi_points(m)
        cross_points = _long_cross_points(m)
        ribbon_points = _long_ribbon_points(m)
        par20 = m.par20_long
        wick_ratio = m.wick_ratio_long
    else:
        rsi_points = _short_rsi_points(m)
        cross_points = _short_cross_points(m)
        ribbon_points = _short_ribbon_points(m)
        par20 = m.par20_short
        wick_ratio = m.wick_ratio_short
    score_b = setup_points + rsi_points + cross_points + ribbon_points

    score_c = (
        C_VOLUME_POINTS * _ramp(m.vol_ratio, 0.7, 1.8)
        + C_ABSORPTION_POINTS * _ramp(par20, 0.8, 1.8)
    )

    if daily is not None:
        score_d, daily_detail = _daily_scores(daily, m.last, candidate.direction)
    else:
        score_d, daily_detail = 0.0, {}
    score = min(100.0, round(score_a + score_b + score_c + score_d, 1))

    return score, {
        "compression": round(score_a, 1),
        "trigger": round(score_b, 1),
        "participation": round(score_c, 1),
        "daily": round(score_d, 1),
        "daily_detail": daily_detail,
        "par20": par20,
        "wick_ratio": wick_ratio,
    }


def _closing_frame(
    raw: pd.DataFrame | None,
    base: pd.DataFrame | None,
    now: datetime,
) -> pd.DataFrame | None:
    """Append the 15:25 closing-auction bar to the 15:05 continuous frame."""
    if raw is None or raw.empty or base is None or base.empty:
        return None
    last_date = base.index[-1].date()
    day = raw[raw.index.date == last_date]
    if day.empty:
        return None
    closing = day[day.index.time == time(15, 25)]
    if closing.empty:
        return None
    timestamp = closing.index[-1]
    bar_end = timestamp + timedelta(minutes=10)
    now_ts = pd.Timestamp(now)
    if timestamp.tzinfo is not None:
        now_ts = (
            now_ts.tz_localize(timestamp.tzinfo)
            if now_ts.tzinfo is None
            else now_ts.tz_convert(timestamp.tzinfo)
        )
    elif now_ts.tzinfo is not None:
        now_ts = now_ts.tz_localize(None)
    if bar_end > now_ts:
        return None

    frame = base.copy()
    for column in ("open", "high", "low", "close", "volume"):
        frame.loc[timestamp, column] = float(closing.iloc[-1][column])
    return frame.sort_index()


class EodCompressionBreakoutStrategy(Strategy):
    """Dual-bar EOD 21-MA Bollinger + Guppy ribbon + RSI(9) scan."""

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="EOD Compression Breakout",
            description=(
                "Unified EOD compression scan: 21-period Bollinger mid-line, "
                "12-EMA Guppy ribbon and RSI(9) trigger. Emits BUY-band LONG "
                "staircase/flat coils, box ignitions and mid-line reclaims, "
                "plus BUY-band SHORT breakdowns."
            ),
            category=Category.INTRADAY,
            universe="NIFTY_500",
            schedule_type="auto",
            timeframes=["10m", "daily"],
            run_at="15:35",
            candle_interval="10m",
        )

    # -- scheduler hook -----------------------------------------------------
    def prefilter(self, quotes: dict[str, Quote], settings: Settings) -> list[str]:
        """Keep stocks whose day range allows an afternoon compression pipe."""
        kept: list[str] = []
        for symbol in self.get_universe(settings):
            quote = quotes.get(normalize_symbol(symbol))
            if quote is None or not quote.high or not quote.low or not quote.ltp:
                kept.append(symbol)
                continue
            day_range_pct = (quote.high - quote.low) / quote.ltp * 100.0
            if day_range_pct <= PREFILTER_DAY_RANGE_PCT:
                kept.append(symbol)
        return kept

    # -- scan ---------------------------------------------------------------
    def scan(
        self,
        data: dict[str, dict[str, pd.DataFrame]],
        settings: Settings,
    ) -> list[Signal]:
        return self._scan(data, settings, datetime.now(IST))

    def _scan(
        self,
        data: dict[str, dict[str, pd.DataFrame]],
        settings: Settings,
        now: datetime,
    ) -> list[Signal]:
        move_date = next_trading_day(now.date()).isoformat()
        signals: list[Signal] = []

        for symbol, frames in data.items():
            frames = frames or {}
            raw = frames.get("10m")
            if raw is None or raw.empty:
                continue
            raw = raw[~raw.index.duplicated(keep="last")].sort_index()
            base = closed_candles(raw, "10m", now)
            if base is None or len(base) < MIN_BARS:
                continue

            daily = _daily_context(frames.get("daily"), now.date())
            m05 = _snapshot(base)
            if m05 is None:
                continue
            closing = _closing_frame(raw, base, now)
            m25 = _snapshot(closing) if closing is not None else None

            candidates = _match_candidates(m05, m25)
            if not candidates:
                continue
            # A confirmed breakdown at 15:25 invalidates an earlier long coil.
            if any(candidate.direction == "SHORT" for candidate in candidates):
                candidates = [
                    candidate for candidate in candidates if candidate.direction == "SHORT"
                ]

            scored: list[tuple[float, _Candidate, dict]] = []
            for candidate in candidates:
                score, detail = _score(candidate, daily)
                if score >= BAND_BUY_MIN:
                    scored.append((score, candidate, detail))
            if not scored:
                continue

            scored.sort(
                key=lambda item: (item[0], item[1].snapshot.timestamp),
                reverse=True,
            )
            score, candidate, detail = scored[0]
            signals.append(
                self._build_signal(
                    symbol,
                    candidate,
                    score,
                    detail,
                    move_date,
                    dual_bar=m25 is not None,
                )
            )

        signals.sort(
            key=lambda signal: (signal.metadata.get("score", 0.0), signal.timestamp),
            reverse=True,
        )
        top = signals[:MAX_SIGNALS]
        if top:
            logger.info("EOD compression: %d actionable signal(s)", len(top))
        return top

    # -- signal construction ------------------------------------------------
    def _build_signal(
        self,
        symbol: str,
        candidate: _Candidate,
        score: float,
        detail: dict,
        move_date: str,
        *,
        dual_bar: bool,
    ) -> Signal:
        m = candidate.snapshot
        if candidate.direction == "LONG":
            entry = round(
                m.last
                if candidate.setup_type in {"BOX_IGNITION", "MIDLINE_RECLAIM"}
                else candidate.box_high * (1.0 + ENTRY_BUFFER),
                2,
            )
            raw_stop = candidate.box_low * (1.0 - STOP_BUFFER)
            max_stop = entry * (1.0 - MIN_BTST_STOP_PCT)
            stop = round(min(raw_stop, max_stop), 2)
            risk_pts = max(entry - stop, entry * 0.01)
            target_1 = round(entry + 2.5 * risk_pts, 2)
            target_2 = round(entry + 4.5 * risk_pts, 2)
            limit_price = round(entry * (1.0 + LIMIT_SLIPPAGE_CAP), 2)
        else:
            entry = round(m.last, 2)
            raw_stop = candidate.box_high * (1.0 + STOP_BUFFER)
            min_stop = entry * (1.0 + MIN_BTST_STOP_PCT)
            stop = round(max(raw_stop, min_stop), 2)
            risk_pts = max(stop - entry, entry * 0.01)
            target_1 = round(entry - 2.5 * risk_pts, 2)
            target_2 = round(entry - 4.5 * risk_pts, 2)
            limit_price = round(entry * (1.0 - LIMIT_SLIPPAGE_CAP), 2)

        risk_pct = round(risk_pts / entry * 100.0, 2) if entry > 0 else 0.0
        daily_detail = detail.get("daily_detail", {})
        par20 = m.par20_long if candidate.direction == "LONG" else m.par20_short
        wick_ratio = (
            m.wick_ratio_long if candidate.direction == "LONG" else m.wick_ratio_short
        )

        return Signal(
            symbol=symbol,
            strategy_name=self.metadata.name,
            timestamp=m.timestamp,
            price=m.last,
            metadata={
                "timeframe": "10m",
                "direction": candidate.direction,
                "setup_type": candidate.setup_type,
                "score": score,
                "band": BAND_BUY,
                "score_compression": detail["compression"],
                "score_trigger": detail["trigger"],
                "score_participation": detail["participation"],
                "score_daily": detail["daily"],
                "score_bonus": 0.0,
                "entry": entry,
                "trigger_price": entry,
                "limit_price": limit_price,
                "stop": stop,
                "target_1": target_1,
                "target_2": target_2,
                "pilot_qty_pct": 40,
                "breakout_qty_pct": 60,
                "risk_pct": risk_pct,
                "box_range_pct": round(candidate.box_range_pct * 100.0, 2),
                "box_position_pct": round(candidate.box_position * 100.0, 1),
                "thin_density_pct": round(candidate.thin_density * 100.0, 0),
                "squeeze_ratio": round(candidate.squeeze, 2),
                "nozzle_ratio": round(candidate.nozzle, 2),
                "mid": round(m.mid, 2),
                "mid_dist_pct": round((m.last - m.mid) / m.mid * 100.0, 2)
                if m.mid
                else 0.0,
                "pct_b": round(m.pct_b, 2),
                "bandwidth_pct": round(m.bandwidth * 100.0, 2),
                "rsi9": round(m.rsi, 1),
                "rsi9_ema3": round(m.rsi_ema3, 1),
                "rsi9_sma21": round(m.rsi_sma21, 1),
                "rsi9_above_slow": bool(m.rsi_ema3 >= m.rsi_sma21),
                "gmma_fast_mean": round(m.fast_mean, 2),
                "gmma_slow_mean": round(m.slow_mean, 2),
                "gmma_fast_above_slow": bool(m.fast_mean >= m.slow_mean),
                "gmma_total_spread_pct": round(m.total_spread * 100.0, 2),
                "gmma_slow_slope_pct": round(m.slow_slope * 100.0, 2),
                "day_open": round(m.day_open, 2),
                "day_high": round(m.day_high, 2),
                "day_low": round(m.day_low, 2),
                "day_impulse_pct": round(m.day_impulse * 100.0, 2),
                "day_high_dist_pct": round(m.day_high_dist * 100.0, 2),
                "day_low_dist_pct": round(m.day_low_dist * 100.0, 2),
                "vol_mult": round(m.vol_ratio, 2),
                "par20": round(par20, 2),
                "wick_ratio": round(wick_ratio, 2),
                "daily_data": bool(daily_detail),
                **daily_detail,
                "compression_bars": BOX_BARS,
                "trigger_bar": candidate.trigger_bar,
                "dual_bar": dual_bar,
                "candle_time": m.timestamp.isoformat(),
                "expected_move_date": move_date,
            },
        )


# Registry hook: the strategy framework discovers this module.
strategy_class = EodCompressionBreakoutStrategy
