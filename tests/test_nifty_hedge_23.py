"""Tests for the Nifty Hedge 23 expiry-day straddle strategy."""

from __future__ import annotations

from datetime import date, datetime

import pytest

import app.strategies.nifty_hedge_23 as hedge
from app.config import Settings
from app.data.options import (
    NIFTY_SPOT_SYMBOL,
    nifty_option_symbol,
    round_to_nifty_strike,
)
from app.data.provider import Quote
from app.strategies.nifty_hedge_23 import NiftyHedge23Strategy

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
    """Minimal quote provider for the Nifty spot and the ATM CE/PE legs."""

    def __init__(
        self,
        spot: float = 26012.0,
        ce: float = 12.3,
        pe: float = 9.1,
        expiry: date = date(2026, 11, 3),
    ) -> None:
        self.spot = spot
        self.ce = ce
        self.pe = pe
        self.strike = round_to_nifty_strike(spot)
        self.ce_symbol = nifty_option_symbol(self.strike, "CE", expiry)
        self.pe_symbol = nifty_option_symbol(self.strike, "PE", expiry)
        self.quote_calls = 0

    def set_premiums(self, ce: float, pe: float) -> None:
        self.ce = ce
        self.pe = pe

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self.quote_calls += 1
        quotes: dict[str, Quote] = {}
        for symbol in symbols:
            if symbol == NIFTY_SPOT_SYMBOL:
                quotes[symbol] = Quote(symbol, self.spot)
            elif symbol == self.ce_symbol:
                quotes[symbol] = Quote(symbol, self.ce)
            elif symbol == self.pe_symbol:
                quotes[symbol] = Quote(symbol, self.pe)
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
    assert snap["ce_symbol"] == "NSE:NIFTY26N0326000CE"
    assert snap["pe_symbol"] == "NSE:NIFTY26N0326000PE"
    assert snap["combined_premium"] == 21.4
    assert snap["in_entry_zone"] is True
    assert snap["recommended_lots"] == 2
    assert snap["tentative"]["strike"] == 26000


def test_entry_in_zone_emits_straddle(clock) -> None:
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
    assert meta["sl_price"] == 10.0

    snap = strategy.snapshot()
    assert snap["status"] == "position_open"
    assert snap["position"]["open"] is True
    assert snap["position"]["lots"] == 2


def test_no_entry_outside_zone(clock) -> None:
    clock(13, 5)
    strategy = _strategy(_OptionProvider(ce=25.0, pe=20.0))  # combined 45

    signals = strategy.scan({}, _settings())

    assert signals == []
    snap = strategy.snapshot()
    assert snap["status"] == "waiting_zone"
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


def test_partial_profit_books_half_and_keeps_position(clock) -> None:
    clock(13, 5)
    provider = _OptionProvider()
    strategy = _strategy(provider)
    settings = _settings()
    strategy.scan({}, settings)  # entry at combined 21.4

    clock(13, 10)
    provider.set_premiums(25.0, 20.0)  # combined 45 >= 2 x 21.4
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
