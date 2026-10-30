"""Strategy: Open-Equals-Low (OEL).

Core signal: the session's opening price is also its lowest price - price
never traded below the open during the session, so buyers were in control from
the very first trade (e.g. LENSKART 28/10: the day opened at its low after a
gap-down and rallied for the rest of the session).

Two variants (all auto):

* **Intraday (10-minute)** - today's session so far: the 09:15 candle's open
  must still be the lowest price across every completed 10-minute bar.
  Evaluated only after one hour of trading (10:15 IST), because at 09:25 the
  day's low is trivially the open for almost every stock. Once the continuous
  session ends, the completed daily candle takes over.
* **Daily (EOD)** - the most recent COMPLETED daily candle: open == low.
  One signal per symbol per completed candle.

Open == low is the only gate; every match is kept. Matches are then ranked by
a volume-led quality score so strong opening demand sorts to the top and thin
matches sort to the bottom:

* **65% opening-volume score** - the 09:15 10-minute bar's volume versus the
  *same* bar over the previous 10 sessions (60% magnitude + 40% percentile
  rank). Comparing like-for-like avoids the intraday volume seasonality that
  makes a 09:15 bar look tiny next to a 14:00 bar.
* **15% move** from the day's open.
* **10% close location** within the day's range.
* **10% opening turnover** (liquidity guard).

The score is 0-100: HIGH >= 70, MEDIUM >= 45, LOW < 45. When 10-minute history
is unavailable the daily volume ratio is used as a fallback. The score never
filters a signal out - it only changes the order.
"""

from __future__ import annotations

import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.core.market import CONTINUOUS_CLOSE
from app.strategies.base import Category, Signal, Strategy, StrategyMetadata
from app.strategies.indicators import closed_candles, volume_ratio

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)

VOLUME_THRESHOLD = 1.5
MIN_MOVE_PCT = 0.5
#: One hour of 10-minute bars before the day's low is meaningful. At 09:25 the
#: first bar's low equals the open for almost every stock, which is noise.
MIN_INTRADAY_BARS = 6
#: Same-bar sessions used for the opening-volume comparison.
VOLUME_LOOKBACK = 10


def _open_equals_low(open_: float, low: float) -> bool:
    """True when open and low agree at 2-decimal precision (chart precision)."""
    return round(float(open_), 2) == round(float(low), 2)


def _move_pct(open_: float, close: float) -> float:
    return (float(close) - float(open_)) / float(open_) * 100 if open_ else 0.0


def _passes_filters(move_pct: float, vol_ratio: float) -> bool:
    return bool(vol_ratio >= VOLUME_THRESHOLD and move_pct >= MIN_MOVE_PCT)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _first_bars(ten_min: pd.DataFrame | None) -> pd.DataFrame:
    """Return the 09:15 10-minute bar for every session in ``ten_min``.

    Only true 09:15 bars are used: a missing opening bar must not be replaced
    by 09:25, because that would understate the opening volume.
    """
    if ten_min is None or ten_min.empty or "volume" not in ten_min.columns:
        return pd.DataFrame()
    idx = ten_min.index
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    frame = ten_min.copy()
    frame.index = idx
    firsts = frame[frame.index.time == MARKET_OPEN].sort_index()
    if firsts.empty:
        return firsts
    # Bad feeds can duplicate the 09:15 stamp; keep the first occurrence.
    return firsts[~firsts.index.duplicated(keep="first")]


def _volume_profile(
    ten_min: pd.DataFrame | None,
    day,
    *,
    lookback: int = VOLUME_LOOKBACK,
) -> dict:
    """Compare the signal day's opening 10m volume with the same bar history.

    Returns ``ratio`` (today / mean of the previous ``lookback`` sessions),
    ``pctile`` (percentage of those sessions today beats), ``vs_prev``
    (today / previous session), ``first_volume``, ``turnover_cr`` and
    ``history_days``. Missing history is returned as ``None``, never guessed.
    """
    empty = {
        "ratio": None,
        "pctile": None,
        "vs_prev": None,
        "first_volume": None,
        "turnover_cr": None,
        "history_days": 0,
    }
    firsts = _first_bars(ten_min)
    if firsts.empty:
        return empty

    day = day.date() if hasattr(day, "date") else day
    today_rows = firsts[firsts.index.date == day]
    if today_rows.empty:
        return empty

    today = today_rows.iloc[0]
    today_vol = float(today["volume"])
    turnover = today_vol * float(today["close"]) / 1e7
    prev = firsts[firsts.index.date < day].tail(lookback)
    prev_vols = prev["volume"].astype(float)
    prev_vols = prev_vols[prev_vols > 0]

    if today_vol <= 0 or prev_vols.empty:
        return {
            **empty,
            "first_volume": today_vol,
            "turnover_cr": turnover,
            "history_days": int(len(prev_vols)),
        }

    avg = float(prev_vols.mean())
    ratio = today_vol / avg if avg > 0 else None
    pctile = (
        100.0
        * (
            float((prev_vols < today_vol).sum())
            + 0.5 * float((prev_vols == today_vol).sum())
        )
        / len(prev_vols)
    )
    last_prev = float(prev_vols.iloc[-1])
    return {
        "ratio": ratio,
        "pctile": pctile,
        "vs_prev": today_vol / last_prev if last_prev > 0 else None,
        "first_volume": today_vol,
        "turnover_cr": turnover,
        "history_days": int(len(prev_vols)),
    }


