"""Tests for strategy discovery and registry."""

from __future__ import annotations

import sys
import types

from app.strategies.base import Category, Strategy, StrategyMetadata
from app.strategies.registry import discover_strategies, get_registry


class _DummyStrategy(Strategy):
    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Dummy",
            description="dummy",
            category=Category.INTRADAY,
            universe="NIFTY_50",
            schedule_type="manual",
            timeframes=["daily"],
        )

    def scan(self, data, settings):
        return []


def test_discover_strategies_finds_dummy(monkeypatch) -> None:
    module = types.ModuleType("app.strategies._dummy_test")
    module.strategy_class = _DummyStrategy
    sys.modules["app.strategies._dummy_test"] = module

    class _Info:
        name = "_dummy_test"

    monkeypatch.setattr("app.strategies.registry.pkgutil.iter_modules", lambda path: [_Info()])
    try:
        discovered = discover_strategies()
    finally:
        del sys.modules["app.strategies._dummy_test"]

    assert _DummyStrategy in discovered


def test_get_registry_instantiates(monkeypatch) -> None:
    module = types.ModuleType("app.strategies._dummy_test")
    module.strategy_class = _DummyStrategy
    sys.modules["app.strategies._dummy_test"] = module

    class _Info:
        name = "_dummy_test"

    monkeypatch.setattr("app.strategies.registry.pkgutil.iter_modules", lambda path: [_Info()])
    try:
        registry = get_registry()
    finally:
        del sys.modules["app.strategies._dummy_test"]

    assert "Dummy" in registry
    assert isinstance(registry["Dummy"], _DummyStrategy)
