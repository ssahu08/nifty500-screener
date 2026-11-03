"""Strategy: Nifty Hedge 23 - expiry-day long-straddle hedge.

Implements the "Nifty Hedge 23" expiry-day strategy explained across three
transcripts (NK Singh / Nitish Sir). On Nifty weekly expiry day (Tuesday),
the strategy buys an ATM Call and an ATM Put *together* (a long straddle
hedge) so that a sharp move in either direction lets the winning leg run while
the losing leg is capped. It is designed for low-capital traders to avoid the
"hero-zero" trap of single-sided option buying.

Two modes are supported:

* **simple** (default) - one entry when the combined CE+PE premium enters the
  17-28 zone (midpoint 23, hence "Hedge 23").
* **averaging** (50-30-20) - scale in at combined premium 50 (2 lots), then 30
  (2 more lots), then 20 or below (2 more lots), max 6 lots, average ~33.33.

Common rules (both modes):

* Trade only on a Nifty expiry day (normally Tuesday). SEBI directive (Oct
  2024): only one weekly index per exchange; NSE kept Nifty weekly on Tuesday
  from 1 Sep 2025. Holiday-shifted expiries (Monday) are handled by
  :mod:`app.data.options`.
* Entry window: after 1:00 PM IST (preferred 1:15-1:30 PM). Time decay has
  accelerated by then, pulling premiums into the zone.
* Stop loss: combined premium below 10 (simple) or 8 (averaging).
* Hard exit: all positions closed at 3:30 PM - never hold to expiry.
* Position sizing: the total premium paid may never exceed the configured
  capital cap (default 5% of capital). One straddle lot costs
  ``combined_premium * lot_size`` (the combined premium already includes both
  legs). If fewer than two lots fit, the entry is skipped rather than
  breaching the cap.
* No fixed target - profit is unlimited. Book half when the premium doubles
  (simple) or at the per-level zones (averaging), then trail the rest.

The strategy is **advisory only**: it never places orders. It also maintains a
live :meth:`snapshot` so the dashboard can show the tentative strike and both
option legs before an entry is triggered.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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

IST = ZoneInfo("Asia/Kolkata")
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Entry zone (combined CE + PE premium)
# ---------------------------------------------------------------------------
ENTRY_ZONE_LOW = 17.0    # best entry zone (lowest risk)
ENTRY_ZONE_HIGH = 28.0   # upper bound of the entry zone
ENTRY_ZONE_MID = 23.0    # midpoint - the strategy is named after this

# ---------------------------------------------------------------------------
# Stop-loss (combined premium)
# ---------------------------------------------------------------------------
SL_SIMPLE = 10.0      # simple mode: exit when combined premium < 10
SL_AVERAGING = 8.0    # averaging mode: exit when combined premium < 8

# ---------------------------------------------------------------------------
# Averaging mode entry levels (50-30-20)
# ---------------------------------------------------------------------------
AVG_LEVEL_1 = 50.0  # first entry: 2 lots
AVG_LEVEL_2 = 30.0  # second entry: 2 more lots
AVG_LEVEL_3 = 20.0  # third entry: 2 more lots
LOTS_PER_LEVEL = 2  # lots added at each level (always even for half-exit)

# ---------------------------------------------------------------------------
# Profit-take zones (combined premium)
# ---------------------------------------------------------------------------
# From level-1 (50) entry: book 1 lot when premium doubles (~100)
PT_LEVEL1_DOUBLE = 100.0
# From level-2 (30) entry: book half (2 lots) at 65-70 zone
PT_LEVEL2_ZONE_LOW = 65.0
PT_LEVEL2_ZONE_HIGH = 70.0
# From level-3 (20) entry: book half (3 lots) at ~45
PT_LEVEL3_ZONE = 45.0

# ---------------------------------------------------------------------------
# Time windows (IST)
# ---------------------------------------------------------------------------
ENTRY_START = time(13, 0)      # 1:00 PM - earliest entry
TIME_SL_START = time(14, 35)   # 2:35 PM - time-based SL activates
MARKET_EXIT = time(15, 30)     # 3:30 PM - hard market close exit

#: How long a cached snapshot may be served before the API refetches quotes.
SNAPSHOT_TTL_SECONDS = 60.0


@dataclass
class _HedgeState:
    """Mutable internal state for one running hedge position.

    Persists across scheduler ticks because the registry keeps a single
    strategy instance for the life of the process.
    """

    mode: str = "simple"          # "simple" or "averaging"
    entered: bool = False         # whether a position is currently open
    expiry: str = ""              # expiry date (ISO, e.g. "2026-11-03")
    expiry_kind: str = ""         # "weekly" or "monthly"
    strike: int = 0               # ATM strike
    ce_symbol: str = ""           # FYERS symbol for the CE leg
    pe_symbol: str = ""           # FYERS symbol for the PE leg
    lots: int = 0                 # total straddle lots (1 lot = 1 CE + 1 PE)
    entry_premiums: list[float] = field(default_factory=list)  # combined premium per entry
    entry_levels: list[str] = field(default_factory=list)      # e.g. ["50", "30", "20"]
    entry_lots: list[int] = field(default_factory=list)        # lots added per entry
    entry_times: list[datetime] = field(default_factory=list)
    average_entry: float = 0.0    # volume-weighted average combined premium
    booked_lots: int = 0          # lots already booked for profit
    booked_levels: list[str] = field(default_factory=list)     # profit targets already taken
    sl_price: float = 0.0         # combined-premium stop level
    exit_sent: bool = False       # whether a full EXIT signal has been emitted
    last_entry_time: datetime | None = None
    # Latest market snapshot (kept even when no position is open).
    last_spot: float = 0.0
    last_ce_premium: float = 0.0
    last_pe_premium: float = 0.0
    last_combined: float = 0.0
    last_action: str = ""
    last_action_time: datetime | None = None
    last_exit_reason: str = ""


class NiftyHedge23Strategy(Strategy):
    """Nifty Hedge 23 - expiry-day long-straddle hedge."""

    def __init__(self) -> None:
        self._state = _HedgeState()
        self._today: date | None = None
        self._snapshot: dict = {}
        self._snapshot_at: datetime | None = None
        # The scheduler and the dashboard API may touch the strategy at the
        # same time; one lock keeps the position state consistent.
        self._lock = threading.RLock()

    # -- metadata -----------------------------------------------------------
    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Nifty Hedge 23",
            description=(
                "Expiry-day long-straddle hedge: buy ATM Nifty CE + PE "
                "together when combined premium enters the 17-28 zone. "
                "Simple single-entry or 50-30-20 averaging mode. "
                "Advisory only - never places trades."
            ),
            category=Category.INTRADAY,
            universe="NIFTY_HEDGE",  # no framework-managed universe
            schedule_type="auto",
            timeframes=[],           # we fetch quotes directly, not candles
            frequency_minutes=5,     # check every 5 min during market hours
        )

    def get_universe(self, settings: Settings) -> list[str]:
        """No candle universe - the strategy fetches option quotes directly."""
        return []

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
        """Fetch fresh quotes for the dashboard, throttled by the TTL.

        The scheduler already updates the snapshot every five minutes. The
        dashboard polls more often, so this only refetches when the cached
        snapshot is older than :data:`SNAPSHOT_TTL_SECONDS`.
        """
        with self._lock:
            if self._snapshot_at is not None:
                age = (datetime.now(IST) - self._snapshot_at).total_seconds()
                if age < SNAPSHOT_TTL_SECONDS:
                    return self.snapshot()

            now = datetime.now(IST)
            if not settings.hedge_23_enabled:
                self._base_snapshot(settings, now, next_weekly_expiry(now.date()),
                                    is_nifty_expiry_day(now.date()))
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
            except Exception as exc:  # noqa: BLE001 - never crash the API
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
            self._apply_quote_snapshot(
                settings, now, spot, ce, pe, combined, ce_sym, pe_sym, strike
            )
            self._set_status(now)
            return self.snapshot()

    # -- main scan ----------------------------------------------------------
    def scan(
        self,
        data: dict,  # unused - we fetch quotes directly
        settings: Settings,
    ) -> list[Signal]:
        with self._lock:
            now = datetime.now(IST)

            if not settings.hedge_23_enabled:
                self._base_snapshot(
                    settings, now, next_weekly_expiry(now.date()),
                    is_nifty_expiry_day(now.date()),
                )
                self._snapshot["status"] = "disabled"
                return []

            # Reset per-day state on a new trading day (also initialises the
            # mode from settings on the very first run of the process).
            self._reset_for_day(settings, now)

            expiry = next_weekly_expiry(now.date())
            expiry_day = is_nifty_expiry_day(now.date())
            self._base_snapshot(settings, now, expiry, expiry_day)

            # Only trade on a Nifty expiry day (normally Tuesday; a holiday
            # can shift it to Monday).
            if not expiry_day:
                self._snapshot["status"] = "not_expiry_day"
                return []

            signals: list[Signal] = []

            # Fetch live data: Nifty spot + ATM CE + PE quotes. This happens
            # even before 13:00 so the dashboard can show the tentative legs.
            fetch_error: str | None = None
            spot = ce = pe = None
            ce_sym = pe_sym = ""
            strike = 0
            try:
                spot, ce, pe, ce_sym, pe_sym, strike = self._fetch_leg_data(
                    settings, now
                )
            except Exception as exc:  # noqa: BLE001 - never crash the scheduler
                fetch_error = str(exc)
                logger.warning("Hedge 23: failed to fetch option data: %s", exc)

            combined = self._state.last_combined
            if spot is not None and ce is not None and pe is not None:
                combined = round(ce + pe, 2)
                self._apply_quote_snapshot(
                    settings, now, spot, ce, pe, combined, ce_sym, pe_sym, strike
                )
            else:
                self._snapshot["status"] = "data_unavailable"
                if fetch_error:
                    self._snapshot["error"] = fetch_error

            # Hard exit at 3:30 PM. Use the last known premium if the final
            # fetch failed, so a position is never left open silently.
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

            # Before 1:00 PM: show the tentative legs, but do not enter.
            if now.time() < ENTRY_START:
                self._set_status(now)
                return signals

            # --- position management -------------------------------------
            if self._state.entered:
                # 1) Hard stop loss.
                sl_reason = self._check_sl(combined)
                if sl_reason:
                    signals.append(
                        self._emit_exit(now, sl_reason, combined, settings)
                    )
                    self._close_position(now, sl_reason)
                    logger.info(
                        "Hedge 23: EXIT %s at combined=%.2f", sl_reason, combined
                    )
                    return signals

                # 2) Partial profit booking (book some lots, trail the rest).
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

                # 3) Averaging scale-in while the position is open.
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

            # --- fresh entry ---------------------------------------------
            entry_level = self._should_enter(now, combined, settings)
            if entry_level is None:
                self._set_status(now)
                return signals

            # Simple mode sizes the whole straddle from the capital cap;
            # averaging mode adds the fixed 2-lot ladder step within the
            # remaining cap.
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
                "Hedge 23: ENTRY %s at combined=%.2f lots=%d strike=%d",
                entry_level, combined, lots, strike,
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
            },
            "averaging_levels": [AVG_LEVEL_1, AVG_LEVEL_2, AVG_LEVEL_3],
            "sl": {
                "simple": SL_SIMPLE,
                "averaging": SL_AVERAGING,
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

        self._snapshot.update(
            {
                "spot": round(spot, 2),
                "atm_strike": strike,
                "ce_symbol": ce_sym,
                "pe_symbol": pe_sym,
                "ce_premium": round(ce, 2),
                "pe_premium": round(pe, 2),
                "combined_premium": combined,
                "in_entry_zone": ENTRY_ZONE_LOW <= combined <= ENTRY_ZONE_HIGH,
                "next_level": next_level,
                "recommended_lots": recommended,
                "cost_per_lot": round(combined * settings.hedge_23_lot_size, 2),
                "recommended_cost": round(
                    recommended * combined * settings.hedge_23_lot_size, 2
                ),
                "tentative": {
                    "strike": strike,
                    "ce_symbol": ce_sym,
                    "pe_symbol": pe_sym,
                    "ce_premium": round(ce, 2),
                    "pe_premium": round(pe, 2),
                    "combined_premium": combined,
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
            "booked_lots": st.booked_lots,
            "booked_levels": list(st.booked_levels),
            "entry_levels": list(st.entry_levels),
            "entry_premiums": list(st.entry_premiums),
            "entry_lots": list(st.entry_lots),
            "entry_times": [t.isoformat() for t in st.entry_times],
            "sl_price": st.sl_price,
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
            return "simple" if ENTRY_ZONE_LOW <= combined <= ENTRY_ZONE_HIGH else None

        levels = set(st.entry_levels)
        if "50" not in levels and combined <= AVG_LEVEL_1:
            return "50"
        if "50" in levels and "30" not in levels and combined <= AVG_LEVEL_2:
            return "30"
        if "30" in levels and "20" not in levels and combined <= AVG_LEVEL_3:
            return "20"
        return None

    # -- data fetching ------------------------------------------------------
    def _fetch_leg_data(
        self, settings: Settings, now: datetime
    ) -> tuple[float | None, float | None, float | None, str, str, int]:
        """Return (spot, ce_premium, pe_premium, ce_symbol, pe_symbol, strike).

        Uses FYERS quotes directly. The ATM strike is derived from the Nifty
        spot and the symbols use the correct weekly/monthly FYERS format.
        """
        if self.provider is None:
            raise RuntimeError("Hedge 23: no market-data provider set")

        expiry = next_weekly_expiry(now.date())

        # Nifty spot quote for the ATM strike.
        spot_q = self.provider.get_quotes([NIFTY_SPOT_SYMBOL])
        spot_quote = spot_q.get(NIFTY_SPOT_SYMBOL)
        if spot_quote is None or not spot_quote.ltp:
            return None, None, None, "", "", 0
        spot = float(spot_quote.ltp)

        strike = round_to_nifty_strike(spot)

        # Build paired CE/PE candidates (correct format first) and resolve
        # them in one batched quote request.
        ce_candidates = nifty_option_symbol_candidates(strike, "CE", expiry)
        pe_candidates = nifty_option_symbol_candidates(strike, "PE", expiry)
        pairs = list(zip(ce_candidates, pe_candidates, strict=False))
        symbols = [sym for pair in pairs for sym in pair]
        quotes = self.provider.get_quotes(symbols)

        ce_sym = pe_sym = ""
        ce_premium = pe_premium = None
        for ce_candidate, pe_candidate in pairs:
            ce_q = quotes.get(ce_candidate)
            pe_q = quotes.get(pe_candidate)
            if (
                ce_q is not None
                and ce_q.ltp is not None
                and pe_q is not None
                and pe_q.ltp is not None
            ):
                ce_sym, pe_sym = ce_candidate, pe_candidate
                ce_premium = float(ce_q.ltp)
                pe_premium = float(pe_q.ltp)
                break

        if not ce_sym or not pe_sym:
            logger.warning(
                "Hedge 23: no valid option symbols for strike %d expiry %s "
                "(tried %s)",
                strike, expiry.isoformat(), ce_candidates,
            )
            return spot, None, None, "", "", strike

        return spot, ce_premium, pe_premium, ce_sym, pe_sym, strike

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
            return None

        # Averaging mode (50-30-20). Adds are allowed while a position is open.
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
            # Fresh position: clear any state left by an earlier trade.
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
            st.sl_price = SL_SIMPLE if st.mode == "simple" else SL_AVERAGING
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

        st.strike = strike
        st.ce_symbol = ce_sym
        st.pe_symbol = pe_sym
        st.last_entry_time = now
        st.last_action = "ADD" if is_add else "ENTRY"
        st.last_action_time = now

        logger.info(
            "Hedge 23: position state lots=%d avg=%.2f sl=%.2f levels=%s",
            st.lots, st.average_entry, st.sl_price, st.entry_levels,
        )

    # -- exit logic ---------------------------------------------------------
    def _check_sl(self, combined: float) -> str | None:
        """Return ``"sl"`` when the combined premium breaches the stop."""
        if self._state.sl_price > 0 and combined <= self._state.sl_price:
            return "sl"
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
            if (
                "simple" not in st.booked_levels
                and st.average_entry > 0
                and combined >= st.average_entry * 2
            ):
                lots = max(1, st.lots // 2)
                return "profit_half", min(lots, remaining), "simple"
            return None

        # Averaging mode: per-level profit targets, each booked once.
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
        # Keep strike/symbols/last premiums for the snapshot, but clear the
        # position-specific fields so the next entry starts cleanly.
        st.lots = 0
        st.entry_premiums = []
        st.entry_levels = []
        st.entry_lots = []
        st.entry_times = []
        st.average_entry = 0.0
        st.booked_lots = 0
        st.booked_levels = []
        st.sl_price = 0.0
        # Keep ``last_entry_time`` so the configured minimum gap also applies
        # to a re-entry after an exit.
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
        """Number of straddle lots for a fresh entry (never breaches the cap).

        One straddle lot costs ``combined * lot_size`` because the combined
        premium already includes both the CE and the PE leg.
        """
        cost_per_lot = combined * settings.hedge_23_lot_size
        if cost_per_lot <= 0:
            return 0
        lots = int(self._max_capital(settings) / cost_per_lot)
        # Round down to the nearest even number for the half-exit rule.
        return (lots // 2) * 2

    def _add_size(self, settings: Settings, combined: float) -> int:
        """Lots available for an averaging add within the remaining cap."""
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
                "strike": st.strike,
                "ce_symbol": st.ce_symbol,
                "pe_symbol": st.pe_symbol,
                "ce_premium": st.last_ce_premium,
                "pe_premium": st.last_pe_premium,
                "combined_premium": combined,
                "lots": lots,
                "total_lots": st.lots,
                "entry_level": entry_level,
                "average_entry": st.average_entry,
                "sl_price": st.sl_price,
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
                "ce_symbol": st.ce_symbol,
                "pe_symbol": st.pe_symbol,
                "combined_premium": combined,
                "lots_booked": lots,
                "booked_lots": st.booked_lots + lots,
                "remaining_lots": max(0, st.lots - st.booked_lots - lots),
                "entry_average": st.average_entry,
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
            # (exit - average entry) x remaining lots x lot_size. The combined
            # premium already covers both legs, so there is no x2 here.
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
                "ce_symbol": st.ce_symbol,
                "pe_symbol": st.pe_symbol,
                "combined_premium": combined,
                "entry_average": st.average_entry,
                "lots": st.lots,
                "booked_lots": st.booked_lots,
                "remaining_lots": remaining,
                "pnl_estimate": pnl,
                "lot_size": settings.hedge_23_lot_size,
            },
        )


# Registry hook: the strategy framework discovers this module.
strategy_class = NiftyHedge23Strategy
