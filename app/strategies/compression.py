"""Strategy 5: EOD Compression & Pipe Ignition Breakout ("day-before & live explosion") - v3.

Drop-in replacement for compression.py. Preserves all framework hooks,
imports, and Signal metadata keys while fixing the 6 recall bottlenecks
identified across empirical NSE winners (HBLENGINE, AARTIDRUGS, PATANJALI,
SHOPERSTOP, TEAMLEASE, AEGISVOPAK, INFY, HINDPETRO, KOTAKBANK, ABDL):

1. Dual-Path Bollinger Squeeze Gate: Passes if bandwidth <= 65% of rolling
   median OR if absolute bandwidth is already ultra-tight (<= 1.85% on 10m,
   <= 3.20% on 1h), preventing multi-day squeezes from self-rejecting.
2. Safe Rolling Lookbacks: Lowered SQUEEZE_MIN_PERIODS (40 on 10m, 10 on 1h)
   so standard intraday fetch windows never return NaN median_width.
3. Shrinking-ATR & Micro-Tick Fix: Measures thin bars / Dojis against a
   50-bar baseline ATR, candle body-to-range ratio (<= 0.22), OR price
   percentage (<= 0.18% of close) so prolonged pipes don't penalize themselves.
4. Three Actionable Detection Modes (`setup_type` in metadata):
   - "EOD_COIL": Currently coiled inside the flat box (BTST pre-breakout).
   - "BOX_IGNITION": Prior 15 bars coiled in a flat box and current bar
     just broke above the box ceiling with volume & RSI(9) momentum.
   - "SPRING_RECLAIM": Post-coil morning shakeout dip (like PATANJALI) that
     just flipped Supertrend bullish or reclaimed the box midline.
5. Shakeout-Proof Stop Loss: Adds structural ATR(50) / 1.5% breathing room
   on EOD_COIL trades to survive morning stop-hunts, plus Target 1 & Target 2.
6. Multi-Timeframe Deduplication & Bonus: Evaluates both 10m and resampled 1h;
   if both pass for the same symbol, keeps the stronger signal and awards a
   +5.0 pt MTF Confluence Bonus.
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

# -- compression box --------------------------------------------------------
#: Bars in the box per timeframe: 15 x 10m = 2.5h, 3 x 1h = 3h.
BOX_BARS = {"10m": 15, "1h": 3}
#: Hard gate: box height as a fraction of the last close (2.2% on 10m, 3.0% on 1h).
MAX_BOX_RANGE_PCT = 0.022
MAX_BOX_RANGE_BY_TF = {"10m": 0.022, "1h": 0.030}

# -- Bollinger squeeze ------------------------------------------------------
BOLLINGER_PERIOD = 20
BOLLINGER_NUM_STD = 2.0
#: Rolling window for the median bandwidth (~10 sessions per timeframe).
SQUEEZE_LOOKBACK = {"10m": 370, "1h": 60}
#: Lowered min_periods so 60-140 bar intraday histories never fail with NaN.
SQUEEZE_MIN_PERIODS = {"10m": 40, "1h": 10}
#: Relative squeeze threshold: current bandwidth <= 65% of rolling median.
SQUEEZE_MAX_RATIO = 0.65
#: Absolute bandwidth fallback (fraction of middle band): passes multi-day pipes
#: even when the rolling median has already been dragged down by prior flat days.
ABS_SQUEEZE_MAX_BW = {"10m": 0.0185, "1h": 0.0320}

# -- scoring: weights (sum = 100 base + up to 5 MTF bonus, capped at 100) ---
# A. compression quality (24)
A_BOX_POINTS = 10.0
A_BOX_BEST_PCT = 0.006
A_SQUEEZE_POINTS = 8.0
A_SQUEEZE_BEST_RATIO = 0.28
A_THIN_POINTS = 6.0
THIN_BODY_ATR = 0.42
THIN_BODY_PRICE_PCT = 0.0018  # 0.18% of price = visual flat dash on chart
THIN_DOJI_RATIO = 0.22        # Body <= 22% of High-Low range

# B. absorption / accumulation evidence (26)
B_PAR_POINTS = 10.0
PAR_BEST = 1.60
PAR_WORST = 0.90
B_WICK_POINTS = 8.0
WICK_BEST = 1.25
WICK_WORST = 0.85
B_POS_POINTS = 8.0
POS_WORST = 0.30
POS_BEST = 0.90

# C. intraday timing & Zerodha RSI(9) subpanel (26 + 6 ignition bonus)
C_RSI_POINTS = 10.0
RSI9_PERIOD = 9
RSI9_PEAK = 55.0
RSI9_SPAN = 20.0              # Widened to [35..75] so SHOPERSTOP/TEAMLEASE score well
C_RSI_CROSS_POINTS = 6.0
C_RSI_CURL_POINTS = 4.0       # Partial credit when EMA3 is curling up toward SMA21
C_ST_POINTS = 6.0
C_ST_NEAR_POINTS = 4.5        # AEGISVOPAK fix: coiled just below red ST flip line
ST_NEAR_PCT = 0.018
C_VWAP_POINTS = 4.0
VWAP_BARS = 25
#: Cap for score C = 26 base + 6 live-ignition volume bonus.
C_MAX_POINTS = 26.0 + 6.0

# D. daily context & narrow-range pedestal (24)
D_TREND_POINTS = 6.0
D_TREND_PARTIAL_POINTS = 3.5
D_52W_POINTS = 6.0
D_52W_BEST = 0.03
D_52W_WORST = 0.30
D_52W_BARS = 250
D_BASE_POINTS = 6.0
D_BASE_MAX_DAYS = 3
BASE_RANGE_ATR = 0.75         # Catches NR4/NR7 compression days reliably
D_LIQ_POINTS = 6.0
LIQ_BEST_CR = 20.0
LIQ_WORST_CR = 1.5
LIQ_DAYS = 20

# Bonus points for MTF (10m + 1h) confluence or live volume ignition
MTF_CONFLUENCE_BONUS = 5.0
IGNITION_VOL_BONUS = 6.0

# -- bands ------------------------------------------------------------------
BAND_BUY = "BUY"
BAND_HOLD = "HOLD"
BAND_SKIP = "SKIP"
BAND_BUY_MIN = 68.0
BAND_HOLD_MIN = 45.0

# -- confluence indicators --------------------------------------------------
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0

# -- prefilter / data -------------------------------------------------------
#: Raised to 5.5% so stocks that stepped up in the morning and coiled flat all
#: afternoon (staircase pipes like INFY, HINDPETRO, KOTAKBANK) are never dropped.
PREFILTER_DAY_RANGE_PCT = 5.5
#: Minimum history before a timeframe can be evaluated.
MIN_BARS = {"10m": 45, "1h": 14}
#: Minimum completed daily bars before the daily context is trusted.
MIN_DAILY_BARS = 40
#: 10-minute bars per full hour (NSE session starts at 09:15).
TEN_MIN_BARS_PER_HOUR = 6
#: Entry/stop buffers around the box edges (fraction).
ENTRY_BUFFER = 0.001
STOP_BUFFER = 0.003
#: Minimum stop distance for BTST EOD_COIL to survive morning stop-hunts (PATANJALI).
MIN_BTST_STOP_PCT = 0.012


def hourly_from_10m(df: pd.DataFrame) -> pd.DataFrame | None:
    """Build NSE-aligned 1-hour candles from 10-minute data.

    Bins start at 09:15; the partial current hour and the 15-minute closing
    stub (fewer than six 10-minute bars) are dropped. Returns ``None`` when no
    full hourly bar survives.
    """
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
    """Linear 0..1 ramp from ``worst`` to ``best`` (either direction)."""
    if worst == best:
        return 0.0
    fraction = (value - worst) / (best - worst)
    return max(0.0, min(1.0, fraction))


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


def _daily_scores(daily: pd.DataFrame, close: float) -> tuple[float, dict]:
    """Daily-context score (24 pts max) plus the underlying measurements."""
    days = daily["close"].ewm(span=20, adjust=False).mean()
    slow = daily["close"].ewm(span=50, adjust=False).mean()
    ema20, ema50 = float(days.iloc[-1]), float(slow.iloc[-1])

    if close > ema20 and ema20 > ema50:
        trend_points = D_TREND_POINTS
    elif close >= ema20 * 0.985:
        trend_points = D_TREND_PARTIAL_POINTS
    else:
        trend_points = 0.0

    high_52w = float(daily["high"].tail(D_52W_BARS).max())
    dist_52w = (high_52w - close) / high_52w if high_52w > 0 else 1.0
    high_points = D_52W_POINTS * _ramp(dist_52w, D_52W_WORST, D_52W_BEST)

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

    # Also award base points if the stock had a strong prior pedestal impulse
    # (+3.5% day in the last 10 sessions) even if the current base is short.
    recent_10d_rets = daily["close"].pct_change().tail(10)
    had_pedestal_impulse = bool((recent_10d_rets >= 0.035).any())
    effective_base_days = max(base_days, 2 if had_pedestal_impulse else 0)
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
        "pedestal_impulse": had_pedestal_impulse,
        "turnover_cr": round(turnover_cr, 1),
    }
    return trend_points + high_points + base_points + liq_points, detail


class EodCompressionBreakoutStrategy(Strategy):
    """EOD compression & live pipe-ignition scan over the Nifty 500 (10m + 1h)."""

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="EOD Compression Breakout",
            description=(
                "High-recall EOD & intraday compression scanner: captures flat "
                "institutional accumulation pipes (EOD_COIL), live pipe breakouts "
                "(BOX_IGNITION), and post-coil shakeout flips (SPRING_RECLAIM)."
            ),
            category=Category.INTRADAY,
            universe="NIFTY_500",
            schedule_type="auto",
            timeframes=["10m", "daily"],
            run_at="15:20",
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

            # Dedup: both 10m and 1h fired -> keep the stronger signal and
            # award the +5 pt MTF confluence bonus (tracked as score_bonus so
            # the score components still add up to the total).
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

    # -- evaluation ---------------------------------------------------------
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
        if len(df) < max(MIN_BARS[timeframe], box_bars + 5):
            return None

        last_close = float(df["close"].iloc[-1])
        last_open = float(df["open"].iloc[-1])
        if last_close <= 0:
            return None

        max_box_pct = MAX_BOX_RANGE_BY_TF.get(timeframe, MAX_BOX_RANGE_PCT)

        # -- Bollinger bandwidth series (used across all 3 setup states) ----
        bb = bollinger(df, BOLLINGER_PERIOD, BOLLINGER_NUM_STD)
        bandwidth = bb["bandwidth"]
        # Normalize bandwidth to a fraction of price if the indicator returns
        # percent (e.g. 1.8 vs 0.018).
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

        # ===================================================================
        # DETECT WHICH OF THE 3 SETUP STATES APPLIES:
        #   1. EOD_COIL: Last N bars are inside the tight box.
        #   2. BOX_IGNITION: Prior N bars (shifted by 1 bar) were inside a
        #      tight box, and the current bar just broke ABOVE that ceiling.
        #   3. SPRING_RECLAIM: Prior box coiled, dipped slightly (<= 1.8%),
        #      and the current green bar just flipped Supertrend / reclaimed.
        # ===================================================================
        setup_type = None
        box = df.iloc[-box_bars:]
        box_high = float(box["high"].max())
        box_low = float(box["low"].min())
        box_range = box_high - box_low
        box_range_pct = box_range / last_close if box_range > 0 else 999.0
        ref_bw = float(bw_frac_series.iloc[-1])
        squeeze_ratio = ref_bw / float(median_width)

        abs_bw_limit = ABS_SQUEEZE_MAX_BW.get(timeframe, 0.020)
        is_squeezed_now = (squeeze_ratio <= SQUEEZE_MAX_RATIO) or (ref_bw <= abs_bw_limit)

        if 0 < box_range_pct <= max_box_pct and is_squeezed_now:
            setup_type = "EOD_COIL"
        else:
            # Check if the PRIOR box (1 bar ago) was tightly coiled and JUST ignited!
            prior_box = df.iloc[-(box_bars + 1) : -1]
            p_high = float(prior_box["high"].max())
            p_low = float(prior_box["low"].min())
            p_range = p_high - p_low
            p_range_pct = p_range / last_close if p_range > 0 else 999.0
            p_bw = float(bw_frac_series.iloc[-2]) if len(bw_frac_series) >= 2 else ref_bw
            p_sq_ratio = p_bw / float(median_width)
            was_squeezed_prior = (p_sq_ratio <= SQUEEZE_MAX_RATIO) or (p_bw <= abs_bw_limit)

            if (
                0 < p_range_pct <= max_box_pct
                and was_squeezed_prior
                and last_close > p_high
                and last_close > last_open
            ):
                setup_type = "BOX_IGNITION"
                box = prior_box
                box_high, box_low = p_high, p_low
                box_range, box_range_pct = p_range, p_range_pct
                squeeze_ratio = p_sq_ratio
            elif timeframe == "10m" and len(df) >= box_bars + 6:
                # State 3: Morning Shakeout Spring (e.g. PATANJALI).
                # The box 4 bars ago was coiled, price dipped <= 1.8% below it,
                # and the current bar is a green reclaim above the box midline.
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
                    box = spring_box
                    box_high = s_high
                    box_low = min(s_low, recent_dip_low)
                    box_range = box_high - box_low
                    box_range_pct = s_range_pct

        if setup_type is None or box_range <= 0:
            return None

        # -- score A: compression quality (fixes shrinking ATR_14 bug) ------
        a_box = A_BOX_POINTS * _ramp(box_range_pct, max_box_pct, A_BOX_BEST_PCT)
        a_squeeze = A_SQUEEZE_POINTS * _ramp(
            min(squeeze_ratio, ref_bw / abs_bw_limit * 0.40),
            SQUEEZE_MAX_RATIO,
            A_SQUEEZE_BEST_RATIO,
        )
        # 50-bar baseline ATR + price % + body/range doji test, so prolonged
        # pipes are still recognised as thin bars instead of self-penalising.
        atr50_series = atr(df, min(50, len(df) - 2)).reindex(box.index)
        bodies = (box["close"] - box["open"]).abs()
        candle_ranges = (box["high"] - box["low"]).replace(0.0, float("nan"))
        is_thin_atr = (bodies <= THIN_BODY_ATR * atr50_series) & atr50_series.notna()
        is_thin_pct = (bodies / box["close"]) <= THIN_BODY_PRICE_PCT
        is_doji_bar = (bodies / candle_ranges).fillna(0.0) <= THIN_DOJI_RATIO
        thin = is_thin_atr | is_thin_pct | is_doji_bar
        thin_density = float(thin.mean()) if len(thin) else 0.0
        a_thin = A_THIN_POINTS * thin_density
        score_a = a_box + a_squeeze + a_thin

        # -- score B: absorption / accumulation evidence -------------------
        last20 = df.iloc[-20:]
        bar_mid = (last20["high"] + last20["low"]) / 2.0
        up_bars = (last20["close"] >= last20["open"]) | (last20["close"] >= bar_mid)
        up_volume = float(last20.loc[up_bars, "volume"].sum())
        down_volume = float(last20.loc[~up_bars, "volume"].sum())
        par20 = up_volume / down_volume if down_volume > 0 else 3.0
        b_par = B_PAR_POINTS * _ramp(par20, PAR_WORST, PAR_BEST)

        lower_wick = (box[["open", "close"]].min(axis=1) - box["low"]).mean()
        upper_wick = (box["high"] - box[["open", "close"]].max(axis=1)).mean()
        wick_ratio = float(lower_wick / upper_wick) if upper_wick > 0 else 3.0
        b_wick = B_WICK_POINTS * _ramp(wick_ratio, WICK_WORST, WICK_BEST)

        box_position = max(0.0, min(1.0, (last_close - box_low) / box_range))
        b_pos = B_POS_POINTS * _ramp(box_position, POS_WORST, POS_BEST)
        score_b = b_par + b_wick + b_pos

        # -- score C: intraday timing (Zerodha RSI(9) + EMA3/SMA21 + ST) ---
        rsi9_series = rsi(df, RSI9_PERIOD)
        rsi9 = float(rsi9_series.iloc[-1])
        rsi9_ema3_series = rsi9_series.ewm(span=3, adjust=False).mean()
        rsi9_ema3 = float(rsi9_ema3_series.iloc[-1])
        rsi9_ema3_prev = (
            float(rsi9_ema3_series.iloc[-2]) if len(rsi9_ema3_series) >= 2 else rsi9_ema3
        )
        rsi9_sma21 = float(rsi9_series.rolling(21, min_periods=5).mean().iloc[-1])
        if pd.isna(rsi9) or pd.isna(rsi9_sma21):
            return None

        c_rsi = C_RSI_POINTS * max(0.0, 1.0 - abs(rsi9 - RSI9_PEAK) / RSI9_SPAN)
        if rsi9_ema3 >= rsi9_sma21:
            c_cross = C_RSI_CROSS_POINTS
        elif (rsi9_ema3 >= rsi9_sma21 - 3.5) and (rsi9_ema3 > rsi9_ema3_prev):
            # Curling upward right inside the coil (SHOPERSTOP / TEAMLEASE / PATANJALI)
            c_cross = C_RSI_CURL_POINTS
        else:
            c_cross = 0.0

        st = supertrend(df, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)
        st_direction = float(st["direction"].iloc[-1])
        st_value = float(st["supertrend"].iloc[-1]) if not pd.isna(st.iloc[-1, 0]) else 0.0
        if st_direction == 1.0:
            c_st = C_ST_POINTS
        elif st_value > 0 and abs(last_close - st_value) / last_close <= ST_NEAR_PCT:
            c_st = C_ST_NEAR_POINTS
        else:
            c_st = 0.0

        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        vwap_series = (typical * df["volume"]).rolling(VWAP_BARS).sum() / df[
            "volume"
        ].rolling(VWAP_BARS).sum().replace(0.0, float("nan"))
        vwap = float(vwap_series.iloc[-1]) if not pd.isna(vwap_series.iloc[-1]) else 0.0
        above_vwap = bool(vwap and last_close >= vwap * 0.998)
        c_vwap = C_VWAP_POINTS if above_vwap else 0.0

        # Live-ignition bonus: the breakout bar carries >= 1.5x the prior
        # 20-bar average volume (Turtle-style confirmation).
        vol_sma20 = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else 0.0
        vol_mult = float(df["volume"].iloc[-1] / vol_sma20) if vol_sma20 > 0 else 1.0
        ignition_bonus = (
            IGNITION_VOL_BONUS
            if (setup_type in ("BOX_IGNITION", "SPRING_RECLAIM") and vol_mult >= 1.5)
            else 0.0
        )

        score_c = min(C_MAX_POINTS, c_rsi + c_cross + c_st + c_vwap + ignition_bonus)

        # -- score D: daily context ----------------------------------------
        daily_points, daily_detail = 0.0, {}
        if daily is not None:
            daily_points, daily_detail = _daily_scores(daily, last_close)
        score_d = daily_points

        score = min(100.0, round(score_a + score_b + score_c + score_d, 1))
        if score >= BAND_BUY_MIN:
            band = BAND_BUY
        elif score >= BAND_HOLD_MIN:
            band = BAND_HOLD
        else:
            band = BAND_SKIP

        # -- actionable levels (with shakeout-proof stop & targets) ---------
        if setup_type == "BOX_IGNITION":
            entry = round(last_close, 2)
        else:
            entry = round(box_high * (1.0 + ENTRY_BUFFER), 2)

        stop_ref = box_low
        if st_direction == 1.0 and box_low <= st_value <= last_close:
            stop_ref = max(box_low, st_value)

        # Ensure the stop has at least MIN_BTST_STOP_PCT breathing room so a
        # morning stop-hunt inside the box doesn't exit a valid BTST coil.
        raw_stop = stop_ref * (1.0 - STOP_BUFFER)
        max_allowed_stop = entry * (1.0 - MIN_BTST_STOP_PCT)
        stop = round(min(raw_stop, max_allowed_stop), 2)

        risk_pts = max(entry - stop, entry * 0.01)
        risk_pct = round((entry - stop) / entry * 100.0, 2) if entry > 0 else 0.0
        target_1 = round(entry + 2.5 * risk_pts, 2)
        target_2 = round(entry + 4.5 * risk_pts, 2)

        return Signal(
            symbol=symbol,
            strategy_name=self.metadata.name,
            timestamp=now,
            price=last_close,
            metadata={
                "timeframe": timeframe,
                "setup_type": setup_type,
                "score": score,
                "band": band,
                "score_compression": round(score_a, 1),
                "score_absorption": round(score_b, 1),
                "score_timing": round(score_c, 1),
                "score_daily": round(score_d, 1),
                "entry": entry,
                "stop": stop,
                "target_1": target_1,
                "target_2": target_2,
                "risk_pct": risk_pct,
                "box_range_pct": round(box_range_pct * 100.0, 2),
                "box_position_pct": round(box_position * 100.0, 1),
                "thin_density_pct": round(thin_density * 100.0, 0),
                "squeeze_ratio": round(squeeze_ratio, 2),
                "vol_mult": round(vol_mult, 2),
                "par20": round(par20, 2),
                "wick_ratio": round(wick_ratio, 2),
                "rsi9": round(rsi9, 1),
                "rsi9_above_slow": bool(rsi9_ema3 >= rsi9_sma21),
                "supertrend_dir": int(st_direction),
                "supertrend": round(st_value, 2),
                "above_vwap": above_vwap,
                "daily_data": daily is not None,
                **daily_detail,
                "compression_bars": box_bars,
                "expected_move_date": move_date,
            },
        )


# Registry hook: the strategy framework discovers this module.
strategy_class = EodCompressionBreakoutStrategy
