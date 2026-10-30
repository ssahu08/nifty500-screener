"""Tests for the global index provider (yfinance mocked)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.data.global_index import INDEX_TICKERS, GlobalIndexProvider


class _FakeTicker:
    def __init__(self, data) -> None:
        self._data = data

    def history(self, start, end, timeout=None):  # noqa: ARG002 - signature match
        return self._data


class _FakeYFinance:
    def __init__(self, data) -> None:
        self._data = data
        self.requested: list[str] = []

    def Ticker(self, symbol):
        self.requested.append(symbol)
        return _FakeTicker(self._data.get(symbol, pd.DataFrame()))


def _closes(days: list[date], values: list[float]) -> pd.DataFrame:
    idx = pd.to_datetime([pd.Timestamp(d) for d in days])
    return pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values, "volume": [1] * len(days)},
        index=idx,
    )


def test_index_tickers_present() -> None:
    expected = {"Nifty", "Nasdaq", "DOW", "S&P", "NYSE", "SSE", "JPX", "SZSE", "KRX", "Euronext"}
    assert expected == set(INDEX_TICKERS)


def test_get_close_returns_value_for_exact_day() -> None:
    days = [date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)]
    fake = _FakeYFinance({"^IXIC": _closes(days, [10.0, 20.0, 30.0])})
    provider = GlobalIndexProvider()
    assert provider.get_close("Nasdaq", date(2026, 1, 3), yfinance=fake) == 20.0


def test_get_close_none_when_no_session_that_day() -> None:
    days = [date(2026, 1, 2)]
    fake = _FakeYFinance({"^IXIC": _closes(days, [10.0])})
    provider = GlobalIndexProvider()
    # No bar for 2026-01-03 (market closed) -> None, not a forward fill.
    assert provider.get_close("Nasdaq", date(2026, 1, 3), yfinance=fake) is None


def test_get_close_none_when_no_data() -> None:
    _FakeYFinance({})
    provider = GlobalIndexProvider()
    assert provider.get_close("Nasdaq", date(2026, 1, 3)) is None


def test_get_overview_marks_closed_days_as_none() -> None:
    days = [date(2026, 1, 2), date(2026, 1, 3)]
    data = {"^NSEI": _closes(days, [100.0, 200.0]), "^IXIC": _closes([date(2026, 1, 2)], [10.0])}
    fake = _FakeYFinance(data)
    provider = GlobalIndexProvider()
    overview = provider.get_overview([date(2026, 1, 2), date(2026, 1, 3)], yfinance=fake)

    assert overview["Nifty"][date(2026, 1, 2)] == 100.0
    assert overview["Nifty"][date(2026, 1, 3)] == 200.0
    # Nasdaq has no bar for 2026-01-03 -> None (closed / missing).
    assert overview["Nasdaq"][date(2026, 1, 3)] is None


def test_get_overview_empty_days_returns_empty_series() -> None:
    provider = GlobalIndexProvider()
    overview = provider.get_overview([])
    assert overview["Nifty"] == {}


def test_get_overview_normalises_nan_to_none() -> None:
    # yfinance reports some missing sessions as NaN, which is invalid JSON.
    days = [date(2026, 1, 2), date(2026, 1, 3)]
    data = {"000001.SS": _closes(days, [100.0, float("nan")])}
    fake = _FakeYFinance(data)
    provider = GlobalIndexProvider()
    overview = provider.get_overview(days, yfinance=fake)
    assert overview["SSE"][date(2026, 1, 2)] == 100.0
    assert overview["SSE"][date(2026, 1, 3)] is None
