"""Strategy 4: Multi-Timeframe Doji (MTD).

Detects Doji candles (indecision) across four timeframes - 10-minute, daily,
weekly and monthly - and ranks the strength of each signal with a 0-100
confluence score.

Doji geometry (per the strategy spec):

* R = High - Low          (total range)
* B = |Close - Open|      (real body)
* U = High - max(O, C)    (upper wick)
* L = min(O, C) - Low     (lower wick)
* ATR14 = Wilder's 14-period Average True Range

Base Doji conditions (both required):

1. R > 0 and B / R <= 0.10  (body at most 10% of the range)
2. R >= 0.45 * ATR14        (real range; filters dead/illiquid candles)

Subtypes:

* Dragonfly Doji  (bullish-leaning): L/R >= 0.60 and U/R <= 0.15
* Gravestone Doji (bearish-leaning): U/R >= 0.60 and L/R <= 0.15
* Long-Legged Doji: U/R >= 0.35, L/R >= 0.35 and R >= 1.0 * ATR14
* Standard Doji: any other candle meeting the base conditions

Weekly and monthly candles are built from daily data in memory (never fetched
separately). Week-end / month-end detection uses the NSE trading calendar.

Geometry is evaluated on the last *fully closed* candle of each timeframe: the
in-progress 10-minute bar (and the 5-minute 15:25 auction stub) and today's
partial daily candle are ignored, so a signal never flickers while its candle
is still forming. Signals carry the matched candle's own timestamp. Weekly and
monthly are evaluated (forming, on the last trading day of the period) only on
their gated days, per the strategy spec.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.core.logging import get_logger
from app.core.market import is_last_trading_day_of_month, is_last_trading_day_of_week
from app.strategies.base import Category, Signal, Strategy, StrategyMetadata
from app.strategies.indicators import atr, closed_candles, resample, rsi, volume_ratio

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Base Doji thresholds.
MAX_BODY_RATIO = 0.10
MIN_RANGE_ATR = 0.45

# Subtype thresholds.
DRAGONFLY_LOWER_WICK = 0.60
DRAGONFLY_UPPER_WICK = 0.15
GRAVESTONE_UPPER_WICK = 0.60
GRAVESTONE_LOWER_WICK = 0.15
LONG_LEGGED_WICK = 0.35
LONG_LEGGED_MIN_RANGE_ATR = 1.0

# Confluence tuning (optional, not hard filters).
RSI_EXTREME_LOW = 30.0
RSI_EXTREME_HIGH = 70.0
VOLUME_THRESHOLD = 1.5
ALL_TIMEFRAMES = ["10m", "daily", "weekly", "monthly"]


def classify_doji(candle: pd.Series, atr_value: float) -> str | None:
    """Return the Doji subtype for one candle, or ``None`` if it is not a Doji."""
    o = float(candle["open"])
    h = float(candle["high"])
    low = float(candle["low"])
    c = float(candle["close"])

    rng = h - low
    if rng <= 0:
        return None

    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - low

    if body / rng > MAX_BODY_RATIO:
        return None
    if rng < MIN_RANGE_ATR * atr_value:
        return None

    if lower / rng >= DRAGONFLY_LOWER_WICK and upper / rng <= DRAGONFLY_UPPER_WICK:
        return "dragonfly"
    if upper / rng >= GRAVESTONE_UPPER_WICK and lower / rng <= GRAVESTONE_LOWER_WICK:
        return "gravestone"
    if (
        upper / rng >= LONG_LEGGED_WICK
        and lower / rng >= LONG_LEGGED_WICK
        and rng >= LONG_LEGGED_MIN_RANGE_ATR * atr_value
    ):
        return "long-legged"
    return "standard"


def _last_candle(df: pd.DataFrame | None) -> pd.Series | None:
    if df is None or df.empty:
        return None
    return df.iloc[-1]


def _build_higher_timeframes(daily: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Build weekly and monthly candles from daily data."""
    if daily is None or daily.empty:
        return {"weekly": pd.DataFrame(), "monthly": pd.DataFrame()}
    return {"weekly": resample(daily, "W"), "monthly": resample(daily, "ME")}


