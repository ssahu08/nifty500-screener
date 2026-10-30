"""Strategy 6: EOD Guppy Ribbon + Bollinger Compression Breakout & Breakdown - v3.2.

Added as a *new* strategy alongside the existing v3 "EOD Compression Breakout"
(``app/strategies/compression.py``), which stays unchanged. This module adds
full 12-EMA Guppy Multiple Moving Average (GMMA) ribbon geometry, adaptive
multi-window afternoon box detection (15-bar and 10-bar), dual-path Bollinger
bandwidth gates, and bidirectional (LONG & SHORT) institutional setup
classification:

Bullish (LONG) setups:
  * STAIRCASE_COIL: morning impulse step (>= 1.2%) followed by an afternoon
    horizontal pipe resting on a rising Blue Slow GMMA ribbon (MANAKCOAT,
    AEROFLEX, BANKBARODA, AXISBANK, ICICIBANK, JSWSTEEL, JWL).
  * EOD_COIL: full-afternoon tight Bollinger + 12-EMA Guppy knot without a
    staircase step (JSWENERGY, BSOFT).
  * BOX_IGNITION: live breakout bar closing above a prior coiled box with
    volume expansion and RSI(9) EMA(3) >= SMA(21).
  * SPRING_RECLAIM: post-coil morning shakeout dip (<= 1.8%) that reclaims the
    box midline with a green candle.

Bearish (SHORT) setups:
  * SHORT_DIST_COIL: afternoon dead-cat pipe trapped underneath a declining
    Blue Slow GMMA ribbon with Fast Red EMAs compressed below Slow Blue EMAs
    (INFY pre-breakdown shelf).
  * SHORT_BREAKDOWN: live high-volume floor breakdown below a coiled
    distribution shelf with RSI(9) collapsing below SMA(21) (INFY -4.05%).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.core.logging import get_logger
from app.core.market import next_trading_day
from app.data.provider import Quote, normalize_symbol
from app.strategies.base import Category, Signal, Strategy, StrategyMetadata
from app.strategies.indicators import atr, bollinger, closed_candles, rsi, supertrend

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# -- compression box windows ------------------------------------------------
#: Standard afternoon box: 15 x 10m = 2.5h, 3 x 1h = 3h.
BOX_BARS = {"10m": 15, "1h": 3}
#: Late afternoon box (10 x 10m = 100 mins) for stocks that rally until 12:30 PM
#: and then pin dead-flat into the 15:30 close (BANKBARODA, AXISBANK, JWL).
LATE_BOX_BARS_10M = 10
MAX_BOX_RANGE_PCT = 0.022
MAX_BOX_RANGE_BY_TF = {"10m": 0.022, "1h": 0.030}

# -- Bollinger squeeze parameters -------------------------------------------
BOLLINGER_PERIOD = 20
BOLLINGER_NUM_STD = 2.0
SQUEEZE_LOOKBACK = {"10m": 370, "1h": 60}
SQUEEZE_MIN_PERIODS = {"10m": 40, "1h": 10}
SQUEEZE_MAX_RATIO = 0.68
ABS_SQUEEZE_MAX_BW = {"10m": 0.0195, "1h": 0.0320}

# -- Guppy Multiple Moving Average (GMMA) periods ---------------------------
#: 6 Fast Red Trader EMAs and 6 Slow Blue Investor EMAs.
GMMA_FAST_PERIODS = (3, 5, 8, 10, 12, 15)
GMMA_SLOW_PERIODS = (30, 35, 40, 45, 50, 60)
GMMA_FAST_PINCH_MAX_PCT = 0.0050   # 0.50% fast ribbon spread
GMMA_KNOT_MAX_PCT = 0.0090         # 0.90% total 12-EMA spread

# -- scoring weights (sum = 100 base + up to 5 MTF bonus, capped at 100) ----
# A. Compression & GMMA Ribbon Pinch Quality (26 pts)
A_BOX_POINTS = 10.0
A_BOX_BEST_PCT = 0.006
A_SQUEEZE_POINTS = 8.0
A_SQUEEZE_BEST_RATIO = 0.25
A_THIN_POINTS = 8.0
THIN_BODY_ATR = 0.42
THIN_BODY_PRICE_PCT = 0.0018
THIN_DOJI_RATIO = 0.22

# B. Institutional Absorption / Distribution & GMMA Structure (24 pts)
B_PAR_POINTS = 8.0
PAR_BEST = 1.55
PAR_WORST = 0.85
B_WICK_POINTS = 6.0
WICK_BEST = 1.25
WICK_WORST = 0.80
B_GMMA_STRUCT_POINTS = 10.0  # Blue-ribbon support + morning pedestal step

# C. Intraday Timing: RSI(9) + EMA(3)/SMA(21) + ST/GMMA + VWAP (26 pts)
C_RSI_POINTS = 10.0
RSI9_PERIOD = 9
RSI9_LONG_PLATEAU = (39.0, 64.0)
RSI9_SHORT_PLATEAU = (34.0, 54.0)
RSI9_OUTER_SPAN = 14.0
C_RSI_CROSS_POINTS = 6.0
C_RSI_CURL_POINTS = 4.5
C_TREND_ALIGN_POINTS = 6.0
C_VWAP_POINTS = 4.0
VWAP_BARS = 25
#: Cap for score C = 26 base + 6 live-ignition volume bonus.
C_MAX_POINTS = 26.0 + 6.0

# D. Daily Context & Multi-Day Pedestal Base (24 pts)
D_TREND_POINTS = 6.0
D_TREND_PARTIAL_POINTS = 3.5
D_52W_POINTS = 6.0
D_52W_BEST = 0.03
D_52W_WORST = 0.30
D_52W_BARS = 250
D_BASE_POINTS = 6.0
D_BASE_MAX_DAYS = 3
BASE_RANGE_ATR = 0.75
D_LIQ_POINTS = 6.0
LIQ_BEST_CR = 20.0
LIQ_WORST_CR = 1.5
LIQ_DAYS = 20

# Bonuses
MTF_CONFLUENCE_BONUS = 5.0
IGNITION_VOL_BONUS = 6.0

# -- score bands ------------------------------------------------------------
BAND_BUY = "BUY"
BAND_HOLD = "HOLD"
BAND_SKIP = "SKIP"
BAND_BUY_MIN = 68.0
BAND_HOLD_MIN = 45.0

# -- confluence & execution constants ---------------------------------------
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0
PREFILTER_DAY_RANGE_PCT = 6.5
MIN_BARS = {"10m": 45, "1h": 14}
MIN_DAILY_BARS = 40
TEN_MIN_BARS_PER_HOUR = 6
ENTRY_BUFFER = 0.0015         # +0.15% trigger buffer outside box edge
LIMIT_SLIPPAGE_CAP = 0.0100   # +1.00% marketable limit cap for 09:15 AM SL-L orders
STOP_BUFFER = 0.0030
MIN_BTST_STOP_PCT = 0.0120    # Minimum 1.20% stop distance to survive morning shakeouts
#: Minimum pre-box impulse for a coil to be labelled STAIRCASE_COIL.
STAIRCASE_MIN_STEP_PCT = 0.012


def hourly_from_10m(df: pd.DataFrame) -> pd.DataFrame | None:
    """Build NSE-aligned 1-hour candles from 10-minute data (starting 09:15 IST)."""
    if df is None or df.empty:
        return None
    grouped = df.resample("1h", offset="15min")
    counts = grouped["close"].count()
    hourly = grouped.agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna(subset=["open"])
    full_hours = counts.reindex(hourly.index) >= TEN_MIN_BARS_PER_HOUR
    hourly = hourly[full_hours]
    return hourly if not hourly.empty else None


def _ramp(value: float, worst: float, best: float) -> float:
    """Linear 0..1 ramp from ``worst`` to ``best`` (works in either direction)."""
    if worst == best:
        return 0.0
    fraction = (value - worst) / (best - worst)
    return max(0.0, min(1.0, fraction))


def _compute_gmma_state(df: pd.DataFrame, box_bars: int) -> dict[str, float | bool]:
    """Computes 12-EMA Guppy Ribbon geometry and morning pedestal impulse metrics."""
    close = df["close"]
    last_close = float(close.iloc[-1])

    fast_series = [close.ewm(span=p, adjust=False).mean() for p in GMMA_FAST_PERIODS]
    slow_series = [close.ewm(span=p, adjust=False).mean() for p in GMMA_SLOW_PERIODS]

    fast_vals = [float(s.iloc[-1]) for s in fast_series]
    slow_vals = [float(s.iloc[-1]) for s in slow_series]
    slow_prev_vals = [
        float(s.iloc[-min(len(s), box_bars)]) for s in slow_series
    ]

    fast_mean = sum(fast_vals) / len(fast_vals)
    slow_mean = sum(slow_vals) / len(slow_vals)
    slow_prev_mean = sum(slow_prev_vals) / len(slow_prev_vals)

    fast_min, fast_max = min(fast_vals), max(fast_vals)
    slow_min, slow_max = min(slow_vals), max(slow_vals)

    fast_spread_pct = (fast_max - fast_min) / last_close if last_close > 0 else 1.0
    slow_spread_pct = (slow_max - slow_min) / last_close if last_close > 0 else 1.0
    total_spread_pct = (
        (max(fast_max, slow_max) - min(fast_min, slow_min)) / last_close
        if last_close > 0
        else 1.0
    )

    # Slope of the Blue Slow Ribbon across the compression box
    slow_slope_pct = (
        (slow_mean - slow_prev_mean) / slow_prev_mean if slow_prev_mean > 0 else 0.0
    )

    # Check if a morning impulse ("staircase step") preceded the afternoon box
    pre_box_window = df.iloc[-min(len(df), box_bars + 18) : -box_bars]
    morning_step_pct = 0.0
    if not pre_box_window.empty:
        pre_low = float(pre_box_window["low"].min())
        pre_high = float(pre_box_window["high"].max())
        if pre_low > 0:
            morning_step_pct = (last_close - pre_low) / pre_low
        # Negative step for bearish distribution setups (INFY)
        if last_close < slow_mean and pre_high > 0:
            morning_step_pct = (last_close - pre_high) / pre_high

    return {
        "fast_mean": fast_mean,
        "slow_mean": slow_mean,
        "fast_min": fast_min,
        "fast_max": fast_max,
        "slow_min": slow_min,
        "slow_max": slow_max,
        "fast_spread_pct": fast_spread_pct,
        "slow_spread_pct": slow_spread_pct,
        "total_spread_pct": total_spread_pct,
        "slow_slope_pct": slow_slope_pct,
        "fast_above_slow": fast_mean >= slow_mean * 0.9985,
        "fast_below_slow": fast_mean <= slow_mean * 1.0015,
        "price_on_or_above_slow": last_close >= slow_min * 0.996,
        "price_on_or_below_slow": last_close <= slow_max * 1.004,
        "morning_step_pct": morning_step_pct,
    }


def _daily_context(daily: pd.DataFrame | None, today: date) -> pd.DataFrame | None:
    """Completed daily bars only (today's partial bar is never trusted)."""
    if daily is None or daily.empty:
        return None
    frame = daily[~daily.index.duplicated(keep="last")].sort_index()
    if isinstance(frame.index, pd.DatetimeIndex):
        dates = frame.index.tz_convert(IST).date if frame.index.tz else frame.index.date
    else:
        dates = pd.Index(frame.index).date
    frame = frame[[d < today for d in dates]]
    return frame if len(frame) >= MIN_DAILY_BARS else None


def _daily_scores(
    daily: pd.DataFrame, close: float, direction: str
) -> tuple[float, dict]:
    """Daily-context score (24 pts max) for LONG or SHORT setups."""
    ema20_s = daily["close"].ewm(span=20, adjust=False).mean()
    ema50_s = daily["close"].ewm(span=50, adjust=False).mean()
    ema20, ema50 = float(ema20_s.iloc[-1]), float(ema50_s.iloc[-1])

    if direction == "LONG":
        if close > ema20 and ema20 > ema50:
            trend_points = D_TREND_POINTS
        elif close >= ema20 * 0.985:
            trend_points = D_TREND_PARTIAL_POINTS
        else:
            trend_points = 0.0
    else:
        # Bearish trend alignment (price below EMA20/EMA50 or rolling over)
        if close < ema20 and ema20 < ema50:
            trend_points = D_TREND_POINTS
        elif close <= ema20 * 1.015:
            trend_points = D_TREND_PARTIAL_POINTS
        else:
            trend_points = 0.0

    high_52w = float(daily["high"].tail(D_52W_BARS).max())
    dist_52w = (high_52w - close) / high_52w if high_52w > 0 else 1.0
    if direction == "LONG":
        high_points = D_52W_POINTS * _ramp(dist_52w, D_52W_WORST, D_52W_BEST)
    else:
        # For SHORT setups, reward stocks that have broken >= 5% off highs
        high_points = D_52W_POINTS * _ramp(dist_52w, 0.02, 0.18)

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
        had_pedestal = bool((recent_10d_rets >= 0.028).any())
    else:
        had_pedestal = bool((recent_10d_rets <= -0.022).any())

    effective_base_days = max(base_days, 2 if had_pedestal else 0)
    base_points = D_BASE_POINTS * min(effective_base_days, D_BASE_MAX_DAYS) / D_BASE_MAX_DAYS

    turnover_cr = float(
        (daily["close"] * daily["volume"]).tail(LIQ_DAYS).mean() / 1e7
    )
    liq_points = D_LIQ_POINTS * _ramp(turnover_cr, LIQ_WORST_CR, LIQ_BEST_CR)

    detail = {
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "dist_52w_pct": round(dist_52w * 100.0, 1),
        "base_days": base_days,
        "pedestal_impulse": had_pedestal,
        "turnover_cr": round(turnover_cr, 1),
    }
    return trend_points + high_points + base_points + liq_points, detail


class GuppyCompressionStrategy(Strategy):
    """Bidirectional EOD Guppy Ribbon + Bollinger Compression Scanner (10m + 1h)."""

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Guppy Ribbon Compression",
            description=(
                "Scans Nifty 500 for Guppy GMMA + Bollinger compression pipes: "
                "flags bullish staircase/coil breakouts (STAIRCASE_COIL, EOD_COIL, "
                "BOX_IGNITION, SPRING_RECLAIM) and bearish distribution breakdowns "
                "(SHORT_DIST_COIL, SHORT_BREAKDOWN)."
            ),
            category=Category.INTRADAY,
            universe="NIFTY_500",
            schedule_type="auto",
            timeframes=["10m", "daily"],
            run_at="15:20",
            candle_interval="10m",
        )

    def prefilter(self, quotes: dict[str, Quote], settings: Settings) -> list[str]:
        """Keep stocks whose full-day range permits an afternoon compression shelf."""
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

    def scan(
        self,
        data: dict[str, dict[str, pd.DataFrame]],
        settings: Settings,
    ) -> list[Signal]:
        now = datetime.now(IST)
        move_date = next_trading_day(now.date()).isoformat()
        signals: list[Signal] = []

        for symbol, frames in data.items():
            frames = frames or {}
            ten_min = closed_candles(frames.get("10m"), "10m", now)
            if ten_min is None or ten_min.empty:
                continue
            daily = _daily_context(frames.get("daily"), now.date())

            candidates: list[tuple[str, pd.DataFrame]] = [("10m", ten_min)]
            hourly = hourly_from_10m(ten_min)
            if hourly is not None:
                candidates.append(("1h", hourly))

            sym_signals: list[Signal] = []
            for timeframe, frame in candidates:
                sig = self._evaluate(symbol, timeframe, frame, daily, now, move_date)
                if sig is not None:
                    sym_signals.append(sig)

            if not sym_signals:
                continue

            # Deduplicate 10m & 1h per symbol; award +5.0 pt MTF confluence
            # bonus. score_bonus records the bonus actually applied so the
            # score components still add up to the total (incl. 100 cap).
            sym_signals.sort(key=lambda s: s.metadata.get("score", 0.0), reverse=True)
            best_sig = sym_signals[0]
            if len(sym_signals) > 1:
                base_score = float(best_sig.metadata["score"])
                boosted = min(100.0, round(base_score + MTF_CONFLUENCE_BONUS, 1))
                best_sig.metadata["score_bonus"] = round(boosted - base_score, 1)
                best_sig.metadata["score"] = boosted
                best_sig.metadata["mtf_confluence"] = True
                if boosted >= BAND_BUY_MIN:
                    best_sig.metadata["band"] = BAND_BUY
                elif boosted >= BAND_HOLD_MIN:
                    best_sig.metadata["band"] = BAND_HOLD
            else:
                best_sig.metadata["score_bonus"] = 0.0
                best_sig.metadata["mtf_confluence"] = False

            signals.append(best_sig)

        signals.sort(key=lambda s: s.metadata.get("score", 0.0), reverse=True)
        return signals

    def _evaluate(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        daily: pd.DataFrame | None,
        now: datetime,
        move_date: str,
    ) -> Signal | None:
        box_bars = BOX_BARS[timeframe]
        if len(df) < max(MIN_BARS[timeframe], box_bars + 6):
            return None

        last_close = float(df["close"].iloc[-1])
        last_open = float(df["open"].iloc[-1])
        if last_close <= 0:
            return None

        max_box_pct = MAX_BOX_RANGE_BY_TF.get(timeframe, MAX_BOX_RANGE_PCT)

        # -- 1. Bollinger Bandwidth & GMMA Ribbon Geometry ------------------
        bb = bollinger(df, BOLLINGER_PERIOD, BOLLINGER_NUM_STD)
        bandwidth = bb["bandwidth"]
        last_bw_raw = float(bandwidth.iloc[-1]) if not pd.isna(bandwidth.iloc[-1]) else 999.0
        bw_scale = 0.01 if last_bw_raw > 0.25 else 1.0
        bw_frac_series = bandwidth * bw_scale

        median_width = (
            bw_frac_series.rolling(
                SQUEEZE_LOOKBACK[timeframe],
                min_periods=min(SQUEEZE_MIN_PERIODS[timeframe], max(10, len(df) // 2)),
            )
            .median()
            .iloc[-1]
        )
        if pd.isna(median_width) or median_width <= 0:
            median_width = float(bw_frac_series.tail(30).median())
        if pd.isna(median_width) or median_width <= 0:
            return None

        gmma = _compute_gmma_state(df, box_bars)

        # -- 2. Adaptive Box Selection (15-bar standard vs 10-bar late pipe) -
        box = df.iloc[-box_bars:]
        box_high = float(box["high"].max())
        box_low = float(box["low"].min())
        box_range = box_high - box_low
        box_range_pct = box_range / last_close if box_range > 0 else 999.0

        if timeframe == "10m":
            late_box = df.iloc[-LATE_BOX_BARS_10M:]
            l_high = float(late_box["high"].max())
            l_low = float(late_box["low"].min())
            l_range = l_high - l_low
            l_range_pct = l_range / last_close if l_range > 0 else 999.0
            # Use the 10-bar late box if the 15-bar box caught the tail of a morning rally
            if 0 < l_range_pct <= (max_box_pct * 0.85) and (
                box_range_pct > max_box_pct or l_range_pct < box_range_pct * 0.65
            ):
                box = late_box
                box_high, box_low, box_range, box_range_pct = (
                    l_high,
                    l_low,
                    l_range,
                    l_range_pct,
                )
                box_bars = LATE_BOX_BARS_10M
                gmma = _compute_gmma_state(df, box_bars)

        ref_bw = float(bw_frac_series.iloc[-1])
        squeeze_ratio = ref_bw / float(median_width)
        abs_bw_limit = ABS_SQUEEZE_MAX_BW.get(timeframe, 0.020)

        is_squeezed_now = (
            (squeeze_ratio <= SQUEEZE_MAX_RATIO)
            or (ref_bw <= abs_bw_limit)
            or (float(gmma["fast_spread_pct"]) <= GMMA_FAST_PINCH_MAX_PCT)
            or (float(gmma["total_spread_pct"]) <= GMMA_KNOT_MAX_PCT)
        )

        # -- 3. Classify Setup Type & Direction (LONG vs SHORT) -------------
        setup_type: str | None = None
        direction = "LONG"

        if 0 < box_range_pct <= max_box_pct and is_squeezed_now:
            if (
                bool(gmma["fast_above_slow"])
                and bool(gmma["price_on_or_above_slow"])
                and float(gmma["slow_slope_pct"]) >= -0.001
            ):
                # A bullish coil is a staircase only when an impulse step
                # preceded the box; otherwise it is a plain EOD base coil.
                if float(gmma["morning_step_pct"]) >= STAIRCASE_MIN_STEP_PCT:
                    setup_type = "STAIRCASE_COIL"
                else:
                    setup_type = "EOD_COIL"
                direction = "LONG"
            elif (
                bool(gmma["fast_below_slow"])
                and bool(gmma["price_on_or_below_slow"])
                and float(gmma["slow_slope_pct"]) <= -0.0015
            ):
                # Bearish distribution shelf under falling Blue GMMA (INFY 27/10-28/10)
                setup_type = "SHORT_DIST_COIL"
                direction = "SHORT"
            else:
                setup_type = "EOD_COIL"
                direction = "LONG"
        else:
            # Check Prior-Box Live Breakout (BOX_IGNITION) or Breakdown (SHORT_BREAKDOWN)
            prior_box = df.iloc[-(box_bars + 1) : -1]
            p_high = float(prior_box["high"].max())
            p_low = float(prior_box["low"].min())
            p_range = p_high - p_low
            p_range_pct = p_range / last_close if p_range > 0 else 999.0
            p_bw = float(bw_frac_series.iloc[-2]) if len(bw_frac_series) >= 2 else ref_bw
            p_sq_ratio = p_bw / float(median_width)
            was_squeezed_prior = (
                (p_sq_ratio <= SQUEEZE_MAX_RATIO)
                or (p_bw <= abs_bw_limit)
                or (float(gmma["fast_spread_pct"]) <= GMMA_FAST_PINCH_MAX_PCT * 1.3)
            )

            if 0 < p_range_pct <= max_box_pct and was_squeezed_prior:
                if last_close > p_high and last_close > last_open:
                    setup_type = "BOX_IGNITION"
                    direction = "LONG"
                    box = prior_box
                    box_high, box_low, box_range, box_range_pct = (
                        p_high,
                        p_low,
                        p_range,
                        p_range_pct,
                    )
                    squeeze_ratio = p_sq_ratio
                elif (
                    last_close < p_low
                    and last_close < last_open
                    and bool(gmma["fast_below_slow"])
                ):
                    setup_type = "SHORT_BREAKDOWN"
                    direction = "SHORT"
                    box = prior_box
                    box_high, box_low, box_range, box_range_pct = (
                        p_high,
                        p_low,
                        p_range,
                        p_range_pct,
                    )
                    squeeze_ratio = p_sq_ratio

            # Check Bullish Morning Shakeout Spring (SPRING_RECLAIM - PATANJALI)
            if setup_type is None and timeframe == "10m" and len(df) >= box_bars + 6:
                spring_box = df.iloc[-(box_bars + 4) : -4]
                s_high = float(spring_box["high"].max())
                s_low = float(spring_box["low"].min())
                s_range = s_high - s_low
                s_range_pct = s_range / last_close if s_range > 0 else 999.0
                recent_dip_low = float(df["low"].iloc[-4:].min())
                dip_depth_pct = (s_low - recent_dip_low) / s_low if s_low > 0 else 999.0

                if (
                    0 < s_range_pct <= max_box_pct
                    and 0.0 <= dip_depth_pct <= 0.018
                    and last_close >= (s_high + s_low) / 2.0
                    and last_close > last_open
                ):
                    setup_type = "SPRING_RECLAIM"
                    direction = "LONG"
                    box = spring_box
                    box_high = s_high
                    box_low = min(s_low, recent_dip_low)
                    box_range = box_high - box_low
                    box_range_pct = s_range_pct

        if setup_type is None or box_range <= 0:
            return None

        # -- 4. Score A: Compression & GMMA Pinch Quality (26 pts) ----------
        a_box = A_BOX_POINTS * _ramp(box_range_pct, max_box_pct, A_BOX_BEST_PCT)
        effective_sq_ratio = min(
            squeeze_ratio,
            ref_bw / abs_bw_limit * 0.40,
            float(gmma["fast_spread_pct"]) / GMMA_FAST_PINCH_MAX_PCT * 0.35,
        )
        a_squeeze = A_SQUEEZE_POINTS * _ramp(
            effective_sq_ratio, SQUEEZE_MAX_RATIO, A_SQUEEZE_BEST_RATIO
        )

        atr50_series = atr(df, min(50, len(df) - 2)).reindex(box.index)
        bodies = (box["close"] - box["open"]).abs()
        candle_ranges = (box["high"] - box["low"]).replace(0.0, float("nan"))
        is_thin = (
            ((bodies <= THIN_BODY_ATR * atr50_series) & atr50_series.notna())
            | ((bodies / box["close"]) <= THIN_BODY_PRICE_PCT)
            | ((bodies / candle_ranges).fillna(0.0) <= THIN_DOJI_RATIO)
        )
        thin_density = float(is_thin.mean()) if len(is_thin) else 0.0
        a_thin = A_THIN_POINTS * thin_density
        score_a = a_box + a_squeeze + a_thin

        # -- 5. Score B: Absorption / Distribution & GMMA Structure (24 pts) -
        last20 = df.iloc[-20:]
        bar_mid = (last20["high"] + last20["low"]) / 2.0
        up_bars = (last20["close"] >= last20["open"]) | (last20["close"] >= bar_mid)
        up_volume = float(last20.loc[up_bars, "volume"].sum())
        down_volume = float(last20.loc[~up_bars, "volume"].sum())

        if direction == "LONG":
            par20 = up_volume / down_volume if down_volume > 0 else 3.0
        else:
            par20 = down_volume / up_volume if up_volume > 0 else 3.0
        b_par = B_PAR_POINTS * _ramp(par20, PAR_WORST, PAR_BEST)

        lower_wick = (box[["open", "close"]].min(axis=1) - box["low"]).mean()
        upper_wick = (box["high"] - box[["open", "close"]].max(axis=1)).mean()
        if direction == "LONG":
            wick_ratio = float(lower_wick / upper_wick) if upper_wick > 0 else 3.0
        else:
            wick_ratio = float(upper_wick / lower_wick) if lower_wick > 0 else 3.0
        b_wick = B_WICK_POINTS * _ramp(wick_ratio, WICK_WORST, WICK_BEST)

        box_position = max(0.0, min(1.0, (last_close - box_low) / box_range))
        if direction == "LONG":
            if bool(gmma["fast_above_slow"]) and bool(gmma["price_on_or_above_slow"]):
                step_bonus = 1.0 if float(gmma["morning_step_pct"]) >= 0.012 else 0.90
                b_struct = B_GMMA_STRUCT_POINTS * step_bonus
            elif bool(gmma["price_on_or_above_slow"]) or (
                float(gmma["total_spread_pct"]) <= GMMA_KNOT_MAX_PCT
            ):
                b_struct = B_GMMA_STRUCT_POINTS * 0.82
            else:
                b_struct = B_GMMA_STRUCT_POINTS * _ramp(box_position, 0.25, 0.85)
        else:
            # SHORT structure: Fast ribbon trapped below declining Slow Blue ribbon
            if bool(gmma["fast_below_slow"]) and bool(gmma["price_on_or_below_slow"]):
                b_struct = B_GMMA_STRUCT_POINTS
            else:
                b_struct = B_GMMA_STRUCT_POINTS * _ramp(1.0 - box_position, 0.25, 0.85)

        score_b = b_par + b_wick + b_struct

        # -- 6. Score C: Intraday Timing & RSI(9) Subpanel (26 pts) ---------
        rsi9_series = rsi(df, RSI9_PERIOD)
        rsi9 = float(rsi9_series.iloc[-1])
        rsi9_ema3_s = rsi9_series.ewm(span=3, adjust=False).mean()
        rsi9_ema3 = float(rsi9_ema3_s.iloc[-1])
        rsi9_ema3_prev = (
            float(rsi9_ema3_s.iloc[-2]) if len(rsi9_ema3_s) >= 2 else rsi9_ema3
        )
        rsi9_sma21 = float(rsi9_series.rolling(21, min_periods=5).mean().iloc[-1])
        if pd.isna(rsi9) or pd.isna(rsi9_sma21):
            return None

        p_low, p_high = (
            RSI9_LONG_PLATEAU if direction == "LONG" else RSI9_SHORT_PLATEAU
        )
        if setup_type == "BOX_IGNITION" and rsi9 >= p_low:
            c_rsi = C_RSI_POINTS
        elif setup_type == "SHORT_BREAKDOWN" and rsi9 <= p_high:
            c_rsi = C_RSI_POINTS
        elif p_low <= rsi9 <= p_high:
            c_rsi = C_RSI_POINTS
        elif rsi9 < p_low:
            c_rsi = C_RSI_POINTS * max(0.0, 1.0 - (p_low - rsi9) / RSI9_OUTER_SPAN)
        else:
            c_rsi = C_RSI_POINTS * max(0.0, 1.0 - (rsi9 - p_high) / RSI9_OUTER_SPAN)

        if direction == "LONG":
            if rsi9_ema3 >= rsi9_sma21:
                c_cross = C_RSI_CROSS_POINTS
            elif (rsi9_ema3 >= rsi9_sma21 - 4.5) and (
                rsi9_ema3 >= rsi9_ema3_prev - 0.5
            ):
                c_cross = C_RSI_CURL_POINTS
            else:
                c_cross = 0.0
        else:
            if rsi9_ema3 <= rsi9_sma21:
                c_cross = C_RSI_CROSS_POINTS
            elif (rsi9_ema3 <= rsi9_sma21 + 4.5) and (rsi9_ema3 <= rsi9_ema3_prev):
                c_cross = C_RSI_CURL_POINTS
            else:
                c_cross = 0.0

        st = supertrend(df, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)
        st_direction = float(st["direction"].iloc[-1])
        st_value = (
            float(st["supertrend"].iloc[-1]) if not pd.isna(st.iloc[-1, 0]) else 0.0
        )

        if direction == "LONG":
            if st_direction == 1.0 or (
                bool(gmma["fast_above_slow"]) and bool(gmma["price_on_or_above_slow"])
            ):
                c_st = C_TREND_ALIGN_POINTS
            elif (
                st_value > 0 and abs(last_close - st_value) / last_close <= 0.018
            ) or (float(gmma["total_spread_pct"]) <= GMMA_KNOT_MAX_PCT):
                c_st = C_TREND_ALIGN_POINTS * 0.75
            else:
                c_st = 0.0
        else:
            if st_direction == -1.0 or (
                bool(gmma["fast_below_slow"]) and bool(gmma["price_on_or_below_slow"])
            ):
                c_st = C_TREND_ALIGN_POINTS
            else:
                c_st = C_TREND_ALIGN_POINTS * 0.50

        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        vwap_series = (typical * df["volume"]).rolling(VWAP_BARS).sum() / df[
            "volume"
        ].rolling(VWAP_BARS).sum().replace(0.0, float("nan"))
        vwap = float(vwap_series.iloc[-1]) if not pd.isna(vwap_series.iloc[-1]) else 0.0

        if direction == "LONG":
            vwap_aligned = bool(
                (vwap and last_close >= vwap * 0.996)
                or bool(gmma["price_on_or_above_slow"])
            )
        else:
            vwap_aligned = bool(
                (vwap and last_close <= vwap * 1.004)
                or bool(gmma["price_on_or_below_slow"])
            )
        c_vwap = C_VWAP_POINTS if vwap_aligned else 0.0

        vol_sma20 = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else 0.0
        vol_mult = float(df["volume"].iloc[-1] / vol_sma20) if vol_sma20 > 0 else 1.0
        ignition_bonus = (
            IGNITION_VOL_BONUS
            if (
                setup_type in ("BOX_IGNITION", "SPRING_RECLAIM", "SHORT_BREAKDOWN")
                and vol_mult >= 1.5
            )
            else 0.0
        )
        score_c = min(
            C_MAX_POINTS,
            c_rsi + c_cross + c_st + c_vwap + ignition_bonus,
        )

        # -- 7. Score D: Daily Context (24 pts) -----------------------------
        daily_points, daily_detail = 0.0, {}
        if daily is not None:
            daily_points, daily_detail = _daily_scores(daily, last_close, direction)
        score_d = daily_points

        score = min(100.0, round(score_a + score_b + score_c + score_d, 1))
        if score >= BAND_BUY_MIN:
            band = BAND_BUY
        elif score >= BAND_HOLD_MIN:
            band = BAND_HOLD
        else:
            band = BAND_SKIP

        # -- 8. Actionable Execution & SL-L Slippage-Protected Levels -------
        if direction == "LONG":
            entry = round(
                last_close
                if setup_type == "BOX_IGNITION"
                else box_high * (1.0 + ENTRY_BUFFER),
                2,
            )
            limit_price = round(entry * (1.0 + LIMIT_SLIPPAGE_CAP), 2)
            stop_ref = (
                min(box_low, float(gmma["slow_min"]))
                if bool(gmma["price_on_or_above_slow"])
                else box_low
            )
            raw_stop = stop_ref * (1.0 - STOP_BUFFER)
            max_allowed_stop = entry * (1.0 - MIN_BTST_STOP_PCT)
            stop = round(min(raw_stop, max_allowed_stop), 2)
            risk_pts = max(entry - stop, entry * 0.01)
            target_1 = round(entry + 2.5 * risk_pts, 2)
            target_2 = round(entry + 4.5 * risk_pts, 2)
        else:
            entry = round(
                last_close
                if setup_type == "SHORT_BREAKDOWN"
                else box_low * (1.0 - ENTRY_BUFFER),
                2,
            )
            limit_price = round(entry * (1.0 - LIMIT_SLIPPAGE_CAP), 2)
            stop_ref = (
                max(box_high, float(gmma["slow_max"]))
                if bool(gmma["price_on_or_below_slow"])
                else box_high
            )
            raw_stop = stop_ref * (1.0 + STOP_BUFFER)
            min_allowed_stop = entry * (1.0 + MIN_BTST_STOP_PCT)
            stop = round(max(raw_stop, min_allowed_stop), 2)
            risk_pts = max(stop - entry, entry * 0.01)
            target_1 = round(entry - 2.5 * risk_pts, 2)
            target_2 = round(entry - 4.5 * risk_pts, 2)

        risk_pct = round(risk_pts / entry * 100.0, 2) if entry > 0 else 0.0

        return Signal(
            symbol=symbol,
            strategy_name=self.metadata.name,
            timestamp=now,
            price=last_close,
            metadata={
                "timeframe": timeframe,
                "direction": direction,
                "setup_type": setup_type,
                "score": score,
                "band": band,
                "score_compression": round(score_a, 1),
                "score_absorption": round(score_b, 1),
                "score_timing": round(score_c, 1),
                "score_daily": round(score_d, 1),
                "entry": entry,
                "trigger_price": entry,
                "limit_price": limit_price,
                "stop": stop,
                "target_1": target_1,
                "target_2": target_2,
                "pilot_qty_pct": 40,
                "breakout_qty_pct": 60,
                "risk_pct": risk_pct,
                "box_range_pct": round(box_range_pct * 100.0, 2),
                "box_position_pct": round(box_position * 100.0, 1),
                "thin_density_pct": round(thin_density * 100.0, 0),
                "squeeze_ratio": round(squeeze_ratio, 2),
                "gmma_fast_above_slow": bool(gmma["fast_above_slow"]),
                "gmma_fast_spread_pct": round(
                    float(gmma["fast_spread_pct"]) * 100.0, 2
                ),
                "gmma_total_spread_pct": round(
                    float(gmma["total_spread_pct"]) * 100.0, 2
                ),
                "gmma_slow_slope_pct": round(
                    float(gmma["slow_slope_pct"]) * 100.0, 2
                ),
                "morning_step_pct": round(
                    float(gmma["morning_step_pct"]) * 100.0, 2
                ),
                "vol_mult": round(vol_mult, 2),
                "par20": round(par20, 2),
                "wick_ratio": round(wick_ratio, 2),
                "rsi9": round(rsi9, 1),
                "rsi9_above_slow": bool(rsi9_ema3 >= rsi9_sma21),
                "supertrend_dir": int(st_direction),
                "supertrend": round(st_value, 2),
                "above_vwap": vwap_aligned,
                "daily_data": daily is not None,
                **daily_detail,
                "compression_bars": box_bars,
                "expected_move_date": move_date,
            },
        )


# Registry hook: the strategy framework discovers this module.
strategy_class = GuppyCompressionStrategy
