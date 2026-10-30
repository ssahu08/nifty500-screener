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

* Trade only on Nifty weekly expiry day (Tuesday). SEBI directive (Oct 2024):
  only one weekly index per exchange; NSE kept Nifty weekly on Tuesday from
  1 Sep 2025.
* Entry window: after 1:00 PM IST (preferred 1:15-1:30 PM). Time decay has
  accelerated by then, pulling premiums into the zone.
* Stop loss: combined premium below 10 (simple) or 8 (averaging).
* Time stop: after 2:35 PM, if the market is range-bound the SL should not
  hit; time premium decay is in your favour.
* Hard exit: all positions closed at 3:30 PM (CAS candle) - never hold to
  expiry.
* Position sizing: max 5% of capital per trade (configurable), minimum 2 lots
  so that half can be booked and the rest trailed.
* No fixed target - profit is unlimited. Book half when the premium doubles
  (simple) or at the per-level zones (averaging), trail the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings
from app.core.logging import get_logger
from app.data.options import (
    NIFTY_SPOT_SYMBOL,
    next_weekly_expiry,
    nifty_option_symbol,
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
SL_SIMPLE = 10.0    # simple mode: exit when combined premium < 10
SL_AVERAGING = 8.0   # averaging mode: exit when combined premium < 8

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


@dataclass
class _HedgeState:
    """Mutable internal state for one running hedge position.

    Persists across scheduler ticks because the registry keeps a single
    strategy instance for the life of the process.
    """

    mode: str = "simple"          # "simple" or "averaging"
    entered: bool = False         # whether a position is currently open
    expiry: str = ""              # expiry date string (e.g. "25JUN2026")
    strike: int = 0               # ATM strike
    ce_symbol: str = ""           # FYERS symbol for the CE leg
    pe_symbol: str = ""           # FYERS symbol for the PE leg
    lots: int = 0                 # total straddle lots (1 lot = 1 CE + 1 PE)
    entry_premiums: list[float] = field(default_factory=list)  # combined premium at each entry
    entry_levels: list[str] = field(default_factory=list)      # e.g. ["50", "30", "20"]
    entry_times: list[datetime] = field(default_factory=list)
    average_entry: float = 0.0    # volume-weighted average combined premium
    booked_lots: int = 0          # lots already booked for profit
    sl_price: float = 0.0         # combined-premium stop level
    exit_sent: bool = False       # whether an EXIT signal has been emitted
    last_entry_time: datetime | None = None


class NiftyHedge23Strategy(Strategy):
    """Nifty Hedge 23 - expiry-day long-straddle hedge."""

    def __init__(self) -> None:
        self._state = _HedgeState()
        self._today = datetime.now(IST).date()

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
        """Nifty spot symbol — needed so the scheduler includes it in shared data.

        The strategy fetches option quotes directly via the provider, but the
        scheduler needs at least one symbol in ``data`` to invoke scan().
        """
        return [NIFTY_SPOT_SYMBOL]

    def prefilter(self, quotes, settings):  # type: ignore[override]
        """Not used - quotes are fetched directly inside scan()."""
        return None

    # -- main scan ----------------------------------------------------------
    def scan(
        self,
        data: dict,  # unused - we fetch quotes directly
        settings: Settings,
    ) -> list[Signal]:
        if not settings.hedge_23_enabled:
            return []

        now = datetime.now(IST)

        # Reset per-day state on a new trading day.
        if now.date() != self._today:
            self._today = now.date()
            self._state = _HedgeState(mode=settings.hedge_23_mode)
            logger.info(
                "Hedge 23: new day %s, mode=%s", self._today, self._state.mode
            )

        signals: list[Signal] = []

        # Only trade on Nifty weekly expiry (Tuesday).
        # SEBI directive (Oct 2024): only one weekly index per exchange;
        # NSE kept Nifty weekly on Tuesday from 1 Sep 2025.
        if now.weekday() != 1:
            return signals

        # Entry window: after 1:00 PM. Hard exit at 3:30 PM.
        if now.time() < ENTRY_START:
            return signals
        if now.time() >= MARKET_EXIT:
            if self._state.entered and not self._state.exit_sent:
                signals.append(self._emit_exit(now, "market_close", settings))
                self._state.exit_sent = True
            return signals

        # Fetch live data: Nifty spot + ATM CE + PE quotes.
        try:
            spot, ce_premium, pe_premium, ce_sym, pe_sym, strike = self._fetch_leg_data(
                settings, now
            )
        except Exception as exc:  # noqa: BLE001 - never crash the scheduler
            logger.warning("Hedge 23: failed to fetch option data: %s", exc)
            return signals

        if spot is None or ce_premium is None or pe_premium is None:
            return signals

        combined = round(ce_premium + pe_premium, 2)

        # Update state with latest symbols/strike.
        self._state.ce_symbol = ce_sym
        self._state.pe_symbol = pe_sym
        self._state.strike = strike

        # --- EXIT checks (position open) ---------------------------------
        if self._state.entered:
            exit_reason = self._check_exit(now, combined, settings)
            if exit_reason:
                signals.append(self._emit_exit(now, exit_reason, combined, settings))
                self._state.entered = False
                self._state.exit_sent = True
                logger.info(
                    "Hedge 23: EXIT %s at combined=%.2f lots=%d",
                    exit_reason, combined, self._state.lots,
                )
            return signals

        # --- ENTRY checks (no position) ----------------------------------
        entry_level = self._should_enter(now, combined, settings)
        if entry_level is None:
            return signals

        lots = self._position_size(settings, combined)
        if lots < 2:
            logger.warning(
                "Hedge 23: position size %d lots < 2, skipping entry", lots
            )
            return signals

        self._enter_position(
            now, combined, ce_sym, pe_sym, strike, lots, entry_level, settings
        )
        signals.append(self._emit_entry(now, combined, lots, entry_level, settings))
        logger.info(
            "Hedge 23: ENTRY %s at combined=%.2f lots=%d strike=%d",
            entry_level, combined, lots, strike,
        )
        return signals

    # -- data fetching ------------------------------------------------------
    def _fetch_leg_data(
        self, settings: Settings, now: datetime
    ) -> tuple[float | None, float | None, float | None, str, str, int]:
        """Return (spot, ce_premium, pe_premium, ce_symbol, pe_symbol, strike).

        Uses FYERS quotes directly. ATM strike is derived from the Nifty spot.
        """
        if self.provider is None:
            raise RuntimeError("Hedge 23: no market-data provider set")

        # Expiry for this week (Tuesday). FYERS option symbols may use the
        # calendar expiry day or the day before; try both.
        expiry = next_weekly_expiry(now.date())
        self._state.expiry = expiry.strftime("%d%b%Y")

        # Try candidate expiry day formats: the calendar expiry and the day
        # before it (FYERS sometimes uses day-1 in option symbols).
        expiry_candidates = [expiry, expiry - timedelta(days=1)]

        # Nifty spot quote for ATM strike.
        spot_q = self.provider.get_quotes([NIFTY_SPOT_SYMBOL])
        spot_quote = spot_q.get(NIFTY_SPOT_SYMBOL)
        if spot_quote is None or not spot_quote.ltp:
            return None, None, None, "", "", 0
        spot = float(spot_quote.ltp)

        strike = round_to_nifty_strike(spot)

        # Find the expiry day format that FYERS accepts for option symbols.
        ce_sym = pe_sym = ""
        for exp in expiry_candidates:
            ce_candidate = nifty_option_symbol(strike, "CE", exp)
            pe_candidate = nifty_option_symbol(strike, "PE", exp)
            test_q = self.provider.get_quotes([ce_candidate, pe_candidate])
            ce_test = test_q.get(ce_candidate)
            pe_test = test_q.get(pe_candidate)
            # FYERS omits invalid symbols from the quotes response entirely
            # (they return errmsg="Please provide a valid symbol"), so a
            # valid symbol has a non-None Quote with an ltp.
            ce_valid = ce_test is not None and ce_test.ltp is not None
            pe_valid = pe_test is not None and pe_test.ltp is not None
            if ce_valid and pe_valid:
                ce_sym, pe_sym = ce_candidate, pe_candidate
                break

        if not ce_sym or not pe_sym:
            logger.warning("Hedge 23: no valid option symbol found for strike %d", strike)
            return None, None, None, "", "", 0

        opt_q = self.provider.get_quotes([ce_sym, pe_sym])
        ce_q = opt_q.get(ce_sym)
        pe_q = opt_q.get(pe_sym)

        ce_premium = float(ce_q.ltp) if ce_q and ce_q.ltp else None
        pe_premium = float(pe_q.ltp) if pe_q and pe_q.ltp else None

        return spot, ce_premium, pe_premium, ce_sym, pe_sym, strike

    # -- entry logic --------------------------------------------------------
    def _should_enter(
        self, now: datetime, combined: float, settings: Settings
    ) -> str | None:
        """Return the entry level string if we should enter, else None."""
        if self._state.entered:
            return None

        mode = self._state.mode

        if mode == "simple":
            if ENTRY_ZONE_LOW <= combined <= ENTRY_ZONE_HIGH:
                return "simple"
            return None

        # Averaging mode (50-30-20).
        levels = self._state.entry_levels
        if AVG_LEVEL_1 not in levels and combined <= AVG_LEVEL_1:
            return "50"
        if "50" in levels and AVG_LEVEL_2 not in levels and combined <= AVG_LEVEL_2:
            return "30"
        if "30" in levels and AVG_LEVEL_3 not in levels and combined <= AVG_LEVEL_3:
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
        st.entered = True
        st.strike = strike
        st.ce_symbol = ce_sym
        st.pe_symbol = pe_sym
        st.last_entry_time = now

        if not st.entry_levels:
            # First entry ever.
            st.lots = lots
            st.entry_premiums = [combined]
            st.entry_levels = [entry_level]
            st.entry_times = [now]
            st.average_entry = combined
            st.sl_price = SL_SIMPLE if st.mode == "simple" else SL_AVERAGING
        else:
            # Adding to an averaging position.
            st.lots += lots
            st.entry_premiums.append(combined)
            st.entry_levels.append(entry_level)
            st.entry_times.append(now)
            # Volume-weighted average across all entry levels.
            # Each level added LOTS_PER_LEVEL lots.
            level_lots = [LOTS_PER_LEVEL] * len(st.entry_premiums)
            # Adjust the last level to the actual lots added this time.
            level_lots[-1] = lots
            total_cost = sum(
                premium * qty
                for premium, qty in zip(
                    st.entry_premiums, level_lots, strict=False
                )
            )
            st.average_entry = round(total_cost / st.lots, 2)

        logger.info(
            "Hedge 23: position state lots=%d avg=%.2f sl=%.2f levels=%s",
            st.lots, st.average_entry, st.sl_price, st.entry_levels,
        )

    # -- exit logic ---------------------------------------------------------
    def _check_exit(
        self, now: datetime, combined: float, settings: Settings
    ) -> str | None:
        """Return an exit reason string if the position should be closed."""
        st = self._state

        # Hard stop loss (combined premium breach).
        if combined <= st.sl_price:
            return "sl"

        # Profit-take rules.
        pt_reason = self._check_profit_take(combined)
        if pt_reason:
            return pt_reason

        return None

    def _check_profit_take(self, combined: float) -> str | None:
        """Partial profit booking based on entry level and mode."""
        st = self._state

        if st.mode == "simple":
            # Book half when premium doubles from entry.
            if combined >= st.average_entry * 2 and st.booked_lots < st.lots // 2:
                return "profit_half"
            return None

        # Averaging mode: per-level profit targets.
        levels = st.entry_levels
        booked = st.booked_lots

        # Level-1 (50) entry: book 1 lot when premium reaches ~100 (double).
        if "50" in levels and booked < 1 and combined >= PT_LEVEL1_DOUBLE:
            return "profit_level1"

        # Level-2 (30) entry: book half (2 lots) at 65-70 zone.
        if "30" in levels and booked < 3 and PT_LEVEL2_ZONE_LOW <= combined <= PT_LEVEL2_ZONE_HIGH:
            return "profit_level2"

        # Level-3 (20) entry: book half (3 lots) at ~45.
        if "20" in levels and booked < 5 and combined >= PT_LEVEL3_ZONE:
            return "profit_level3"

        return None

    # -- position sizing ----------------------------------------------------
    def _position_size(self, settings: Settings, combined: float) -> int:
        """Number of straddle lots (1 lot = 1 CE + 1 PE)."""
        max_capital = settings.hedge_23_capital * settings.hedge_23_max_capital_pct
        lot_size = settings.hedge_23_lot_size
        cost_per_lot = combined * lot_size * 2  # CE + PE
        if cost_per_lot <= 0:
            return 0
        lots = int(max_capital / cost_per_lot)
        # Round down to nearest even number (for half-exit rule), min 2.
        lots = (lots // 2) * 2
        return max(lots, 2)

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
                "action": "ENTRY",
                "mode": st.mode,
                "expiry": st.expiry,
                "strike": st.strike,
                "ce_symbol": st.ce_symbol,
                "pe_symbol": st.pe_symbol,
                "combined_premium": combined,
                "lots": lots,
                "entry_level": entry_level,
                "average_entry": st.average_entry,
                "sl_price": st.sl_price,
                "capital": settings.hedge_23_capital,
                "lot_size": settings.hedge_23_lot_size,
            },
        )

    def _emit_exit(
        self, now: datetime, reason: str, combined: float, settings: Settings,
    ) -> Signal:
        st = self._state
        exit_price = combined
        pnl = None
        if st.average_entry > 0 and exit_price > 0:
            # Approximate P&L: (exit - entry) × lots × lot_size × 2 legs
            pnl = round(
                (exit_price - st.average_entry) * st.lots * settings.hedge_23_lot_size * 2,
                2,
            )
        return Signal(
            symbol="NIFTY HEDGE 23",
            strategy_name=self.metadata.name,
            timestamp=now,
            price=exit_price,
            metadata={
                "action": "EXIT",
                "mode": st.mode,
                "reason": reason,
                "expiry": st.expiry,
                "strike": st.strike,
                "ce_symbol": st.ce_symbol,
                "pe_symbol": st.pe_symbol,
                "combined_premium": exit_price,
                "entry_average": st.average_entry,
                "lots": st.lots,
                "booked_lots": st.booked_lots,
                "remaining_lots": st.lots - st.booked_lots,
                "pnl_estimate": pnl,
                "lot_size": settings.hedge_23_lot_size,
            },
        )


# Registry hook: the strategy framework discovers this module.
strategy_class = NiftyHedge23Strategy
