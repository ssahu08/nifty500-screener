"""Strategy: Open-Equals-Low (OEL).

Core signal: the session's opening price is also its lowest price - price
never traded below the open during the session, so buyers were in control from
the very first trade (e.g. LENSKART 28/10: the day opened at its low after a
gap-down and rallied for the rest of the session).

Four variants, split across two daily runs:

* **First candle (10-minute)** - runs once at 09:25, after the 09:15-09:25
  candle has completed. The signal is that first candle's open == low: price
  never traded below the open during the opening bar. This is a first-candle
  candidate, so a later dip below the open does not invalidate it.
* **Daily (EOD)** - runs at 15:35 on the most recent COMPLETED daily candle:
  open == low. One signal per symbol per completed candle.
* **Weekly** - the last completed weekly candle before the current week:
  open == low. Built by resampling the daily frame to ``W-FRI``.
* **Monthly** - the last completed monthly candle before the current month:
  open == low. Built by resampling the daily frame to ``ME``.

The weekly/monthly period advances on the first run of the new week/month, not
at Friday's or month-end's close: the daily cache can still hold a partial
current-day bar fetched earlier that afternoon. Waiting for the next period's
first run means the prior period's daily bars are refreshed with final values.

Weekly/monthly signals are cached per completed-period label and re-emitted on
every scan, so the quote prefilter (which only keeps today's open == low
symbols) cannot make them disappear between period boundaries.

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
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.core.market import CONTINUOUS_CLOSE, is_trading_day
from app.data.provider import Quote, normalize_symbol
from app.strategies.base import Category, Signal, Strategy, StrategyMetadata
from app.strategies.indicators import closed_candles, resample, volume_ratio

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)

VOLUME_THRESHOLD = 1.5
MIN_MOVE_PCT = 0.5
#: Same-bar sessions used for the opening-volume comparison.
VOLUME_LOOKBACK = 10
#: Higher-timeframe resampling rules (daily -> weekly/monthly).
WEEKLY_RULE = "W-FRI"
MONTHLY_RULE = "ME"
#: Volume lookback per higher timeframe, in that timeframe's own candles.
PERIOD_VOLUME_LOOKBACK = {"weekly": 20, "monthly": 12}


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


def _latest_completed_week_end(now: datetime) -> date:
    """Last trading day of the ISO week before the one containing ``now``.

    The period advances on the **first run of a new week**, not at Friday's
    close. This matters because the daily cache can still hold a partial
    current-day bar fetched earlier that afternoon (FYERS returns the day so
    far). Waiting until the new week's first run guarantees the previous
    week's daily candles were refreshed with their final values.
    """
    monday = now.date() - timedelta(days=now.date().weekday())
    cursor = monday - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _latest_completed_month_end(now: datetime) -> date:
    """Last trading day of the calendar month before the one containing ``now``.

    Like the weekly helper, the period advances on the first run of the new
    month so the prior month's daily candles have settled in the cache.
    """
    cursor = now.date().replace(day=1) - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _week_label(day: date) -> date:
    """The Friday that labels the ISO week containing ``day``."""
    return day + timedelta(days=4 - day.weekday())


def _month_label(day: date) -> date:
    """The calendar month-end that labels the month containing ``day``."""
    nxt = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def _period_timestamp(frame: pd.DataFrame, label: date) -> pd.Timestamp:
    """Timezone-aware 00:00 label used by a resampled period frame."""
    ts = pd.Timestamp(label)
    tz = getattr(frame.index, "tz", None)
    return ts.tz_localize(tz) if tz is not None else ts


def _period_candle(frame: pd.DataFrame, label: date) -> pd.Series | None:
    """Return the resampled candle whose period label is ``label``."""
    if frame is None or frame.empty:
        return None
    ts = _period_timestamp(frame, label)
    if ts in frame.index:
        return frame.loc[ts]
    matches = frame[frame.index.date == label]
    return matches.iloc[-1] if not matches.empty else None


class OpenEqualsLowStrategy(Strategy):
    """Open-Equals-Low engine (live 10-min + daily/weekly/monthly, all auto)."""

    def __init__(self) -> None:
        #: variant -> (completed-period label, signals). Higher-timeframe
        #: signals are computed once per completed period and re-emitted on
        #: every scan; the quote prefilter only keeps today's matches, so
        #: without this cache they would vanish on the next run.
        self._htf_cache: dict[str, tuple[str, list[Signal]]] = {}
        #: Period keys captured by :meth:`prefilter` so :meth:`scan` makes the
        #: same refresh decision within one run.
        self._pending_keys: dict[str, str] | None = None

    @staticmethod
    def _is_open_equals_low(candle) -> bool:
        """Candle-based helper kept for tests/external callers."""
        return _open_equals_low(candle["open"], candle["low"])

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Open-Equals-Low",
            description=(
                "Open price equals low (price never traded below the open). "
                "Live 10-min: today's low so far; daily EOD, weekly and monthly: "
                "latest completed candle. Ranked by volume-led quality."
            ),
            category=Category.INTRADAY,
            universe="NIFTY_500",
            schedule_type="auto",
            timeframes=["10m", "daily"],
            run_at="09:25",          # first 10m candle has just completed
            extra_run_at=["15:35"],  # daily/weekly/monthly variants
            candle_interval="10m",
        )

    def required_timeframes(self, now: datetime | None = None) -> list[str]:
        """Only fetch what the current slot needs.

        The 09:25 slot evaluates the opening 10-minute candle, so daily history
        would add a full-universe fetch before the first signal. The 15:35 slot
        evaluates daily/weekly/monthly candles and does not need 10m history.
        """
        now = now if now is not None else datetime.now(IST)
        if getattr(now, "tzinfo", None) is None:
            now = now.replace(tzinfo=IST)
        return ["daily"] if now.time() >= CONTINUOUS_CLOSE else ["10m"]

    # -------------------------------------------------------------- prefilter
    def prefilter(
        self,
        quotes: dict[str, Quote],
        settings: Settings,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Keep only symbols whose day open is still the day low.

        The batched quote already carries today's open and low, which is the
        entire OEL gate. Fetching 10-minute and daily history for all ~500
        symbols otherwise burns the whole Fyers quota and makes a scan take
        ~25 minutes. Symbols with a missing/incomplete quote are kept so a data
        hiccup can never hide a signal.

        At 09:25 the day's low *is* the opening candle's low, so the quote gate
        is exactly the first-candle rule. The run is scheduled to start on
        time; a much later catch-up run could miss a symbol whose first candle
        matched but whose second candle traded lower.

        At 15:35 the weekly/monthly cache may be stale when a new period has
        completed, and daily candles are then needed for the *whole* universe
        (the quote gate only knows about today). In that case return the full
        universe once; later runs go back to the cheap quote prefilter.
        """
        now = now if now is not None else datetime.now(IST)
        if getattr(now, "tzinfo", None) is None:
            now = now.replace(tzinfo=IST)

        if now.time() < CONTINUOUS_CLOSE:
            # First-candle slot: the higher-timeframe refresh belongs to the
            # 15:35 slot and must not force a full-universe 10m fetch here.
            self._pending_keys = None
            return self._quote_gated(quotes, settings)

        keys = self._htf_keys(now)
        self._pending_keys = keys
        if any(self._needs_refresh(variant, keys[variant]) for variant in keys):
            return self.get_universe(settings)
        return self._quote_gated(quotes, settings)

    def _quote_gated(self, quotes: dict[str, Quote], settings: Settings) -> list[str]:
        kept: list[str] = []
        for symbol in self.get_universe(settings):
            quote = quotes.get(normalize_symbol(symbol))
            if quote is None or not quote.open or not quote.low:
                kept.append(symbol)
                continue
            if _open_equals_low(quote.open, quote.low):
                kept.append(symbol)
        return kept

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

        # The 09:25 slot fetches only 10m and the 15:35 slot only daily. Skip
        # the higher-timeframe cache entirely when daily data was not fetched;
        # otherwise an empty "refresh" at 09:25 would suppress the 15:35 run.
        daily_available = any(
            isinstance(frames, dict) and frames.get("daily") is not None
            for frames in data.values()
        )

        # Reuse the keys prefilter decided on so the fetch and the scan agree
        # even if the clock crosses a period boundary between the two calls.
        keys = self._pending_keys or self._htf_keys(now)
        self._pending_keys = None
        refresh = (
            {
                variant: self._needs_refresh(variant, key)
                for variant, key in keys.items()
            }
            if daily_available
            else {}
        )
        fresh: dict[str, list[Signal]] = {
            variant: [] for variant, needed in refresh.items() if needed
        }

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

            for variant in fresh:
                label = date.fromisoformat(keys[variant])
                sig = self._scan_period(
                    symbol, frames.get("daily"), now, variant, label
                )
                if sig is not None:
                    fresh[variant].append(sig)

        # Persist the freshly computed higher-timeframe signals for this period
        # and always re-emit them, so the day-level quote prefilter cannot hide
        # a completed weekly/monthly candle between runs.
        if daily_available:
            for variant, needed in refresh.items():
                if needed:
                    self._htf_cache[variant] = (keys[variant], fresh[variant])
                signals.extend(self._htf_cache[variant][1])

        # Rank by quality: high opening volume first, thin matches last.
        signals.sort(
            key=lambda s: (
                -(s.metadata.get("score") or 0.0),
                -(s.metadata.get("open_vol_ratio") or 0.0),
                s.symbol,
            )
        )
        return signals

    # --------------------------------------------------- higher timeframes
    @staticmethod
    def _htf_keys(now: datetime) -> dict[str, str]:
        """Completed-period labels for the weekly and monthly variants."""
        return {
            "weekly": _week_label(_latest_completed_week_end(now)).isoformat(),
            "monthly": _month_label(_latest_completed_month_end(now)).isoformat(),
        }

    def _needs_refresh(self, variant: str, key: str) -> bool:
        cached = self._htf_cache.get(variant)
        return cached is None or cached[0] != key

    def _scan_period(
        self, symbol, raw, now, variant: str, label: date
    ) -> Signal | None:
        """Evaluate the latest completed weekly or monthly candle."""
        daily = closed_candles(raw, "daily", now)
        if daily is None or daily.empty:
            return None

        if variant == "weekly":
            rule = WEEKLY_RULE
            prefix = "week"
        elif variant == "monthly":
            rule = MONTHLY_RULE
            prefix = "month"
        else:
            return None

        frame = resample(daily, rule)
        candle = _period_candle(frame, label)
        if candle is None or not _open_equals_low(candle["open"], candle["low"]):
            return None

        # Trim to the completed period so the volume ratio compares like with
        # like and never includes the in-progress candle.
        history = frame.loc[: _period_timestamp(frame, label)]
        if history.empty:
            return None

        open_ = float(candle["open"])
        low = float(candle["low"])
        high = float(candle["high"])
        close = float(candle["close"])
        move = _move_pct(open_, close)
        period_ratio = volume_ratio(history, PERIOD_VOLUME_LOOKBACK[variant])
        turnover = close * float(candle["volume"]) / 1e7

        metrics = _quality_metrics(
            open_ratio=None,
            open_pctile=None,
            daily_ratio=period_ratio,
            move_pct=move,
            close=close,
            low=low,
            high=high,
            turnover_cr=turnover,
        )
        metrics["volume_basis"] = variant

        return Signal(
            symbol=symbol,
            strategy_name=self.metadata.name,
            timestamp=candle.name.to_pydatetime(),
            price=close,
            metadata={
                "timeframe": variant,
                "variant": variant,
                "passes_filters": _passes_filters(move, metrics["volume_ratio"]),
                **metrics,
                "period_vol_ratio": round(period_ratio, 2),
                "daily_vol_ratio": None,
                "open_vol_ratio": None,
                "open_vol_pctile": None,
                "open_vol_vs_prev": None,
                "open_vol_history_days": 0,
                "first_bar_volume": None,
                "first_bar_turnover_cr": None,
                "move_pct": round(move, 2),
                f"{prefix}_open": round(open_, 2),
                f"{prefix}_low": round(low, 2),
                f"{prefix}_high": round(high, 2),
                f"{prefix}_close": round(close, 2),
                "ltp": round(close, 2),
                "period_label": label.isoformat(),
            },
        )

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

        # The signal is the FIRST candle only: its open must equal its low.
        # Later bars are irrelevant - this is an opening-demand candidate, not
        # a day-long "never traded below the open" filter.
        first = today.iloc[0]
        day_open = float(first["open"])
        day_low = float(first["low"])
        if not _open_equals_low(day_open, day_low):
            return None

        # Report the first candle's own close/high so the signal is stable
        # regardless of how late a catch-up run starts.
        ltp = float(first["close"])
        day_high = float(first["high"])
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
                "bars_elapsed": 1,
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
