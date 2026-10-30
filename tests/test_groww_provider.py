"""Tests for the Groww off-hours provider (hermetic: fake requests session)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.data.groww_provider import GrowwProvider, to_groww_params
from app.data.provider import OHLCV_COLUMNS

IST = ZoneInfo("Asia/Kolkata")


def ist(ts: str) -> float:
    return datetime.fromisoformat(ts).replace(tzinfo=IST).timestamp()


class _FakeResponse:
    def __init__(self, payload, status_code=200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses=None, error=None) -> None:
        self.responses = responses or {}
        self.error = error
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.responses.get(params.get("trading_symbol"), {}))


def _candles(rows):
    return {"candles": [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows]}


def _ist_candle_rows(base: str, count: int = 2, gap_minutes: int = 10):
    """``count`` session-aligned candles starting at ``base`` (IST)."""
    t0 = datetime.fromisoformat(base).replace(tzinfo=IST)
    rows = []
    for i in range(count):
        stamp = int((t0 + timedelta(minutes=i * gap_minutes)).timestamp())
        rows.append([stamp, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000 * (i + 1)])
    return rows


def test_symbol_params() -> None:
    assert to_groww_params("NSE:SBIN-EQ") == ("NSE", "CASH", "SBIN")
    assert to_groww_params("SBIN") == ("NSE", "CASH", "SBIN")
    assert to_groww_params("NSE:NIFTY 50-EQ") == ("NSE", "CASH", "NIFTY")


def test_10m_candles_converted_to_ist() -> None:
    rows = _ist_candle_rows("2026-10-16 09:15:00", 3)
    session = _FakeSession(responses={"SBIN": _candles(rows)})
    provider = GrowwProvider(api_key="test-key", session=session)

    df = provider.get_candles("NSE:SBIN-EQ", "10m", date(2026, 10, 16), date(2026, 10, 16))

    assert list(df.columns) == OHLCV_COLUMNS
    assert len(df) == 3
    assert str(df.index[0].tz) == "Asia/Kolkata"
    assert df.index[0].strftime("%H:%M") == "09:15"
    assert df.index[1].strftime("%H:%M") == "09:25"
    assert df.iloc[0]["open"] == 100.0
    assert df.iloc[0]["high"] == 101.0
    assert df.iloc[0]["low"] == 99.0
    assert df.iloc[0]["close"] == 100.5
    assert df.iloc[0]["volume"] == 1000.0


def test_request_params_and_auth() -> None:
    rows = _ist_candle_rows("2026-10-16 09:15:00", 1)
    session = _FakeSession(responses={"RELIANCE": _candles(rows)})
    provider = GrowwProvider(api_key="secret-token", session=session)
    provider.get_candles("NSE:RELIANCE-EQ", "10m", date(2026, 10, 16), date(2026, 10, 16))

    call = session.calls[0]
    assert call["url"].endswith("/v1/historical/candle/range")
    assert call["params"]["exchange"] == "NSE"
    assert call["params"]["segment"] == "CASH"
    assert call["params"]["trading_symbol"] == "RELIANCE"
    assert call["params"]["interval_in_minutes"] == 10
    assert call["headers"]["Authorization"] == "Bearer secret-token"
    assert call["headers"]["X-API-VERSION"] == "1.0"


def test_daily_candles_localized_to_ist_midnight() -> None:
    t0 = int(datetime(2026, 10, 14, tzinfo=IST).timestamp())
    rows = [[t0 + i * 86400, 10.0 + i, 11.0 + i, 9.0 + i, 10.5 + i, 1000.0] for i in range(3)]
    session = _FakeSession(responses={"TCS": _candles(rows)})
    provider = GrowwProvider(api_key="test-key", session=session)

    df = provider.get_candles("NSE:TCS-EQ", "daily", date(2026, 10, 14), date(2026, 10, 16))

    assert len(df) == 3
    assert str(df.index[0].tz) == "Asia/Kolkata"
    assert (df.index.hour == 0).all()
    assert df.iloc[-1]["close"] == 12.5


def test_unknown_interval_returns_empty_frame() -> None:
    provider = GrowwProvider(api_key="test-key", session=_FakeSession())
    df = provider.get_candles("NSE:SBIN-EQ", "3m", date(2026, 10, 16), date(2026, 10, 16))
    assert df.empty
    assert list(df.columns) == OHLCV_COLUMNS


def test_wide_range_is_split_into_chunks() -> None:
    session = _FakeSession(responses={"SBIN": _candles(_ist_candle_rows("2026-10-16 09:15:00", 2))})
    provider = GrowwProvider(api_key="test-key", session=session)

    # 40 days of 10-minute data exceeds the 30-day max per request.
    provider.get_candles("NSE:SBIN-EQ", "10m", date(2026, 9, 6), date(2026, 10, 15))

    assert len(session.calls) == 2
    starts = [c["params"]["start_time"] for c in session.calls]
    assert starts[0].startswith("2026-09-06")
    assert starts[1].startswith("2026-10-06")


def test_provider_failure_returns_empty_frame() -> None:
    provider = GrowwProvider(api_key="test-key", session=_FakeSession(error=RuntimeError("down")))
    df = provider.get_candles("NSE:SBIN-EQ", "daily", date(2026, 10, 14), date(2026, 10, 16))
    assert df.empty
    assert list(df.columns) == OHLCV_COLUMNS


def test_get_quotes_derived_from_daily_bars() -> None:
    rows = _ist_candle_rows("2026-10-14 00:00:00", 3)
    session = _FakeSession(responses={"SBIN": _candles(rows)})
    provider = GrowwProvider(api_key="test-key", session=session)

    quotes = provider.get_quotes(["NSE:SBIN-EQ"])

    assert set(quotes) == {"NSE:SBIN-EQ"}
    q = quotes["NSE:SBIN-EQ"]
    assert q.ltp == 102.5
    assert q.prev_close == 101.5
    assert q.open == 102.0
    assert q.volume == 3000.0


def test_health_check() -> None:
    rows = _ist_candle_rows("2026-10-14 00:00:00", 2)
    ok = GrowwProvider(
        api_key="test-key", session=_FakeSession(responses={"NIFTY": _candles(rows)})
    )
    assert ok.health_check() is True
    down = GrowwProvider(api_key="test-key", session=_FakeSession(error=RuntimeError("timeout")))
    assert down.health_check() is False
