"""Tests for the Nifty Hedge 23 expiry-day straddle/strangle strategy."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

import app.strategies.nifty_hedge_23 as hedge
from app.config import Settings
from app.data.options import (
    NIFTY_SPOT_SYMBOL,
    nifty_option_symbol,
    round_to_nifty_strike,
)
from app.data.provider import Quote
from app.strategies.nifty_hedge_23 import (
    NiftyHedge23Strategy,
    classify_nifty_movement,
)

IST = hedge.IST


class _Clock:
    """A controllable stand-in for ``datetime`` (only ``now`` is needed)."""

    current = datetime(2026, 11, 3, 10, 0, tzinfo=IST)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


@pytest.fixture
def clock(monkeypatch):
    """Freeze the strategy clock at a chosen IST time on 3 Nov 2026."""
    monkeypatch.setattr(hedge, "datetime", _Clock)
    _Clock.current = datetime(2026, 11, 3, 10, 0, tzinfo=IST)

    def at(hour: int, minute: int, day: int = 3) -> None:
        _Clock.current = datetime(2026, 11, day, hour, minute, tzinfo=IST)

    return at


class _OptionProvider:
    """Quote provider for the Nifty spot and selected CE/PE strikes."""

    def __init__(
        self,
        spot: float = 26012.0,
        ce: float = 12.3,
        pe: float = 9.1,
        expiry: date = date(2026, 11, 3),
        *,
        premiums: dict[int, dict[str, float]] | None = None,
    ) -> None:
        self.spot = spot
        self.expiry = expiry
        self.strike = round_to_nifty_strike(spot)
        self.premiums: dict[int, dict[str, float]] = {
            self.strike: {"CE": ce, "PE": pe}
        }
        if premiums:
            self.premiums.update(premiums)
        self.quote_calls = 0

    def set_premiums(self, ce: float, pe: float) -> None:
        self.premiums[self.strike] = {"CE": ce, "PE": pe}

    def set_strike_premiums(self, strike: int, ce: float, pe: float) -> None:
        self.premiums[strike] = {"CE": ce, "PE": pe}

    def _symbol_map(self) -> dict[str, float]:
        mapping: dict[str, float] = {}
        for strike, legs in self.premiums.items():
            for option_type, premium in legs.items():
                mapping[nifty_option_symbol(strike, option_type, self.expiry)] = (
                    premium
                )
        return mapping

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self.quote_calls += 1
        mapping = self._symbol_map()
        quotes: dict[str, Quote] = {}
        for symbol in symbols:
            if symbol == NIFTY_SPOT_SYMBOL:
                quotes[symbol] = Quote(symbol, self.spot)
            elif symbol in mapping:
                quotes[symbol] = Quote(symbol, mapping[symbol])
        return quotes


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_path": "data/test.db",
        "hedge_23_enabled": True,
        "hedge_23_mode": "simple",
        "hedge_23_capital": 100_000.0,
        "hedge_23_max_capital_pct": 0.05,
        "hedge_23_lot_size": 65,
        "hedge_23_min_entry_gap_minutes": 5,
    }
    values.update(overrides)
    return Settings(**values)


def _strategy(provider: _OptionProvider | None = None) -> NiftyHedge23Strategy:
    strategy = NiftyHedge23Strategy()
    strategy.provider = provider or _OptionProvider()
    return strategy


def _ten_min_frame(rows: list[list[float]]) -> pd.DataFrame:
    """Build a 10-minute Nifty frame starting at 09:15 IST on 3 Nov 2026."""
    index = pd.date_range(
        "2026-11-03 09:15", periods=len(rows), freq="10min", tz=IST
    )
    return pd.DataFrame(
        rows, index=index, columns=["open", "high", "low", "close", "volume"]
    )


def _trending_bullish_frame() -> pd.DataFrame:
    rows = []
    for i in range(6):
        open_ = 26000 + i * 20
        close = open_ + 18
        rows.append([open_, close + 5, open_ - 5, close, 1000])
    return _ten_min_frame(rows)


def _chop_frame() -> pd.DataFrame:
    rows = []
    for i in range(6):
        open_ = 26000 if i % 2 == 0 else 26005
        close = 26005 if i % 2 == 0 else 26000
        rows.append([open_, 26010, 25995, close, 1000])
    return _ten_min_frame(rows)


def _coiled_frame() -> pd.DataFrame:
    sequence = [
        (26000, 26040),
        (26040, 26080),
        (26080, 26100),
        (26100, 26060),
        (26060, 26020),
        (26020, 26010),
        (26010, 26000),
        (26000, 26010),
        (26010, 26000),
        (26000, 26010),
        (26010, 26000),
        (26000, 26005),
    ]
    rows = [
        [open_, max(open_, close) + 8, min(open_, close) - 8, close, 1000]
        for open_, close in sequence
    ]
    return _ten_min_frame(rows)


# -- movement classification -------------------------------------------------
def test_classify_trending_bullish() -> None:
    movement = classify_nifty_movement(_trending_bullish_frame(), 26115)

    assert movement["is_trending"] is True
    assert movement["behaviour"] == "TRENDING_BULLISH"
    assert movement["bias"] == "BULLISH"
    assert movement["day_range_pts"] >= 60.0


def test_classify_range_bound_chop() -> None:
    movement = classify_nifty_movement(_chop_frame(), 26002)

    assert movement["is_trending"] is False
    assert movement["behaviour"] == "RANGE_BOUND_CHOP"
    assert movement["bias"] == "NEUTRAL"


def test_classify_coiled_for_surge() -> None:
    movement = classify_nifty_movement(_coiled_frame(), 26005)

    assert movement["is_trending"] is True
    assert movement["behaviour"] == "COILED_FOR_SURGE"


def test_classify_quote_fallback_without_ohlc_allows_trade() -> None:
    movement = classify_nifty_movement(None, 26000)

    assert movement["is_trending"] is True
    assert movement["behaviour"] == "TRENDING_READY"


# -- snapshots / tentative legs ---------------------------------------------
def test_snapshot_before_entry_window(clock) -> None:
    clock(10, 0)
    strategy = _strategy()

    signals = strategy.scan({}, _settings())

    assert signals == []
    snap = strategy.snapshot()
    assert snap["status"] == "waiting_entry_window"
    assert snap["is_expiry_day"] is True
    assert snap["expiry"] == "2026-11-03"
    assert snap["expiry_kind"] == "weekly"
    assert snap["atm_strike"] == 26000
    assert snap["ce_strike"] == 26000
    assert snap["pe_strike"] == 26000
    assert snap["radius_pts"] == 0
    assert snap["ce_symbol"] == "NSE:NIFTY26N0326000CE"
    assert snap["pe_symbol"] == "NSE:NIFTY26N0326000PE"
    assert snap["combined_premium"] == 21.4
    assert snap["in_entry_zone"] is True
    assert snap["legs_balanced"] is True
    assert snap["recommended_lots"] == 2
    assert snap["sl_price"] == 10.0
    assert snap["target_1"] == 42.8
    assert snap["target_2"] == 53.5
    assert snap["tentative"]["strike"] == 26000


def test_snapshot_exposes_movement_and_radius(clock) -> None:
    clock(10, 0)
    provider = _OptionProvider(
        premiums={
            26050: {"CE": 20.0},
            25950: {"PE": 18.0},
            26000: {"CE": 60.0, "PE": 60.0},
        }
    )
    strategy = _strategy(provider)

    strategy.scan({}, _settings())

    snap = strategy.snapshot()
    assert snap["radius_pts"] == 50
    assert snap["ce_strike"] == 26050
    assert snap["pe_strike"] == 25950
    assert snap["combined_premium"] == 38.0
    assert snap["movement"]["behaviour"] in {
        "TRENDING_READY",
        "TRENDING_BULLISH",
        "TRENDING_BEARISH",
        "COILED_FOR_SURGE",
        "RANGE_BOUND_CHOP",
    }


# -- entry logic -------------------------------------------------------------
def test_entry_in_classic_zone_emits_straddle(clock) -> None:
    clock(13, 5)
    strategy = _strategy()

    signals = strategy.scan({}, _settings())

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["action"] == "ENTRY"
    assert meta["lots"] == 2
    assert meta["combined_premium"] == 21.4
    assert meta["strike"] == 26000
    assert meta["ce_symbol"] == "NSE:NIFTY26N0326000CE"
    assert meta["pe_symbol"] == "NSE:NIFTY26N0326000PE"
    assert meta["entry_level"] == "simple"
    assert meta["entry"] == 21.4
    assert meta["stop"] == 10.0
    assert meta["sl_price"] == 10.0
    assert meta["target_1"] == 42.8
    assert meta["target_2"] == 53.5
    assert meta["time_sl"] == "14:35 IST"

    snap = strategy.snapshot()
    assert snap["status"] == "position_open"
    assert snap["position"]["open"] is True
    assert snap["position"]["lots"] == 2


def test_open_position_keeps_entered_strikes_when_spot_moves(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider()
    strategy = _strategy(provider)
    settings = _settings()
    strategy.scan({}, settings)  # entry at 26000, combined 21.4

    clock(13, 10)
    provider.spot = 26300.0  # ATM moves; a re-selection would pick 26350/26250
    provider.set_strike_premiums(26000, 14.0, 11.0)  # held legs, combined 25
    provider.set_strike_premiums(26350, 20.0, 0.0)
    provider.premiums[26250] = {"PE": 18.0}
    provider.premiums[26300] = {"CE": 60.0, "PE": 60.0}

    signals = strategy.scan({}, settings)

    assert signals == []
    snap = strategy.snapshot()
    assert snap["position"]["open"] is True
    assert snap["position"]["ce_strike"] == 26000
    assert snap["position"]["pe_strike"] == 26000
    assert snap["position"]["radius_pts"] == 0
    assert snap["ce_strike"] == 26000
    assert snap["pe_strike"] == 26000
    assert snap["ce_symbol"] == "NSE:NIFTY26N0326000CE"
    assert snap["combined_premium"] == 25.0


def test_extended_zone_entry_uses_18_sl_and_targets(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider(ce=25.0, pe=20.0)  # combined 45 -> extended zone
    strategy = _strategy(provider)
    settings = _settings(hedge_23_capital=1_000_000)

    signals = strategy.scan({}, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["entry_level"] == "extended"
    assert meta["combined_premium"] == 45.0
    assert meta["sl_price"] == 18.0
    assert meta["target_1"] == 90.0
    assert meta["target_2"] == 112.5
    assert meta["lots"] >= 2


def test_no_entry_outside_both_zones(clock) -> None:
    clock(13, 5)
    strategy = _strategy(_OptionProvider(ce=18.0, pe=12.0))  # combined 30

    signals = strategy.scan({}, _settings())

    assert signals == []
    snap = strategy.snapshot()
    assert snap["status"] == "waiting_zone"
    assert snap["in_entry_zone"] is False


def test_movement_gate_blocks_entry_in_chop(clock) -> None:
    clock(13, 5)
    strategy = _strategy()  # combined 21.4 would normally enter
    data = {NIFTY_SPOT_SYMBOL: {"10m": _chop_frame()}}

    signals = strategy.scan(data, _settings())

    assert signals == []
    snap = strategy.snapshot()
    assert snap["status"] == "range_bound_wait"
    assert snap["next_action"] == "WAIT_TREND"
    assert snap["movement"]["behaviour"] == "RANGE_BOUND_CHOP"
    assert "RANGE_BOUND_CHOP" in snap["note"]


def test_radius_50_pair_selected_when_in_zone(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider(
        premiums={
            26000: {"CE": 60.0, "PE": 60.0},   # ATM combined 120 (outside)
            26050: {"CE": 20.0},               # radius 50 CE
            25950: {"PE": 18.0},               # radius 50 PE -> combined 38
        }
    )
    strategy = _strategy(provider)
    settings = _settings(hedge_23_capital=1_000_000)

    signals = strategy.scan({}, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["radius_pts"] == 50
    assert meta["ce_strike"] == 26050
    assert meta["pe_strike"] == 25950
    assert meta["ce_symbol"] == "NSE:NIFTY26N0326050CE"
    assert meta["pe_symbol"] == "NSE:NIFTY26N0325950PE"
    assert meta["combined_premium"] == 38.0
    assert meta["entry_level"] == "extended"


def test_radius_100_pair_selected_when_50_is_outside(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider(
        premiums={
            26000: {"CE": 60.0, "PE": 60.0},   # ATM 120
            26050: {"CE": 60.0},               # radius 50 120
            25950: {"PE": 60.0},
            26100: {"CE": 21.0},               # radius 100 combined 40
            25900: {"PE": 19.0},
        }
    )
    strategy = _strategy(provider)
    settings = _settings(hedge_23_capital=1_000_000)

    signals = strategy.scan({}, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["radius_pts"] == 100
    assert meta["ce_strike"] == 26100
    assert meta["pe_strike"] == 25900


def test_unbalanced_extended_pair_is_skipped(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider(
        premiums={
            26000: {"CE": 80.0, "PE": 30.0},   # ATM 110, unbalanced
            26050: {"CE": 40.0},               # radius 50 50, unbalanced
            25950: {"PE": 10.0},
        }
    )
    strategy = _strategy(provider)
    settings = _settings(hedge_23_capital=1_000_000)

    signals = strategy.scan({}, settings)

    assert signals == []
    snap = strategy.snapshot()
    assert snap["radius_pts"] == 0  # fall back to ATM
    assert snap["legs_balanced"] is False
    assert snap["in_entry_zone"] is False


def test_averaging_mode_scales_in(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider(ce=30.0, pe=20.0)  # combined 50
    strategy = _strategy(provider)
    settings = _settings(hedge_23_mode="averaging", hedge_23_capital=1_000_000)

    signals = strategy.scan({}, settings)
    assert len(signals) == 1
    assert signals[0].metadata["entry_level"] == "50"
    assert signals[0].metadata["lots"] == 2

    clock(13, 15)
    provider.set_premiums(18.0, 11.0)  # combined 29 -> level 30
    signals = strategy.scan({}, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["action"] == "ADD"
    assert meta["entry_level"] == "30"
    assert meta["total_lots"] == 4

    snap = strategy.snapshot()
    assert snap["position"]["open"] is True
    assert snap["position"]["lots"] == 4
    assert snap["position"]["entry_levels"] == ["50", "30"]
    assert snap["position"]["average_entry"] == 39.5


def test_position_size_never_exceeds_capital_cap(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider()  # combined 21.4
    strategy = _strategy(provider)
    # 2% of 100,000 = 2,000. One lot costs 21.4 x 65 = 1,391, but two lots
    # cost 2,782 > 2,000, so no even lot fits and the entry is skipped.
    settings = _settings(hedge_23_max_capital_pct=0.02)

    signals = strategy.scan({}, settings)
    assert signals == []
    snap = strategy.snapshot()
    assert snap["recommended_lots"] == 0
    assert "capital cap" in snap["note"]

    # At combined 20 with the default 5% cap, two lots cost 2,600 <= 5,000.
    provider.set_premiums(12.0, 8.0)
    clock(13, 15)
    signals = strategy.scan({}, _settings())
    assert len(signals) == 1
    assert signals[0].metadata["lots"] == 2


# -- exits -------------------------------------------------------------------
def test_stop_loss_exits(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider()
    strategy = _strategy(provider)
    settings = _settings()
    strategy.scan({}, settings)

    clock(13, 10)
    provider.set_premiums(5.0, 4.0)  # combined 9 <= SL 10
    signals = strategy.scan({}, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["action"] == "EXIT"
    assert meta["reason"] == "sl"
    assert strategy.snapshot()["position"]["open"] is False


def test_time_based_sl_exits_range_bound_decay(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider()
    strategy = _strategy(provider)
    settings = _settings()
    strategy.scan({}, settings)  # entry at combined 21.4

    clock(14, 40)
    provider.set_premiums(9.0, 7.0)  # combined 16, above hard SL 10
    data = {NIFTY_SPOT_SYMBOL: {"10m": _chop_frame()}}
    signals = strategy.scan(data, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["action"] == "EXIT"
    assert meta["reason"] == "time_sl"


def test_partial_profit_books_half_and_keeps_position(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider()
    strategy = _strategy(provider)
    settings = _settings()
    strategy.scan({}, settings)  # entry at combined 21.4

    clock(13, 10)
    provider.set_premiums(25.0, 20.0)  # combined 45 >= 2 x 21.4, < 2.5x
    signals = strategy.scan({}, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["action"] == "BOOK"
    assert meta["lots_booked"] == 1
    assert meta["remaining_lots"] == 1

    snap = strategy.snapshot()
    assert snap["position"]["open"] is True
    assert snap["position"]["booked_lots"] == 1
    assert snap["position"]["remaining_lots"] == 1


def test_target_2_closes_full_position(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider()
    strategy = _strategy(provider)
    settings = _settings()
    strategy.scan({}, settings)  # entry at 21.4, target_2 = 53.5

    clock(13, 10)
    provider.set_premiums(30.0, 24.0)  # combined 54 >= target_2
    signals = strategy.scan({}, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["action"] == "EXIT"
    assert meta["reason"] == "target_2"
    assert strategy.snapshot()["position"]["open"] is False


def test_market_close_exit(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider()
    strategy = _strategy(provider)
    settings = _settings()
    strategy.scan({}, settings)

    clock(15, 30)
    provider.set_premiums(15.0, 10.0)  # combined 25
    signals = strategy.scan({}, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["action"] == "EXIT"
    assert meta["reason"] == "market_close"
    assert meta["combined_premium"] == 25.0
    assert strategy.snapshot()["status"] == "market_closed"


def test_reentry_after_exit_starts_fresh(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider()
    strategy = _strategy(provider)
    settings = _settings()
    strategy.scan({}, settings)  # entry at 21.4

    clock(13, 10)
    provider.set_premiums(5.0, 4.0)  # SL at combined 9
    strategy.scan({}, settings)

    clock(13, 20)
    provider.set_premiums(12.0, 10.0)  # combined 22, back in the zone
    signals = strategy.scan({}, settings)

    assert len(signals) == 1
    meta = signals[0].metadata
    assert meta["action"] == "ENTRY"
    assert meta["entry_level"] == "simple"

    snap = strategy.snapshot()
    assert snap["position"]["entry_levels"] == ["simple"]
    assert snap["position"]["average_entry"] == 22.0
    assert snap["position"]["lots"] == 2
    assert snap["position"]["booked_lots"] == 0


# -- calendar / setup --------------------------------------------------------
def test_non_expiry_day_returns_no_signal(clock) -> None:
    clock(13, 5, day=4)  # Wednesday 4 Nov 2026
    strategy = _strategy()

    signals = strategy.scan({}, _settings())

    assert signals == []
    snap = strategy.snapshot()
    assert snap["is_expiry_day"] is False
    assert snap["status"] == "not_expiry_day"
    assert snap["expiry"] == "2026-11-09"  # 10 Nov is a holiday
    assert snap["expiry_kind"] == "weekly"


def test_mode_initialised_from_settings_on_first_scan(clock) -> None:
    clock(10, 0)
    strategy = _strategy()

    strategy.scan({}, _settings(hedge_23_mode="averaging"))

    assert strategy.snapshot()["mode"] == "averaging"


def test_refresh_snapshot_fetches_live_quotes(clock) -> None:
    clock(10, 0)
    strategy = _strategy()

    snap = strategy.refresh_snapshot(_settings())

    assert snap["status"] == "waiting_entry_window"
    assert snap["combined_premium"] == 21.4
    assert snap["recommended_lots"] == 2
