"""Tests for the HTTP API endpoints (Phase 6)."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.api.routes import INDEX_TICKERS, clear_overview_cache
from app.config import get_settings
from app.main import app


@pytest.fixture()
def client(monkeypatch, tmp_path):
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("ENV", "test")
    monkeypatch.delenv("FYERS_CLIENT_ID", raising=False)
    monkeypatch.delenv("FYERS_SECRET_KEY", raising=False)
    clear_overview_cache()
    with TestClient(app) as test_client:
        # The .env file may carry real FYERS credentials; blank them on the
        # running app so no test can ever reach the live API (the tokens are
        # also loaded from .env, not just the environment).
        app.state.settings.fyers_client_id = ""
        app.state.settings.fyers_secret_key = ""
        yield test_client
    get_settings.cache_clear()


class _FakeGlobalProvider:
    def get_overview(self, trading_days, yfinance=None):
        return {
            label: {d: 100.0 + i for i, d in enumerate(trading_days)}
            for label in INDEX_TICKERS
        }


class _FakeMarketProvider:
    def __init__(self, days):
        self._days = days

    def get_candles(self, symbol, interval, start, end):
        idx = pd.to_datetime([d.isoformat() for d in self._days])
        return pd.DataFrame(
            {
                "open": [200.0] * len(self._days),
                "high": [201.0] * len(self._days),
                "low": [199.0] * len(self._days),
                "close": [222.0 + i for i in range(len(self._days))],
                "volume": [10.0] * len(self._days),
            },
            index=idx,
        )


def test_health(client) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["retention_days"] == 14


def test_dashboard_still_served(client) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "Market Overview" in res.text


def test_list_strategies(client) -> None:
    res = client.get("/api/strategies")
    assert res.status_code == 200
    names = {s["name"] for s in res.json()}
    assert "Multi-Timeframe Doji" in names
    assert "Open-Equals-Low" in names
    assert "EOD Compression Breakout" in names
    for item in res.json():
        assert set(item) >= {
            "name",
            "description",
            "category",
            "universe",
            "schedule_type",
            "run_at",
            "timeframes",
        }
    eod = next(s for s in res.json() if s["name"] == "EOD Compression Breakout")
    assert eod["run_at"] == "15:35"


def test_run_unknown_strategy_404(client) -> None:
    res = client.post("/api/run/NoSuchStrategy")
    assert res.status_code == 404


def test_run_and_results_roundtrip(client) -> None:
    from app.scheduler.jobs import StrategyScheduler
    from tests.test_scheduler import _EchoStrategy, _FakeProvider

    app.state.registry = {"Echo": _EchoStrategy()}
    app.state.scheduler = StrategyScheduler(
        app.state.settings,
        app.state.registry,
        _FakeProvider(),
        app.state.store,
    )

    res = client.post("/api/run/Echo")
    assert res.status_code == 200
    body = res.json()
    assert body["signals"] == 1
    assert body["results"][0]["symbol"] == "NSE:SBIN-EQ"

    res = client.get("/api/results")
    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["strategy"] == "Echo"
    assert rows[0]["category"] == "intraday"

    res = client.get("/api/results/Echo")
    assert res.status_code == 200
    assert len(res.json()["signals"]) == 1

    res = client.get("/api/history/Echo?days=14")
    assert res.status_code == 200
    assert len(res.json()["signals"]) == 1


def test_run_status_idle_and_done(client) -> None:
    from app.scheduler.jobs import StrategyScheduler
    from tests.test_scheduler import _EchoStrategy, _FakeProvider

    app.state.registry = {"Echo": _EchoStrategy()}
    app.state.scheduler = StrategyScheduler(
        app.state.settings,
        app.state.registry,
        _FakeProvider(),
        app.state.store,
    )

    res = client.get("/api/run/Echo/status")
    assert res.status_code == 200
    assert res.json() == {"state": "idle"}

    client.post("/api/run/Echo")
    body = client.get("/api/run/Echo/status").json()
    assert body["state"] == "done"
    assert body["signals"] == 1
    assert body["elapsed_s"] >= 0


def test_run_status_unknown_strategy_404(client) -> None:
    assert client.get("/api/run/NoSuchStrategy/status").status_code == 404


def test_results_category_filter(client) -> None:
    res = client.get("/api/results?category=event-driven")
    assert res.status_code == 200
    assert res.json() == []


def test_history_unknown_strategy_404(client) -> None:
    assert client.get("/api/history/NoSuchStrategy").status_code == 404


def test_overview_structure(client) -> None:
    app.state.global_provider = _FakeGlobalProvider()
    res = client.get("/api/overview")
    assert res.status_code == 200
    body = res.json()
    assert len(body["columns"]) == 5
    assert body["columns"] == sorted(body["columns"], reverse=True)  # newest first
    assert len(body["rows"]) == len(INDEX_TICKERS)
    assert body["errors"] == []
    labels = [r["label"] for r in body["rows"]]
    assert labels == list(INDEX_TICKERS)
    for row in body["rows"]:
        # Fake closes are 100..105 oldest -> newest; showcase = last five reversed.
        assert len(row["values"]) == 5
        assert row["values"] == [105.0, 104.0, 103.0, 102.0, 101.0]
        assert len(row["changes"]) == 5
        assert row["changes"][0] == pytest.approx((105.0 - 104.0) / 104.0 * 100.0)
        assert row["changes"][1] == pytest.approx((104.0 - 103.0) / 103.0 * 100.0)


def test_overview_change_none_when_closed(client) -> None:
    class _ClosedMonday(_FakeGlobalProvider):
        def get_overview(self, trading_days, yfinance=None):
            out = super().get_overview(trading_days, yfinance)
            for label in INDEX_TICKERS:
                out[label][trading_days[2]] = None  # a mid-window closed day
            return out

    app.state.global_provider = _ClosedMonday()
    body = client.get("/api/overview?refresh=true").json()
    for row in body["rows"]:
        values = row["values"]  # newest first: fetched oldest F0..F5 -> [F5..F1]
        assert values[3] is None  # the closed day (F2)
        assert row["changes"][3] is None  # closed day vs previous session
        assert row["changes"][2] is None  # next session vs the closed day
        assert row["changes"][0] is not None and row["changes"][4] is not None


def test_overview_nifty_prefers_fyers(client) -> None:
    from datetime import date as _date

    days = []
    cursor = _date.today()
    while len(days) < 6:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days = list(reversed(days))

    app.state.global_provider = _FakeGlobalProvider()
    app.state.settings.fyers_client_id = "x"
    app.state.settings.fyers_secret_key = "y"
    app.state.market_provider = _FakeMarketProvider(days)

    res = client.get("/api/overview?refresh=true")
    assert res.status_code == 200
    nifty_row = next(r for r in res.json()["rows"] if r["label"] == "Nifty")
    # Fake FYERS closes run 222..227 across the 6 fetched days; the newest of
    # the five shown days is 227 and stays first (newest-first ordering).
    assert nifty_row["values"][0] == 227.0