class MultiTimeframeDojiStrategy(Strategy):
    """Multi-Timeframe Doji engine (Nifty 500)."""

    def __init__(self) -> None:
        self._today = datetime.now(IST).date()
        self._is_week_end = is_last_trading_day_of_week(self._today)
        self._is_month_end = is_last_trading_day_of_month(self._today)

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Multi-Timeframe Doji",
            description=(
                "Doji detected on 10-min, daily, weekly and monthly timeframes, "
                "ranked by a 0-100 confluence score."
            ),
            category=Category.INTRADAY,
            universe="NIFTY_500",
            # Paused on prod (2026-11-04): the full-universe 10-minute scan
            # needs ~1,000 history calls and cannot keep up with a 10-minute
            # cadence on the current FYERS plan. Manual/ad-hoc only for now.
            schedule_type="manual",
            timeframes=["10m", "daily"],
            candle_interval="10m",
        )

    def scan(
        self,
        data: dict[str, dict[str, pd.DataFrame]],
        settings: Settings,
    ) -> list[Signal]:
        signals: list[Signal] = []
        now = datetime.now(IST)

        for symbol, frames in data.items():
            frames = frames or {}
            daily = frames.get("daily")
            if daily is None or daily.empty:
                continue
            daily_closed = closed_candles(daily, "daily", now)
            if daily_closed is None or daily_closed.empty:
                continue

            atr_daily = atr(daily_closed, 14)
            if atr_daily.empty:
                continue

            # Weekly/monthly are built from the raw (forming) daily frame so the
            # week/month being evaluated includes its latest trading day.
            higher = _build_higher_timeframes(daily)
            candidates: dict[str, pd.DataFrame | None] = {
                "10m": closed_candles(frames.get("10m"), "10m", now),
                "daily": daily_closed,
            }
            if self._is_week_end:
                candidates["weekly"] = higher["weekly"]
            if self._is_month_end:
                candidates["monthly"] = higher["monthly"]

            # Evaluate each timeframe once, recording matches.
            matches: list[tuple[str, str, float, pd.Timestamp]] = []
            for tf, df in candidates.items():
                candle = _last_candle(df)
                if candle is None:
                    continue
                atr_value = self._atr_for(tf, df)
                if atr_value <= 0:
                    continue
                subtype = classify_doji(candle, atr_value)
                if subtype is not None:
                    matches.append((tf, subtype, float(candle["close"]), candle.name))

            if not matches:
                continue

            confluence = self._confluence(candidates)
            for tf, subtype, price, candle_time in matches:
                signals.append(
                    Signal(
                        symbol=symbol,
                        strategy_name=self.metadata.name,
                        timestamp=candle_time.to_pydatetime(),
                        price=price,
                        metadata={
                            "timeframe": tf,
                            "subtype": subtype,
                            "confluence_score": confluence,
                        },
                    )
                )
        return signals

    @staticmethod
    def _atr_for(tf: str, df: pd.DataFrame | None) -> float:
        if df is None or df.empty:
            return 0.0
        series = atr(df, 14)
        if series.empty:
            return 0.0
        return float(series.iloc[-1])

    def _confluence(self, candidates: dict[str, pd.DataFrame | None]) -> int:
        """0-100 confluence score.

        Base: 25 points per timeframe with a Doji (max 75 across the four
        timeframes). Bonuses: RSI(14) at an extreme (+10), volume >= 1.5x its
        20-bar average (+10). Capped at 100.
        """
        doji_count = 0
        for tf, df in candidates.items():
            candle = _last_candle(df)
            if candle is None:
                continue
            if classify_doji(candle, self._atr_for(tf, df)) is not None:
                doji_count += 1
        score = int((doji_count / len(ALL_TIMEFRAMES)) * 75)

        daily = candidates.get("daily")
        if daily is not None and not daily.empty:
            rsi_value = rsi(daily, 14).iloc[-1]
            if pd.notna(rsi_value) and (
                rsi_value <= RSI_EXTREME_LOW or rsi_value >= RSI_EXTREME_HIGH
            ):
                score += 10
            if volume_ratio(daily, 20) >= VOLUME_THRESHOLD:
                score += 10
        return min(score, 100)


# Registry hook: the strategy framework discovers this module.
strategy_class = MultiTimeframeDojiStrategy
