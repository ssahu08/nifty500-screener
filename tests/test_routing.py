"""Tests for session-aware provider routing (FYERS live, off-hours fallback)."""

from __future__ import annotations

import pandas as pd
import pytest

from app.config import Settings
from app.data import build_market_provider
from app.data.fyers import FyersProvider
from app.data.provider import MarketDataProvider, Quote
from app.data.routing import RoutingProvider
from app.data.yfinance_provider import YFinanceProvider


class _FakeProvider(MarketDataProvider):
    QUOTE_BATCH_SIZE = 50

    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls: list[str] = []

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self.calls.append("quotes")
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        return {s: Quote(symbol=s, ltp=1.0) for s in symbols}

    def get_candles(self, symbol, interval, start, end) -> pd.DataFrame:
        self.calls.append("candles")
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        return pd.DataFrame({c: [1.0] for c in ("open", "high", "low", "close", "volume")})

    def health_check(self) -> bool:
        return not self.fail


def test_market_open_uses_primary() -> None:
    primary, offhours = _FakeProvider("fyers"), _FakeProvider("yf")
    router = RoutingProvider(primary, offhours, market_open=lambda: True)

    router.get_candles("NSE:SBIN-EQ", "10m", None, None)
    router.get_quotes(["NSE:SBIN-EQ"])

    assert primary.calls == ["candles", "quotes"]
    assert offhours.calls == []


def test_market_closed_uses_offhours() -> None:
    primary, offhours = _FakeProvider("fyers"), _FakeProvider("yf")
    router = RoutingProvider(primary, offhours, market_open=lambda: False)

    router.get_candles("NSE:SBIN-EQ", "10m", None, None)
    router.get_quotes(["NSE:SBIN-EQ"])

    assert offhours.calls == ["candles", "quotes"]
    assert primary.calls == []


def test_primary_failure_falls_back_to_offhours() -> None:
    primary = _FakeProvider("fyers", fail=True)
    offhours = _FakeProvider("yf")
    router = RoutingProvider(primary, offhours, market_open=lambda: True)

    df = router.get_candles("NSE:SBIN-EQ", "10m", None, None)
    quotes = router.get_quotes(["NSE:SBIN-EQ"])

    assert not df.empty
    assert quotes["NSE:SBIN-EQ"].ltp == 1.0
    assert primary.calls == ["candles", "quotes"]
    assert offhours.calls == ["candles", "quotes"]


def test_offhours_failure_raises_when_closed() -> None:
    primary = _FakeProvider("fyers")
    offhours = _FakeProvider("yf", fail=True)
    router = RoutingProvider(primary, offhours, market_open=lambda: False)

    with pytest.raises(RuntimeError, match="yf down"):
        router.get_candles("NSE:SBIN-EQ", "10m", None, None)
    assert primary.calls == []
    assert offhours.calls == ["candles"]


def test_without_offhours_always_uses_primary() -> None:
    primary = _FakeProvider("fyers")
    router = RoutingProvider(primary, None, market_open=lambda: False)
    router.get_candles("NSE:SBIN-EQ", "10m", None, None)
    assert primary.calls == ["candles"]


def test_quote_batch_size_is_the_smaller_of_the_two() -> None:
    primary, offhours = _FakeProvider("fyers"), _FakeProvider("yf")
    primary.QUOTE_BATCH_SIZE = 200
    offhours.QUOTE_BATCH_SIZE = 25
    assert RoutingProvider(primary, offhours, market_open=lambda: True).QUOTE_BATCH_SIZE == 25


def test_health_check_falls_through_to_the_other_source() -> None:
    primary = _FakeProvider("fyers", fail=True)
    offhours = _FakeProvider("yf")
    router = RoutingProvider(primary, offhours, market_open=lambda: True)
    assert router.health_check() is True

    both_down = RoutingProvider(
        _FakeProvider("fyers", fail=True),
        _FakeProvider("yf", fail=True),
        market_open=lambda: False,
    )
    assert both_down.health_check() is False


def test_build_market_provider_wires_routing(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "x.db"),
        market_provider="fyers",
        offhours_market_provider="yfinance",
    )
    provider = build_market_provider(settings)
    assert isinstance(provider, RoutingProvider)
    assert isinstance(provider.primary, FyersProvider)
    assert isinstance(provider.offhours, YFinanceProvider)


def test_build_market_provider_without_offhours(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "x.db"),
        market_provider="fyers",
        offhours_market_provider="",
    )
    assert isinstance(build_market_provider(settings), FyersProvider)


def test_build_market_provider_offhours_only(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "x.db"),
        market_provider="",
        offhours_market_provider="yfinance",
    )
    assert isinstance(build_market_provider(settings), YFinanceProvider)


def test_build_market_provider_without_any_source(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "x.db"),
        market_provider="",
        offhours_market_provider="",
    )
    with pytest.raises(RuntimeError):
        build_market_provider(settings)