def _quality_metrics(
    *,
    open_ratio: float | None,
    open_pctile: float | None,
    daily_ratio: float | None,
    move_pct: float,
    close: float,
    low: float,
    high: float,
    turnover_cr: float | None,
) -> dict:
    """Build the 0-100 volume-led quality score used for ranking.

    Volume dominates (65%) plus a 10% liquidity guard; move and close location
    are secondary confirmations. The score never filters a signal out.
    """
    if open_ratio is not None:
        basis = "open_10m"
        ratio = open_ratio
        magnitude = min(1.0, math.log1p(max(ratio, 0.0)) / math.log1p(3.0))
        pctile = 50.0 if open_pctile is None else open_pctile
        vol_score = 100.0 * (0.6 * magnitude + 0.4 * pctile / 100.0)
    elif daily_ratio is not None:
        basis = "daily"
        ratio = daily_ratio
        magnitude = min(1.0, math.log1p(max(ratio, 0.0)) / math.log1p(3.0))
        vol_score = 100.0 * magnitude
    else:
        basis = "none"
        ratio = None
        vol_score = 0.0

    move_score = _clamp(move_pct / 5.0 * 100.0)
    close_score = _clamp((close - low) / (high - low) * 100.0) if high > low else 0.0
    liquidity_score = (
        _clamp(turnover_cr / 1.0 * 100.0) if turnover_cr is not None else 50.0
    )
    score = round(
        _clamp(
            0.65 * vol_score
            + 0.15 * move_score
            + 0.10 * close_score
            + 0.10 * liquidity_score
        ),
        1,
    )
    if basis == "none":
        quality = "UNRATED"
    elif score >= 70:
        quality = "HIGH"
    elif score >= 45:
        quality = "MEDIUM"
    else:
        quality = "LOW"

    return {
        "score": score,
        "quality": quality,
        "volume_basis": basis,
        "volume_ratio": round(ratio, 2) if ratio is not None else 0.0,
        "score_parts": {
            "volume": round(vol_score, 1),
            "move": round(move_score, 1),
            "close": round(close_score, 1),
            "liquidity": round(liquidity_score, 1),
        },
    }


def _volume_metadata(profile: dict) -> dict:
    """Serialize the opening-volume profile for signal metadata/UI."""
    return {
        "open_vol_ratio": (
            round(profile["ratio"], 2) if profile["ratio"] is not None else None
        ),
        "open_vol_pctile": (
            round(profile["pctile"], 1) if profile["pctile"] is not None else None
        ),
        "open_vol_vs_prev": (
            round(profile["vs_prev"], 2) if profile["vs_prev"] is not None else None
        ),
        "open_vol_history_days": profile["history_days"],
        "first_bar_volume": profile["first_volume"],
        "first_bar_turnover_cr": (
            round(profile["turnover_cr"], 2)
            if profile["turnover_cr"] is not None
            else None
        ),
    }


