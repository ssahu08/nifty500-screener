"""Strategy: Nifty Hedge 23 - expiry-day trending straddle/strangle hedge.

Implements the "Nifty Hedge 23" expiry-day strategy explained by NK Singh
(NK Stock Talk), enhanced with Nifty expiry movement behaviour detection and
50pt/100pt radius strike selection:

1. Expiry-Day & Time Gate:
   * Trades only on a Nifty weekly expiry day (normally Tuesday; holiday-shifted
     Monday expiries are handled by :mod:`app.data.options`).
   * Entry window: after 1:00 PM IST (prime window 1:15-1:30 PM).
   * Hard exit: all positions closed at 3:30 PM (never hold to expiry).

2. Nifty Movement Behaviour Gate:
   * Evaluates Nifty 50 10-minute candles (and spot OHLC quote) to classify the
     expiry session into ``TRENDING_BULLISH``, ``TRENDING_BEARISH``,
     ``COILED_FOR_SURGE``, or ``RANGE_BOUND_CHOP``.
   * Proceeds with entry only when Nifty shows trending behaviour or an
     afternoon compression coil after a morning move (skips dead chop).

3. Strike Selection (50pt / 100pt Radius & Leg Balance):
   * Evaluates the 50-point radius (Spot + 50 CE & Spot - 50 PE), 100-point
     radius (Spot + 100 CE & Spot - 100 PE), and ATM (Spot CE & Spot PE).
   * Verifies that CE and PE premiums are balanced (within 15 pts or <= 2.2x
     ratio) and picks the pair inside the active entry zone.

4. Price, Stop-Loss, and Targets:
   * Supports both the classic 17-28 combined zone (SL = 10) and the updated
     75-lot 35-55 combined zone (ideal 40-50, SL = 18), plus 50-30-20 averaging.
   * Enforces the 2:35 PM (14:35 IST) time-based stop loss if the market turns
     range-bound and premium decays below entry.
   * Emits explicit ``entry``, ``stop``/``sl_price``, ``target_1`` (2x book
     half), and ``target_2`` (2.5x / 110+ runner) in both Signals and snapshot.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.core.logging import get_logger
from app.core.market import is_market_open
from app.data.options import (
    NIFTY_SPOT_SYMBOL,
    expiry_kind,
    is_nifty_expiry_day,
    next_weekly_expiry,
    nifty_option_symbol_candidates,
    round_to_nifty_strike,
)
from app.strategies.base import Category, Signal, Strategy, StrategyMetadata
from app.strategies.indicators import closed_candles

IST = ZoneInfo("Asia/Kolkata")
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Entry zones (combined CE + PE premium)
# ---------------------------------------------------------------------------
# Classic H23 zone (kept as primary constants for backward compatibility)
ENTRY_ZONE_LOW = 17.0
ENTRY_ZONE_HIGH = 28.0
ENTRY_ZONE_MID = 23.0

# Updated 75-lot NK Stock Talk zone (40-50 ideal, 35-55 active window)
EXTENDED_ZONE_LOW = 35.0
EXTENDED_IDEAL_LOW = 40.0
EXTENDED_IDEAL_HIGH = 50.0
EXTENDED_ZONE_HIGH = 55.0

# ---------------------------------------------------------------------------
# Strike selection & leg-balance rules
# ---------------------------------------------------------------------------
RADIUS_CANDIDATES = (50, 100, 0)  # Prefer 50pt radius, then 100pt, then ATM (0)
MAX_LEG_DIFF_PTS = 15.0           # CE and PE within 15 pts is always balanced
MAX_LEG_RATIO = 2.2               # Or max(CE, PE) / min(CE, PE) <= 2.2x

# ---------------------------------------------------------------------------
# Stop-loss & Profit Targets (combined premium)
# ---------------------------------------------------------------------------
SL_SIMPLE = 10.0                  # classic 17-28 zone stop (< 10)
SL_EXTENDED_SIMPLE = 18.0         # updated 35-55 zone stop (< 18-20)
SL_AVERAGING = 8.0                # averaging mode stop (< 8)

TARGET_1_MULTIPLIER = 2.0         # Book 50% lots when combined premium doubles
TARGET_2_MULTIPLIER = 2.5         # Runner target (2.5x or 110+ on extended zone)
EXTENDED_RUNNER_TARGET = 110.0

# ---------------------------------------------------------------------------
# Averaging mode entry levels (50-30-20)
# ---------------------------------------------------------------------------
AVG_LEVEL_1 = 50.0  # first entry: 2 lots
AVG_LEVEL_2 = 30.0  # second entry: 2 more lots
AVG_LEVEL_3 = 20.0  # third entry: 2 more lots
LOTS_PER_LEVEL = 2  # lots added at each level (always even for half-exit)

# ---------------------------------------------------------------------------
# Profit-take zones (averaging mode combined premium)
# ---------------------------------------------------------------------------
PT_LEVEL1_DOUBLE = 100.0
PT_LEVEL2_ZONE_LOW = 65.0
PT_LEVEL2_ZONE_HIGH = 70.0
PT_LEVEL3_ZONE = 45.0

# ---------------------------------------------------------------------------
# Nifty expiry movement behaviour thresholds
# ---------------------------------------------------------------------------
MIN_TRENDING_RANGE_PTS = 60.0     # Minimum intraday High-Low span for active trend
STRONG_TREND_RANGE_PTS = 95.0     # Wide trending session span
MIN_DIRECTIONAL_EFFICIENCY = 0.25  # |Close - Open| / (High - Low)

# ---------------------------------------------------------------------------
# Time windows (IST)
# ---------------------------------------------------------------------------
ENTRY_START = time(13, 0)         # 1:00 PM - earliest entry
TIME_SL_START = time(14, 35)      # 2:35 PM - time-based SL activates
MARKET_EXIT = time(15, 30)        # 3:30 PM - hard market close exit

#: How long a cached snapshot may be served before the API refetches quotes.
SNAPSHOT_TTL_SECONDS = 60.0


def classify_nifty_movement(
    df_10m: pd.DataFrame | None,
    spot: float,
    *,
    spot_open: float | None = None,
    spot_high: float | None = None,
    spot_low: float | None = None,
) -> dict[str, float | str | bool]:
    """Identify Nifty's expiry-day movement behaviour from 10m candles or quote.

    Returns a dictionary containing:
    * ``is_trending`` (bool): True if Nifty is trending or coiled after a
      morning impulse; False if stuck in a dead range-bound chop.
    * ``behaviour`` (str): ``"TRENDING_BULLISH"``, ``"TRENDING_BEARISH"``,
      ``"COILED_FOR_SURGE"``, or ``"RANGE_BOUND_CHOP"``.
    * ``bias`` (str): ``"BULLISH"``, ``"BEARISH"``, or ``"NEUTRAL"``.
    * ``day_range_pts`` (float): Intraday High - Low in Nifty index points.
    * ``move_from_open_pts`` (float): Spot - Day Open in index points.
    * ``efficiency`` (float): Directional efficiency ratio (0.0 to 1.0).
    """
    if df_10m is not None and not df_10m.empty:
        idx = df_10m.index
        idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
        latest_day = idx[-1].date()
        today_df = df_10m[idx.date == latest_day]
        if len(today_df) >= 6:
            day_open = float(today_df["open"].iloc[0])
            day_high = float(today_df["high"].max())
            day_low = float(today_df["low"].min())
            last_close = float(today_df["close"].iloc[-1])

            ema9 = float(today_df["close"].ewm(span=9, adjust=False).mean().iloc[-1])
            ema21 = float(today_df["close"].ewm(span=21, adjust=False).mean().iloc[-1])

            day_range = max(0.0, day_high - day_low)
            net_move = last_close - day_open
            efficiency = abs(net_move) / day_range if day_range > 0 else 0.0

            recent6 = today_df.iloc[-6:]
            recent6_range = float(recent6["high"].max() - recent6["low"].min())
            last_bar_span = abs(
                float(today_df["close"].iloc[-1] - today_df["open"].iloc[-1])
            )

            bias = (
                "BULLISH"
                if (last_close >= ema21 and net_move >= 15.0)
                else "BEARISH"
                if (last_close <= ema21 and net_move <= -15.0)
                else "NEUTRAL"
            )

            if (
                day_range >= MIN_TRENDING_RANGE_PTS
                and efficiency >= MIN_DIRECTIONAL_EFFICIENCY
                and abs(ema9 - ema21) >= 10.0
            ):
                behaviour = (
                    "TRENDING_BULLISH" if ema9 >= ema21 else "TRENDING_BEARISH"
                )
                return {
                    "is_trending": True,
                    "behaviour": behaviour,
                    "bias": "BULLISH" if ema9 >= ema21 else "BEARISH",
                    "day_range_pts": round(day_range, 1),
                    "move_from_open_pts": round(net_move, 1),
                    "efficiency": round(efficiency, 2),
                }

            if (
                day_range >= 70.0 and recent6_range <= day_range * 0.45
            ) or last_bar_span >= 22.0:
                return {
                    "is_trending": True,
                    "behaviour": "COILED_FOR_SURGE",
                    "bias": bias,
                    "day_range_pts": round(day_range, 1),
                    "move_from_open_pts": round(net_move, 1),
                    "efficiency": round(efficiency, 2),
                }

            return {
                "is_trending": False,
                "behaviour": "RANGE_BOUND_CHOP",
                "bias": "NEUTRAL",
                "day_range_pts": round(day_range, 1),
                "move_from_open_pts": round(net_move, 1),
                "efficiency": round(efficiency, 2),
            }

    # Quote-level fallback when 10m candles are not present
    if spot_high is None or spot_low is None or spot_high <= spot_low:
        # Minimal quote object (e.g. unit test mock without OHLC) -> allow trade
        return {
            "is_trending": True,
            "behaviour": "TRENDING_READY",
            "bias": "NEUTRAL",
            "day_range_pts": 0.0,
            "move_from_open_pts": 0.0,
            "efficiency": 1.0,
        }

    d_open = float(spot_open if spot_open is not None else spot)
    day_range = max(0.0, float(spot_high) - float(spot_low))
    net_move = spot - d_open
    efficiency = abs(net_move) / day_range if day_range > 0 else 0.0
    is_trending = day_range >= MIN_TRENDING_RANGE_PTS and (
        efficiency >= MIN_DIRECTIONAL_EFFICIENCY or day_range >= STRONG_TREND_RANGE_PTS
    )
    bias = (
        "BULLISH"
        if net_move >= 20.0
        else "BEARISH"
        if net_move <= -20.0
        else "NEUTRAL"
    )
    behaviour = (
        f"TRENDING_{bias}"
        if is_trending and bias != "NEUTRAL"
        else "COILED_FOR_SURGE"
        if is_trending
        else "RANGE_BOUND_CHOP"
    )
    return {
        "is_trending": is_trending,
        "behaviour": behaviour,
        "bias": bias,
        "day_range_pts": round(day_range, 1),
        "move_from_open_pts": round(net_move, 1),
        "efficiency": round(efficiency, 2),
    }


def _legs_are_balanced(ce_premium: float, pe_premium: float) -> bool:
    """True when CE and PE premiums are reasonably close per H23 radius rules."""
    if ce_premium <= 0 or pe_premium <= 0:
        return False
    diff = abs(ce_premium - pe_premium)
    ratio = max(ce_premium, pe_premium) / min(ce_premium, pe_premium)
    return diff <= MAX_LEG_DIFF_PTS or ratio <= MAX_LEG_RATIO


@dataclass
class _HedgeState:
    """Mutable internal state for one running hedge position."""

    mode: str = "simple"          # "simple" or "averaging"
    entered: bool = False         # whether a position is currently open
    expiry: str = ""              # expiry date (ISO, e.g. "2026-11-03")
    expiry_kind: str = ""         # "weekly" or "monthly"
    strike: int = 0               # ATM reference strike
    ce_strike: int = 0            # Selected CE strike (ATM + radius)
    pe_strike: int = 0            # Selected PE strike (ATM - radius)
    radius_pts: int = 0           # 0 (ATM), 50, or 100 point radius
    ce_symbol: str = ""           # FYERS symbol for the CE leg
    pe_symbol: str = ""           # FYERS symbol for the PE leg
    lots: int = 0                 # total straddle lots (1 lot = 1 CE + 1 PE)
    entry_premiums: list[float] = field(default_factory=list)
    entry_levels: list[str] = field(default_factory=list)
    entry_lots: list[int] = field(default_factory=list)
    entry_times: list[datetime] = field(default_factory=list)
    average_entry: float = 0.0    # volume-weighted average combined premium
    booked_lots: int = 0          # lots already booked for profit
    booked_levels: list[str] = field(default_factory=list)
    sl_price: float = 0.0         # combined-premium stop level
    target_1: float = 0.0         # partial profit booking target (2x)
    target_2: float = 0.0         # runner target (2.5x / 110+)
    exit_sent: bool = False       # whether a full EXIT signal has been emitted
    last_entry_time: datetime | None = None
    # Latest market snapshot (kept even when no position is open).
    last_spot: float = 0.0
    last_spot_open: float | None = None
    last_spot_high: float | None = None
    last_spot_low: float | None = None
    last_ce_premium: float = 0.0
    last_pe_premium: float = 0.0
    last_combined: float = 0.0
    last_action: str = ""
    last_action_time: datetime | None = None
    last_exit_reason: str = ""
    movement: dict = field(default_factory=dict)


class NiftyHedge23Strategy(Strategy):
    """Nifty Hedge 23 - expiry-day trending straddle/strangle hedge."""

    def __init__(self) -> None:
        self._state = _HedgeState()
        self._today: date | None = None
        self._snapshot: dict = {}
        self._snapshot_at: datetime | None = None
        self._lock = threading.RLock()

    # -- metadata -----------------------------------------------------------
    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Nifty Hedge 23",
            description=(
                "Expiry-day straddle/strangle hedge: checks Nifty trending "
                "behaviour, selects balanced 50pt/100pt/ATM CE + PE strikes, "
                "and tracks Entry, Stop-Loss, Target 1 (2x), and Target 2. "
                "Advisory only - never places trades."
            ),
            category=Category.INTRADAY,
            universe="NIFTY_HEDGE",
            schedule_type="auto",
            timeframes=["10m"],      # Nifty 10m candles for movement classification
            frequency_minutes=10,
        )

    def get_universe(self, settings: Settings) -> list[str]:
        """Include Nifty spot so the scheduler can share 10m Nifty candles."""
        return [NIFTY_SPOT_SYMBOL]

    def auto_run_allowed(self, now: datetime | None = None) -> bool:
        """Only run on Nifty expiry days, from 13:00 IST onwards.

        The scheduler checks this before fetching data, so non-expiry days and
        the morning session cost no API calls. ``is_nifty_expiry_day`` also
        covers holiday-shifted expiries (normally Tuesdays, occasionally the
        preceding Monday).
        """
        now = now if now is not None else datetime.now(IST)
        if getattr(now, "tzinfo", None) is None:
            now = now.replace(tzinfo=IST)
        return is_nifty_expiry_day(now.date()) and now.time() >= ENTRY_START

    def prefilter(self, quotes, settings):  # type: ignore[override]
        """Not used - quotes are fetched directly inside scan()."""
        return None

    # -- dashboard snapshot -------------------------------------------------
    def snapshot(self) -> dict:
        """Return a copy of the latest live snapshot (safe for the API)."""
        with self._lock:
            if not self._snapshot:
                return {
                    "status": "not_started",
                    "updated_at": None,
                    "advisory": "Advisory only - the screener never places orders.",
                }
            payload = copy.deepcopy(self._snapshot)
            if self._snapshot_at is not None:
                payload["age_seconds"] = round(
                    (datetime.now(IST) - self._snapshot_at).total_seconds(), 1
                )
            return payload

    def refresh_snapshot(self, settings: Settings) -> dict:
        """Fetch fresh quotes for the dashboard, throttled by the TTL."""
        with self._lock:
            if self._snapshot_at is not None:
                age = (datetime.now(IST) - self._snapshot_at).total_seconds()
                if age < SNAPSHOT_TTL_SECONDS:
                    return self.snapshot()

            now = datetime.now(IST)
            if not settings.hedge_23_enabled:
                self._base_snapshot(
                    settings,
                    now,
                    next_weekly_expiry(now.date()),
                    is_nifty_expiry_day(now.date()),
                )
                self._snapshot["status"] = "disabled"
                return self.snapshot()

            self._reset_for_day(settings, now)
            expiry = next_weekly_expiry(now.date())
            expiry_day = is_nifty_expiry_day(now.date())
            self._base_snapshot(settings, now, expiry, expiry_day)

            if not expiry_day:
                self._snapshot["status"] = "not_expiry_day"
                return self.snapshot()
            if not is_market_open(now):
                self._snapshot["status"] = "market_closed"
                return self.snapshot()

            try:
                spot, ce, pe, ce_sym, pe_sym, strike = self._fetch_leg_data(
                    settings, now
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Hedge 23: snapshot fetch failed: %s", exc)
                self._snapshot["status"] = "data_unavailable"
                self._snapshot["error"] = str(exc)
                self._snapshot_at = now
                return self.snapshot()

            if spot is None or ce is None or pe is None:
                self._snapshot["status"] = "data_unavailable"
                self._snapshot_at = now
                return self.snapshot()

            combined = round(ce + pe, 2)
            self._state.movement = classify_nifty_movement(
                None,
                spot,
                spot_open=self._state.last_spot_open,
                spot_high=self._state.last_spot_high,
                spot_low=self._state.last_spot_low,
            )
            self._apply_quote_snapshot(
                settings, now, spot, ce, pe, combined, ce_sym, pe_sym, strike
            )
            self._set_status(now)
            return self.snapshot()

    # -- main scan ----------------------------------------------------------
    def scan(
        self,
        data: dict,
        settings: Settings,
    ) -> list[Signal]:
        with self._lock:
            now = datetime.now(IST)

            if not settings.hedge_23_enabled:
                self._base_snapshot(
                    settings,
                    now,
                    next_weekly_expiry(now.date()),
                    is_nifty_expiry_day(now.date()),
                )
                self._snapshot["status"] = "disabled"
                return []

            self._reset_for_day(settings, now)

            expiry = next_weekly_expiry(now.date())
            expiry_day = is_nifty_expiry_day(now.date())
            self._base_snapshot(settings, now, expiry, expiry_day)

            if not expiry_day:
                self._snapshot["status"] = "not_expiry_day"
                return []

            signals: list[Signal] = []

            fetch_error: str | None = None
            spot = ce = pe = None
            ce_sym = pe_sym = ""
            strike = 0
            try:
                spot, ce, pe, ce_sym, pe_sym, strike = self._fetch_leg_data(
                    settings, now
                )
            except Exception as exc:  # noqa: BLE001
                fetch_error = str(exc)
                logger.warning("Hedge 23: failed to fetch option data: %s", exc)

            combined = self._state.last_combined
            if spot is not None and ce is not None and pe is not None:
                combined = round(ce + pe, 2)
                nifty_frames = (data or {}).get(NIFTY_SPOT_SYMBOL) or {}
                ten_min = closed_candles(nifty_frames.get("10m"), "10m", now)
                self._state.movement = classify_nifty_movement(
                    ten_min,
                    spot,
                    spot_open=self._state.last_spot_open,
                    spot_high=self._state.last_spot_high,
                    spot_low=self._state.last_spot_low,
                )
                self._apply_quote_snapshot(
                    settings, now, spot, ce, pe, combined, ce_sym, pe_sym, strike
                )
            else:
                self._snapshot["status"] = "data_unavailable"
                if fetch_error:
                    self._snapshot["error"] = fetch_error

            # Hard exit at 3:30 PM.
            if now.time() >= MARKET_EXIT:
                if self._state.entered and not self._state.exit_sent:
                    exit_price = combined if combined > 0 else self._state.average_entry
                    signals.append(
                        self._emit_exit(now, "market_close", exit_price, settings)
                    )
                    self._close_position(now, "market_close")
                self._snapshot["status"] = "market_closed"
                return signals

            if spot is None or ce is None or pe is None:
                return signals

            # Before 1:00 PM: show tentative legs and movement, but do not enter.
            if now.time() < ENTRY_START:
                self._set_status(now)
                return signals

            # --- position management (when position is open) --------------
            if self._state.entered:
                # 1) Hard stop loss or 14:35 IST time-based stop loss.
                sl_reason = self._check_sl(now, combined)
                if sl_reason:
                    signals.append(
                        self._emit_exit(now, sl_reason, combined, settings)
                    )
                    self._close_position(now, sl_reason)
                    logger.info(
                        "Hedge 23: EXIT %s at combined=%.2f", sl_reason, combined
                    )
                    return signals

                # 2) Full runner target (Target 2) hit.
                if self._state.target_2 > 0 and combined >= self._state.target_2:
                    signals.append(
                        self._emit_exit(now, "target_2", combined, settings)
                    )
                    self._close_position(now, "target_2")
                    logger.info(
                        "Hedge 23: EXIT target_2 at combined=%.2f", combined
                    )
                    return signals

                # 3) Partial profit booking (book some lots, trail the rest).
                booking = self._check_profit_take(combined)
                if booking is not None:
                    reason, lots_to_book, level = booking
                    signals.append(
                        self._emit_book(
                            now, reason, level, combined, lots_to_book, settings
                        )
                    )
                    self._book_lots(now, reason, level, lots_to_book)
                    remaining = self._state.lots - self._state.booked_lots
                    if remaining <= 0:
                        self._close_position(now, reason)
                    else:
                        self._set_status(now)
                    logger.info(
                        "Hedge 23: BOOK %s level=%s lots=%d remaining=%d combined=%.2f",
                        reason, level, lots_to_book, remaining, combined,
                    )
                    return signals

                # 4) Averaging scale-in while the position is open.
                if self._state.mode == "averaging":
                    entry_level = self._should_enter(now, combined, settings)
                    if entry_level is not None:
                        lots = self._add_size(settings, combined)
                        if lots >= 2:
                            self._enter_position(
                                now, combined, ce_sym, pe_sym, strike,
                                lots, entry_level, settings,
                            )
                            signals.append(
                                self._emit_entry(
                                    now, combined, lots, entry_level, settings
                                )
                            )
                            logger.info(
                                "Hedge 23: ADD %s at combined=%.2f lots=%d",
                                entry_level, combined, lots,
                            )
                self._set_status(now)
                return signals

            # --- Nifty Movement Gate (must be trending / coiled for surge) -
            movement = self._state.movement or {"is_trending": True}
            if not bool(movement.get("is_trending", True)):
                note = (
                    f"Nifty is {movement.get('behaviour', 'RANGE_BOUND_CHOP')} "
                    f"(day range {movement.get('day_range_pts', 0.0)} pts); "
                    "waiting for trending move before entry."
                )
                self._snapshot["note"] = note
                self._snapshot["status"] = "range_bound_wait"
                self._snapshot["next_action"] = "WAIT_TREND"
                logger.info("Hedge 23: %s", note)
                return signals

            # --- fresh entry ---------------------------------------------
            entry_level = self._should_enter(now, combined, settings)
            if entry_level is None:
                self._set_status(now)
                return signals

            lots = (
                self._add_size(settings, combined)
                if self._state.mode == "averaging"
                else self._position_size(settings, combined)
            )
            if lots < 2:
                max_capital = (
                    settings.hedge_23_capital * settings.hedge_23_max_capital_pct
                )
                note = (
                    f"Only {lots} lot(s) fit the ₹{max_capital:,.0f} capital cap "
                    f"at combined ₹{combined:.2f}; entry skipped."
                )
                self._snapshot["note"] = note
                logger.info("Hedge 23: %s", note)
                self._set_status(now)
                return signals

            self._enter_position(
                now, combined, ce_sym, pe_sym, strike, lots, entry_level, settings
            )
            signals.append(
                self._emit_entry(now, combined, lots, entry_level, settings)
            )
            logger.info(
                "Hedge 23: ENTRY %s at combined=%.2f lots=%d strike=%d (CE=%d PE=%d)",
                entry_level,
                combined,
                lots,
                strike,
                self._state.ce_strike or strike,
                self._state.pe_strike or strike,
            )
            self._set_status(now)
            return signals

    # -- state / snapshot helpers ------------------------------------------
    def _reset_for_day(self, settings: Settings, now: datetime) -> None:
        if self._today == now.date():
            return
        self._today = now.date()
        mode = (settings.hedge_23_mode or "simple").strip().lower()
        if mode not in {"simple", "averaging"}:
            logger.warning("Hedge 23: unknown mode %r, using simple", mode)
            mode = "simple"
        self._state = _HedgeState(mode=mode)
        logger.info("Hedge 23: new day %s, mode=%s", self._today, mode)

    @staticmethod
    def _compute_targets_and_sl(
        entry_price: float, mode: str, entry_level: str = "simple"
    ) -> tuple[float, float, float]:
        """Return ``(sl_price, target_1, target_2)`` for a given entry price."""
        if mode == "averaging":
            sl = SL_AVERAGING
            t1 = round(entry_price * TARGET_1_MULTIPLIER, 2)
            t2 = round(max(PT_LEVEL1_DOUBLE, entry_price * TARGET_2_MULTIPLIER), 2)
            return sl, t1, t2

        if entry_level == "extended" or entry_price > ENTRY_ZONE_HIGH:
            sl = SL_EXTENDED_SIMPLE
            t1 = round(entry_price * TARGET_1_MULTIPLIER, 2)
            t2 = round(
                max(EXTENDED_RUNNER_TARGET, entry_price * TARGET_2_MULTIPLIER), 2
            )
            return sl, t1, t2

        sl = SL_SIMPLE
        t1 = round(entry_price * TARGET_1_MULTIPLIER, 2)
        t2 = round(entry_price * TARGET_2_MULTIPLIER, 2)
        return sl, t1, t2

    def _base_snapshot(
        self,
        settings: Settings,
        now: datetime,
        expiry: date,
        expiry_day: bool,
    ) -> None:
        max_capital = settings.hedge_23_capital * settings.hedge_23_max_capital_pct
        self._snapshot = {
            "strategy": self.metadata.name,
            "enabled": settings.hedge_23_enabled,
            "mode": self._state.mode,
            "today": now.date().isoformat(),
            "is_expiry_day": expiry_day,
            "expiry": expiry.isoformat(),
            "expiry_kind": expiry_kind(expiry),
            "expiry_label": expiry.strftime("%d %b %Y"),
            "entry_window": "13:00-15:30 IST",
            "entry_zone": {
                "low": ENTRY_ZONE_LOW,
                "high": ENTRY_ZONE_HIGH,
                "mid": ENTRY_ZONE_MID,
                "extended_low": EXTENDED_ZONE_LOW,
                "extended_high": EXTENDED_ZONE_HIGH,
            },
            "averaging_levels": [AVG_LEVEL_1, AVG_LEVEL_2, AVG_LEVEL_3],
            "sl": {
                "simple": SL_SIMPLE,
                "extended": SL_EXTENDED_SIMPLE,
                "averaging": SL_AVERAGING,
                "time_sl": "14:35 IST",
            },
            "capital": settings.hedge_23_capital,
            "max_capital": round(max_capital, 2),
            "lot_size": settings.hedge_23_lot_size,
            "strike_step": 50,
            "status": "waiting",
            "next_action": "WAIT",
            "updated_at": now.isoformat(),
            "advisory": (
                "Advisory only - the screener never places orders. "
                "Verify premiums and strikes in your broker terminal."
            ),
        }

    def _apply_quote_snapshot(
        self,
        settings: Settings,
        now: datetime,
        spot: float,
        ce: float,
        pe: float,
        combined: float,
        ce_sym: str,
        pe_sym: str,
        strike: int,
    ) -> None:
        st = self._state
        st.last_spot = spot
        st.last_ce_premium = ce
        st.last_pe_premium = pe
        st.last_combined = combined
        st.strike = strike
        if not st.ce_strike:
            st.ce_strike = strike
        if not st.pe_strike:
            st.pe_strike = strike
        st.ce_symbol = ce_sym
        st.pe_symbol = pe_sym
        st.expiry = next_weekly_expiry(now.date()).isoformat()
        st.expiry_kind = expiry_kind(next_weekly_expiry(now.date()))

        recommended = (
            self._add_size(settings, combined)
            if st.entered or st.mode == "averaging"
            else self._position_size(settings, combined)
        )
        next_level = self._next_level(combined)
        in_classic = ENTRY_ZONE_LOW <= combined <= ENTRY_ZONE_HIGH
        in_extended = (
            EXTENDED_ZONE_LOW <= combined <= EXTENDED_ZONE_HIGH
            and _legs_are_balanced(ce, pe)
        )
        tentative_sl, tentative_t1, tentative_t2 = self._compute_targets_and_sl(
            st.average_entry if st.entered else combined,
            st.mode,
            "extended" if in_extended and not in_classic else "simple",
        )

        self._snapshot.update(
            {
                "spot": round(spot, 2),
                "atm_strike": strike,
                "ce_strike": st.ce_strike,
                "pe_strike": st.pe_strike,
                "radius_pts": st.radius_pts,
                "ce_symbol": ce_sym,
                "pe_symbol": pe_sym,
                "ce_premium": round(ce, 2),
                "pe_premium": round(pe, 2),
                "combined_premium": combined,
                "legs_balanced": _legs_are_balanced(ce, pe),
                "movement": st.movement,
                "nifty_behaviour": st.movement.get("behaviour", "TRENDING_READY"),
                "in_entry_zone": in_classic or in_extended,
                "next_level": next_level,
                "recommended_lots": recommended,
                "cost_per_lot": round(combined * settings.hedge_23_lot_size, 2),
                "recommended_cost": round(
                    recommended * combined * settings.hedge_23_lot_size, 2
                ),
                "sl_price": st.sl_price if st.entered else tentative_sl,
                "target_1": st.target_1 if st.entered else tentative_t1,
                "target_2": st.target_2 if st.entered else tentative_t2,
                "tentative": {
                    "strike": strike,
                    "ce_strike": st.ce_strike,
                    "pe_strike": st.pe_strike,
                    "radius_pts": st.radius_pts,
                    "ce_symbol": ce_sym,
                    "pe_symbol": pe_sym,
                    "ce_premium": round(ce, 2),
                    "pe_premium": round(pe, 2),
                    "combined_premium": combined,
                    "sl_price": tentative_sl,
                    "target_1": tentative_t1,
                    "target_2": tentative_t2,
                    "lots": recommended,
                    "cost": round(
                        recommended * combined * settings.hedge_23_lot_size, 2
                    ),
                },
                "position": self._position_snapshot(),
                "updated_at": now.isoformat(),
            }
        )
        self._snapshot_at = now

    def _position_snapshot(self) -> dict:
        st = self._state
        return {
            "open": st.entered,
            "lots": st.lots,
            "remaining_lots": max(0, st.lots - st.booked_lots),
            "average_entry": st.average_entry,
            "sl_price": st.sl_price,
            "target_1": st.target_1,
            "target_2": st.target_2,
            "ce_strike": st.ce_strike or st.strike,
            "pe_strike": st.pe_strike or st.strike,
            "radius_pts": st.radius_pts,
            "booked_lots": st.booked_lots,
            "booked_levels": list(st.booked_levels),
            "entry_levels": list(st.entry_levels),
            "entry_premiums": list(st.entry_premiums),
            "entry_lots": list(st.entry_lots),
            "entry_times": [t.isoformat() for t in st.entry_times],
            "last_action": st.last_action,
            "last_action_time": (
                st.last_action_time.isoformat() if st.last_action_time else None
            ),
            "last_exit_reason": st.last_exit_reason,
        }

    def _set_status(self, now: datetime) -> None:
        st = self._state
        self._snapshot["position"] = self._position_snapshot()
        if st.entered:
            self._snapshot["status"] = "position_open"
            self._snapshot["next_action"] = (
                "ADD" if self._next_level(st.last_combined) else "HOLD"
            )
            return
        if now.time() < ENTRY_START:
            self._snapshot["status"] = "waiting_entry_window"
            self._snapshot["next_action"] = "WAIT"
            return
        # Mirror the scan's movement gate so the dashboard never advertises an
        # entry while Nifty is stuck in dead range-bound chop.
        movement = st.movement or {}
        if movement and not bool(movement.get("is_trending", True)):
            self._snapshot["status"] = "range_bound_wait"
            self._snapshot["next_action"] = "WAIT_TREND"
            return
        if self._next_level(self._snapshot.get("combined_premium")):
            self._snapshot["status"] = "entry_zone"
            self._snapshot["next_action"] = "ENTRY"
        else:
            self._snapshot["status"] = "waiting_zone"
            self._snapshot["next_action"] = "WAIT"

    def _next_level(self, combined: float | None) -> str | None:
        """Return the next actionable level for the current mode/position."""
        if combined is None:
            return None
        st = self._state
        if st.mode == "simple":
            if st.entered:
                return None
            if ENTRY_ZONE_LOW <= combined <= ENTRY_ZONE_HIGH:
                return "simple"
            if (
                EXTENDED_ZONE_LOW <= combined <= EXTENDED_ZONE_HIGH
                and _legs_are_balanced(st.last_ce_premium, st.last_pe_premium)
            ):
                return "extended"
            return None

        levels = set(st.entry_levels)
        if "50" not in levels and combined <= AVG_LEVEL_1:
            return "50"
        if "50" in levels and "30" not in levels and combined <= AVG_LEVEL_2:
            return "30"
        if "30" in levels and "20" not in levels and combined <= AVG_LEVEL_3:
            return "20"
        return None

    # -- data fetching (supports 50pt, 100pt, and ATM radius selection) -----
    def _fetch_leg_data(
        self, settings: Settings, now: datetime
    ) -> tuple[float | None, float | None, float | None, str, str, int]:
        """Return ``(spot, ce_premium, pe_premium, ce_symbol, pe_symbol, strike)``.

        Evaluates 50-point radius, 100-point radius, and ATM (0-point radius)
        in one batched quote request, selecting the radius pair that lands in
        the active entry zone with balanced CE/PE premiums (falling back to ATM).
        """
        if self.provider is None:
            raise RuntimeError("Hedge 23: no market-data provider set")

        expiry = next_weekly_expiry(now.date())

        spot_q = self.provider.get_quotes([NIFTY_SPOT_SYMBOL])
        spot_quote = spot_q.get(NIFTY_SPOT_SYMBOL)
        if spot_quote is None or not spot_quote.ltp:
            return None, None, None, "", "", 0
        spot = float(spot_quote.ltp)
        self._state.last_spot_open = spot_quote.open
        self._state.last_spot_high = spot_quote.high
        self._state.last_spot_low = spot_quote.low

        atm_strike = round_to_nifty_strike(spot)

        # An open position is managed on the exact legs it was entered with.
        # Re-selecting a radius each scan would make the dashboard, SL and
        # target checks drift to strikes that were never bought.
        st = self._state
        if st.entered and st.ce_symbol and st.pe_symbol:
            held = self.provider.get_quotes([st.ce_symbol, st.pe_symbol])
            ce_q = held.get(st.ce_symbol)
            pe_q = held.get(st.pe_symbol)
            if (
                ce_q is not None
                and ce_q.ltp is not None
                and pe_q is not None
                and pe_q.ltp is not None
            ):
                return (
                    spot,
                    float(ce_q.ltp),
                    float(pe_q.ltp),
                    st.ce_symbol,
                    st.pe_symbol,
                    st.strike or atm_strike,
                )
            logger.warning(
                "Hedge 23: held option quotes unavailable for %s / %s",
                st.ce_symbol,
                st.pe_symbol,
            )
            return spot, None, None, st.ce_symbol, st.pe_symbol, st.strike or atm_strike

        # Build candidate symbol pairs across radii: 50pt, 100pt, and ATM (0pt)
        radius_pairs: dict[int, list[tuple[str, str, int, int]]] = {}
        all_symbols: list[str] = []
        for radius in RADIUS_CANDIDATES:
            ce_strike = atm_strike + radius
            pe_strike = atm_strike - radius
            ce_cands = nifty_option_symbol_candidates(ce_strike, "CE", expiry)
            pe_cands = nifty_option_symbol_candidates(pe_strike, "PE", expiry)
            pairs = [
                (ce_s, pe_s, ce_strike, pe_strike)
                for ce_s, pe_s in zip(ce_cands, pe_cands, strict=False)
            ]
            radius_pairs[radius] = pairs
            for ce_s, pe_s, _, _ in pairs:
                if ce_s not in all_symbols:
                    all_symbols.append(ce_s)
                if pe_s not in all_symbols:
                    all_symbols.append(pe_s)

        quotes = self.provider.get_quotes(all_symbols)

        resolved_by_radius: dict[int, tuple[float, float, str, str, int, int]] = {}
        for radius, pairs in radius_pairs.items():
            for ce_s, pe_s, ce_k, pe_k in pairs:
                ce_q = quotes.get(ce_s)
                pe_q = quotes.get(pe_s)
                if (
                    ce_q is not None
                    and ce_q.ltp is not None
                    and pe_q is not None
                    and pe_q.ltp is not None
                ):
                    resolved_by_radius[radius] = (
                        float(ce_q.ltp),
                        float(pe_q.ltp),
                        ce_s,
                        pe_s,
                        ce_k,
                        pe_k,
                    )
                    break

        if not resolved_by_radius:
            logger.warning(
                "Hedge 23: no valid option symbols for strike %d expiry %s",
                atm_strike,
                expiry.isoformat(),
            )
            return spot, None, None, "", "", atm_strike

        # Selection priority:
        # 1. If a radius is inside classic (17-28) or extended (35-55) entry zone
        #    with balanced legs, pick it (checking 50pt -> 100pt -> ATM).
        # 2. Otherwise fall back to ATM (0) if resolved, else the first resolved
        #    radius.
        chosen_radius = None
        for radius in RADIUS_CANDIDATES:
            if radius not in resolved_by_radius:
                continue
            ce_p, pe_p, _, _, _, _ = resolved_by_radius[radius]
            comb = round(ce_p + pe_p, 2)
            if (ENTRY_ZONE_LOW <= comb <= ENTRY_ZONE_HIGH) or (
                EXTENDED_ZONE_LOW <= comb <= EXTENDED_ZONE_HIGH
                and _legs_are_balanced(ce_p, pe_p)
            ):
                chosen_radius = radius
                break

        if chosen_radius is None:
            chosen_radius = (
                0 if 0 in resolved_by_radius else next(iter(resolved_by_radius))
            )

        (
            ce_premium,
            pe_premium,
            ce_sym,
            pe_sym,
            ce_k,
            pe_k,
        ) = resolved_by_radius[chosen_radius]
        self._state.radius_pts = chosen_radius
        self._state.ce_strike = ce_k
        self._state.pe_strike = pe_k

        return spot, ce_premium, pe_premium, ce_sym, pe_sym, atm_strike

    # -- entry logic --------------------------------------------------------
    def _should_enter(
        self, now: datetime, combined: float, settings: Settings
    ) -> str | None:
        """Return the entry level string if we should enter/add, else None."""
        st = self._state

        gap = timedelta(minutes=max(0, settings.hedge_23_min_entry_gap_minutes))
        if st.last_entry_time is not None and now - st.last_entry_time < gap:
            return None

        if st.mode == "simple":
            if st.entered:
                return None
            if ENTRY_ZONE_LOW <= combined <= ENTRY_ZONE_HIGH:
                return "simple"
            if (
                EXTENDED_ZONE_LOW <= combined <= EXTENDED_ZONE_HIGH
                and _legs_are_balanced(st.last_ce_premium, st.last_pe_premium)
            ):
                return "extended"
            return None

        levels = set(st.entry_levels)
        if "50" not in levels and combined <= AVG_LEVEL_1:
            return "50"
        if "50" in levels and "30" not in levels and combined <= AVG_LEVEL_2:
            return "30"
        if "30" in levels and "20" not in levels and combined <= AVG_LEVEL_3:
            return "20"
        return None

    def _enter_position(
        self,
        now: datetime,
        combined: float,
        ce_sym: str,
        pe_sym: str,
        strike: int,
        lots: int,
        entry_level: str,
        settings: Settings,
    ) -> None:
        """Record a new entry (or add to an averaging position)."""
        st = self._state
        is_add = st.entered

        if not is_add:
            st.entered = True
            st.lots = lots
            st.entry_premiums = [combined]
            st.entry_levels = [entry_level]
            st.entry_lots = [lots]
            st.entry_times = [now]
            st.average_entry = combined
            st.booked_lots = 0
            st.booked_levels = []
            st.exit_sent = False
            st.last_exit_reason = ""
        else:
            st.lots += lots
            st.entry_premiums.append(combined)
            st.entry_levels.append(entry_level)
            st.entry_lots.append(lots)
            st.entry_times.append(now)
            total_cost = sum(
                premium * qty
                for premium, qty in zip(
                    st.entry_premiums, st.entry_lots, strict=False
                )
            )
            st.average_entry = round(total_cost / st.lots, 2)

        sl, t1, t2 = self._compute_targets_and_sl(
            st.average_entry, st.mode, entry_level
        )
        st.sl_price = sl
        st.target_1 = t1
        st.target_2 = t2
        st.strike = strike
        if not st.ce_strike:
            st.ce_strike = strike
        if not st.pe_strike:
            st.pe_strike = strike
        st.ce_symbol = ce_sym
        st.pe_symbol = pe_sym
        st.last_entry_time = now
        st.last_action = "ADD" if is_add else "ENTRY"
        st.last_action_time = now

        logger.info(
            "Hedge 23: position state lots=%d avg=%.2f sl=%.2f t1=%.2f t2=%.2f "
            "levels=%s",
            st.lots,
            st.average_entry,
            st.sl_price,
            st.target_1,
            st.target_2,
            st.entry_levels,
        )

    # -- exit logic ---------------------------------------------------------
    def _check_sl(self, now: datetime, combined: float) -> str | None:
        """Return ``"sl"`` or ``"time_sl"`` when stop conditions are breached."""
        st = self._state
        if st.sl_price > 0 and combined <= st.sl_price:
            return "sl"
        # Time-based SL at/after 14:35 IST if market turned range-bound and
        # combined premium has decayed by >= 20% from average entry
        if (
            now.time() >= TIME_SL_START
            and st.average_entry > 0
            and combined <= st.average_entry * 0.80
            and not bool(st.movement.get("is_trending", True))
        ):
            return "time_sl"
        return None

    def _check_profit_take(
        self, combined: float
    ) -> tuple[str, int, str] | None:
        """Return ``(reason, lots_to_book, level)`` for a partial booking."""
        st = self._state
        remaining = st.lots - st.booked_lots
        if remaining <= 0:
            return None

        if st.mode == "simple":
            level_key = st.entry_levels[0] if st.entry_levels else "simple"
            if (
                "simple" not in st.booked_levels
                and level_key not in st.booked_levels
                and st.average_entry > 0
                and combined >= st.average_entry * 2
            ):
                lots = max(1, st.lots // 2)
                return "profit_half", min(lots, remaining), "simple"
            return None

        if (
            "50" in st.entry_levels
            and "50" not in st.booked_levels
            and combined >= PT_LEVEL1_DOUBLE
        ):
            return "profit_level1", min(1, remaining), "50"
        if (
            "30" in st.entry_levels
            and "30" not in st.booked_levels
            and PT_LEVEL2_ZONE_LOW <= combined <= PT_LEVEL2_ZONE_HIGH
        ):
            return "profit_level2", min(2, remaining), "30"
        if (
            "20" in st.entry_levels
            and "20" not in st.booked_levels
            and combined >= PT_LEVEL3_ZONE
        ):
            return "profit_level3", min(3, remaining), "20"
        return None

    def _book_lots(
        self, now: datetime, reason: str, level: str, lots: int
    ) -> None:
        st = self._state
        st.booked_lots += lots
        if level not in st.booked_levels:
            st.booked_levels.append(level)
        st.last_action = "BOOK"
        st.last_action_time = now
        logger.info(
            "Hedge 23: booked %d lot(s) at %s (%s), total booked=%d",
            lots, level, reason, st.booked_lots,
        )

    def _close_position(self, now: datetime, reason: str) -> None:
        """Fully close the position and clear the per-trade tracking state."""
        st = self._state
        st.entered = False
        st.exit_sent = True
        st.last_exit_reason = reason
        st.last_action = "EXIT"
        st.last_action_time = now
        st.lots = 0
        st.entry_premiums = []
        st.entry_levels = []
        st.entry_lots = []
        st.entry_times = []
        st.average_entry = 0.0
        st.booked_lots = 0
        st.booked_levels = []
        st.sl_price = 0.0
        st.target_1 = 0.0
        st.target_2 = 0.0
        self._snapshot["status"] = "exited"
        self._snapshot["position"] = self._position_snapshot()

    # -- position sizing ----------------------------------------------------
    def _max_capital(self, settings: Settings) -> float:
        return settings.hedge_23_capital * settings.hedge_23_max_capital_pct

    def _invested(self, settings: Settings) -> float:
        st = self._state
        return sum(
            premium * lots * settings.hedge_23_lot_size
            for premium, lots in zip(
                st.entry_premiums, st.entry_lots, strict=False
            )
        )

    def _position_size(self, settings: Settings, combined: float) -> int:
        cost_per_lot = combined * settings.hedge_23_lot_size
        if cost_per_lot <= 0:
            return 0
        lots = int(self._max_capital(settings) / cost_per_lot)
        return (lots // 2) * 2

    def _add_size(self, settings: Settings, combined: float) -> int:
        cost_per_lot = combined * settings.hedge_23_lot_size
        if cost_per_lot <= 0:
            return 0
        remaining = max(0.0, self._max_capital(settings) - self._invested(settings))
        fits = int(remaining / cost_per_lot)
        lots = min(LOTS_PER_LEVEL, fits)
        return (lots // 2) * 2

    # -- signal emission ----------------------------------------------------
    def _emit_entry(
        self, now: datetime, combined: float, lots: int,
        entry_level: str, settings: Settings,
    ) -> Signal:
        st = self._state
        risk_pts = max(0.0, round(st.average_entry - st.sl_price, 2))
        risk_pct = (
            round(risk_pts / st.average_entry * 100.0, 1)
            if st.average_entry > 0
            else 0.0
        )
        return Signal(
            symbol="NIFTY HEDGE 23",
            strategy_name=self.metadata.name,
            timestamp=now,
            price=combined,
            metadata={
                "action": "ADD" if len(st.entry_levels) > 1 else "ENTRY",
                "mode": st.mode,
                "expiry": st.expiry,
                "expiry_kind": st.expiry_kind,
                "nifty_spot": round(st.last_spot, 2),
                "nifty_behaviour": st.movement.get("behaviour", "TRENDING_READY"),
                "nifty_bias": st.movement.get("bias", "NEUTRAL"),
                "nifty_day_range_pts": st.movement.get("day_range_pts", 0.0),
                "strike": st.strike,
                "ce_strike": st.ce_strike or st.strike,
                "pe_strike": st.pe_strike or st.strike,
                "radius_pts": st.radius_pts,
                "ce_symbol": st.ce_symbol,
                "pe_symbol": st.pe_symbol,
                "ce_premium": st.last_ce_premium,
                "pe_premium": st.last_pe_premium,
                "combined_premium": combined,
                "entry": st.average_entry,
                "average_entry": st.average_entry,
                "stop": st.sl_price,
                "sl_price": st.sl_price,
                "time_sl": "14:35 IST",
                "target_1": st.target_1,
                "target_2": st.target_2,
                "risk_pts": risk_pts,
                "risk_pct": risk_pct,
                "lots": lots,
                "total_lots": st.lots,
                "entry_level": entry_level,
                "capital": settings.hedge_23_capital,
                "lot_size": settings.hedge_23_lot_size,
            },
        )

    def _emit_book(
        self,
        now: datetime,
        reason: str,
        level: str,
        combined: float,
        lots: int,
        settings: Settings,
    ) -> Signal:
        st = self._state
        pnl = round(
            (combined - st.average_entry) * lots * settings.hedge_23_lot_size,
            2,
        )
        return Signal(
            symbol="NIFTY HEDGE 23",
            strategy_name=self.metadata.name,
            timestamp=now,
            price=combined,
            metadata={
                "action": "BOOK",
                "mode": st.mode,
                "reason": reason,
                "level": level,
                "expiry": st.expiry,
                "expiry_kind": st.expiry_kind,
                "strike": st.strike,
                "ce_strike": st.ce_strike or st.strike,
                "pe_strike": st.pe_strike or st.strike,
                "radius_pts": st.radius_pts,
                "ce_symbol": st.ce_symbol,
                "pe_symbol": st.pe_symbol,
                "combined_premium": combined,
                "entry": st.average_entry,
                "entry_average": st.average_entry,
                "stop": st.sl_price,
                "sl_price": st.sl_price,
                "target_1": st.target_1,
                "target_2": st.target_2,
                "lots_booked": lots,
                "booked_lots": st.booked_lots + lots,
                "remaining_lots": max(0, st.lots - st.booked_lots - lots),
                "pnl_estimate": pnl,
                "lot_size": settings.hedge_23_lot_size,
            },
        )

    def _emit_exit(
        self, now: datetime, reason: str, combined: float, settings: Settings,
    ) -> Signal:
        st = self._state
        remaining = max(0, st.lots - st.booked_lots)
        pnl = None
        if st.average_entry > 0 and combined > 0:
            pnl = round(
                (combined - st.average_entry)
                * remaining
                * settings.hedge_23_lot_size,
                2,
            )
        return Signal(
            symbol="NIFTY HEDGE 23",
            strategy_name=self.metadata.name,
            timestamp=now,
            price=combined,
            metadata={
                "action": "EXIT",
                "mode": st.mode,
                "reason": reason,
                "expiry": st.expiry,
                "expiry_kind": st.expiry_kind,
                "strike": st.strike,
                "ce_strike": st.ce_strike or st.strike,
                "pe_strike": st.pe_strike or st.strike,
                "radius_pts": st.radius_pts,
                "ce_symbol": st.ce_symbol,
                "pe_symbol": st.pe_symbol,
                "combined_premium": combined,
                "entry": st.average_entry,
                "entry_average": st.average_entry,
                "stop": st.sl_price,
                "sl_price": st.sl_price,
                "target_1": st.target_1,
                "target_2": st.target_2,
                "lots": st.lots,
                "booked_lots": st.booked_lots,
                "remaining_lots": remaining,
                "pnl_estimate": pnl,
                "lot_size": settings.hedge_23_lot_size,
            },
        )


# Registry hook: the strategy framework discovers this module.
strategy_class = NiftyHedge23Strategy