class OpenEqualsLowStrategy(Strategy):
    """Open-Equals-Low engine (live 10-min + daily EOD, all auto)."""

    @staticmethod
    def _is_open_equals_low(candle) -> bool:
        """Candle-based helper kept for tests/external callers."""
        return _open_equals_low(candle["open"], candle["low"])

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Open-Equals-Low",
            description=(
                "Day open price equals day low (price never traded below the "
                "open). Live 10-min: today's low so far; daily EOD: completed "
                "candle. Ranked by opening-volume quality."
            ),
            category=Category.INTRADAY,
            universe="NIFTY_500",
            schedule_type="auto",
            timeframes=["10m", "daily"],
            frequency_minutes=10,
            candle_interval="10m",
        )

    # ------------------------------------------------------------------ scan
    def scan(
        self,
        data: dict[str, dict[str, pd.DataFrame]],
        settings: Settings,
        *,
        now: datetime | None = None,
    ) -> list[Signal]:
        now = now if now is not None else datetime.now(IST)
        if getattr(now, "tzinfo", None) is None:
            now = now.replace(tzinfo=IST)

        signals: list[Signal] = []

        for symbol, frames in data.items():
            frames = frames or {}

            sig = self._scan_intraday(symbol, frames.get("10m"), now)
            if sig is not None:
                signals.append(sig)

            sig = self._scan_daily(
                symbol, frames.get("daily"), frames.get("10m"), now
            )
            if sig is not None:
                signals.append(sig)

        # Rank by quality: high opening volume first, thin matches last.
        signals.sort(
            key=lambda s: (
                -(s.metadata.get("score") or 0.0),
                -(s.metadata.get("open_vol_ratio") or 0.0),
                s.symbol,
            )
        )
        return signals

    # -------------------------------------------------------------- 10-minute
    def _scan_intraday(self, symbol, raw, now) -> Signal | None:
        # The intraday variant is a live-session signal. After the continuous
        # close the completed daily candle is authoritative, so stop here to
        # avoid emitting the same day twice (10m + daily).
        if now.time() >= CONTINUOUS_CLOSE:
            return None

        ten_min = closed_candles(raw, "10m", now)
        if ten_min is None or ten_min.empty:
            return None

        # Work in IST so "today" and 09:15 are evaluated correctly.
        idx = ten_min.index
        idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)

        # Only TODAY's candles. Previously the code used the "most recent date",
        # which at 09:20 is YESTERDAY, so stale signals fired and blocked
        # today's real one.
        today_mask = idx.date == now.date()
        if not today_mask.any():
            return None

        today = ten_min[today_mask]
        first_ts_ist = idx[today_mask][0]

        # The signal is about the FIRST candle of the session (09:15).
        if first_ts_ist.time() != MARKET_OPEN:
            return None

        # Wait for a meaningful sample: at 09:25 the day's low is trivially
        # the open for almost every stock that ticked up in the first bar.
        if len(today) < MIN_INTRADAY_BARS:
            return None

        # The actual criterion: the DAY's open must equal the DAY's low across
        # every completed bar so far - not just the first candle's low.
        first = today.iloc[0]
        day_open = float(first["open"])
        day_low = float(today["low"].min())
        if not _open_equals_low(day_open, day_low):
            return None

        # Report the latest completed close and the move from the day's open,
        # not the stale first-candle close/move.
        ltp = float(today.iloc[-1]["close"])
        day_high = float(today["high"].max())
        move = _move_pct(day_open, ltp)

        profile = _volume_profile(ten_min, now.date())
        metrics = _quality_metrics(
            open_ratio=profile["ratio"],
            open_pctile=profile["pctile"],
            daily_ratio=None,
            move_pct=move,
            close=ltp,
            low=day_low,
            high=day_high,
            turnover_cr=profile["turnover_cr"],
        )

        return Signal(
            symbol=symbol,
            strategy_name=self.metadata.name,
            timestamp=first_ts_ist.to_pydatetime(),
            price=ltp,
            metadata={
                "timeframe": "10m",
                "variant": "intraday",
                "passes_filters": _passes_filters(
                    move, metrics["volume_ratio"]
                ),
                **metrics,
                **_volume_metadata(profile),
                "daily_vol_ratio": None,
                "move_pct": round(move, 2),
                "day_open": round(day_open, 2),
                "day_low": round(day_low, 2),
                "day_high": round(day_high, 2),
                "day_close": round(ltp, 2),
                "ltp": round(ltp, 2),
                "bars_elapsed": len(today),
            },
        )

    # ------------------------------------------------------------------ daily
    def _scan_daily(self, symbol, raw, ten_min, now) -> Signal | None:
        daily = closed_candles(raw, "daily", now)
        if daily is None or daily.empty:
            return None

        candle = daily.iloc[-1]
        if not _open_equals_low(candle["open"], candle["low"]):
            return None

        day_open = float(candle["open"])
        day_low = float(candle["low"])
        day_high = float(candle["high"])
        day_close = float(candle["close"])
        move = _move_pct(day_open, day_close)
        daily_ratio = volume_ratio(daily, 20)
        profile = _volume_profile(ten_min, candle.name)
        daily_turnover = day_close * float(candle["volume"]) / 1e7
        turnover = (
            profile["turnover_cr"]
            if profile["turnover_cr"] is not None
            else daily_turnover
        )

        metrics = _quality_metrics(
            open_ratio=profile["ratio"],
            open_pctile=profile["pctile"],
            daily_ratio=daily_ratio,
            move_pct=move,
            close=day_close,
            low=day_low,
            high=day_high,
            turnover_cr=turnover,
        )

        return Signal(
            symbol=symbol,
            strategy_name=self.metadata.name,
            timestamp=candle.name.to_pydatetime(),
            price=day_close,
            metadata={
                "timeframe": "daily",
                "variant": "eod",
                "passes_filters": _passes_filters(
                    move, metrics["volume_ratio"]
                ),
                **metrics,
                **_volume_metadata(profile),
                "daily_vol_ratio": round(daily_ratio, 2),
                "daily_turnover_cr": round(daily_turnover, 2),
                "move_pct": round(move, 2),
                "day_open": round(day_open, 2),
                "day_low": round(day_low, 2),
                "day_high": round(day_high, 2),
                "day_close": round(day_close, 2),
                "ltp": round(day_close, 2),
            },
        )


strategy_class = OpenEqualsLowStrategy
