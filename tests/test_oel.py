"""Tests for the Open-Equals-Low strategy."""

from __future__ import annotations

import pandas as pd

from app.config import Settings
from app.data.provider import Quote
from app.strategies.base import Category
from app.strategies.oel import OpenEqualsLowStrategy


def _settings() -> Settings:
    return Settings(_env_file=None, database_path="data/test.db")


def _candle(open=100.0, high=105.0, low=100.0, close=103.0):
    return pd.Series({"open": open, "high": high, "low": low, "close": close})


def _daily_frame(n=30, open_eq_low=True, end=None):
    """Daily frame with all candles having open == low (or not)."""
    if end is None:
        idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="Asia/Kolkata")
    else:
        idx = pd.date_range(end=end, periods=n, freq="D", tz="Asia/Kolkata")
    rows = []
    for _ in range(n):
        if open_eq_low:
            rows.append(
                {
                    "open": 100.0,
                    "high": 105.0,
                    "low": 100.0,
                    "close": 103.0,
                    "volume": 100.0,
                }
            )
        else:
            rows.append(
                {
                    "open": 100.0,
                    "high": 105.0,
                    "low": 98.0,
                    "close": 103.0,
                    "volume": 100.0,
                }
            )
    return pd.DataFrame(rows, index=idx)


def _intraday_frame(n=30, open_eq_low=True):
    """10-minute intraday frame for a single day (all open == low or not)."""
    idx = pd.date_range(
        "2026-01-15 09:15", periods=n, freq="10min", tz="Asia/Kolkata"
    )
    rows = []
    for _ in range(n):
        if open_eq_low:
            rows.append(
                {
                    "open": 100.0,
                    "high": 105.0,
                    "low": 100.0,
                    "close": 103.0,
                    "volume": 100.0,
                }
            )
        else:
            rows.append(
                {
                    "open": 100.0,
                    "high": 105.0,
                    "low": 98.0,
                    "close": 103.0,
                    "volume": 100.0,
                }
            )
    return pd.DataFrame(rows, index=idx)


def _multi_day_frame(volumes, bars_per_day=6, start="2026-01-01"):
    """One 09:15..10:05 block per session; the first bar gets each volume."""
    frames = []
    for i, vol in enumerate(volumes):
        day = pd.Timestamp(start) + pd.Timedelta(days=i)
        idx = pd.date_range(
            day + pd.Timedelta(hours=9, minutes=15),
            periods=bars_per_day,
            freq="10min",
            tz="Asia/Kolkata",
        )
        rows = [
            {
                "open": 100.0,
                "high": 105.0,
                "low": 100.0,
                "close": 103.0,
                "volume": float(vol) if j == 0 else 100.0,
            }
            for j in range(bars_per_day)
        ]
        frames.append(pd.DataFrame(rows, index=idx))
    return pd.concat(frames)


NOW_INTRADAY = pd.Timestamp("2026-01-15 11:00", tz="Asia/Kolkata")
NOW_DAILY = pd.Timestamp("2026-03-05 16:00", tz="Asia/Kolkata")


def test_metadata() -> None:
    s = OpenEqualsLowStrategy()
    m = s.metadata
    assert m.name == "Open-Equals-Low"
    assert m.category == Category.INTRADAY
    assert m.schedule_type == "auto"
    assert m.timeframes == ["10m", "daily"]


def test_is_open_equals_low_true() -> None:
    assert OpenEqualsLowStrategy._is_open_equals_low(_candle()) is True


def test_is_open_equals_low_false() -> None:
    candle = _candle(low=98.0)
    assert OpenEqualsLowStrategy._is_open_equals_low(candle) is False


def test_scan_detects_oel_on_10m() -> None:
    s = OpenEqualsLowStrategy()
    daily = _daily_frame()
    data = {"SBIN": {"10m": _intraday_frame(), "daily": daily}}
    signals = s.scan(data, _settings(), now=NOW_INTRADAY)
    assert any(sig.metadata["timeframe"] == "10m" for sig in signals)


def test_scan_no_signal_when_low_below_open() -> None:
    s = OpenEqualsLowStrategy()
    data = {
        "SBIN": {
            "10m": _intraday_frame(open_eq_low=False),
            "daily": _daily_frame(open_eq_low=False),
        }
    }
    signals = s.scan(data, _settings(), now=NOW_INTRADAY)
    assert signals == []


def test_scan_empty_frames() -> None:
    s = OpenEqualsLowStrategy()
    data = {"SBIN": {"10m": pd.DataFrame(), "daily": pd.DataFrame()}}
    assert s.scan(data, _settings()) == []


def test_signal_timestamp_is_first_candle_of_day() -> None:
    s = OpenEqualsLowStrategy()
    frame = _intraday_frame(30, open_eq_low=True)
    signals = s.scan(
        {"SBIN": {"10m": frame, "daily": _daily_frame()}}, _settings(), now=NOW_INTRADAY
    )
    ten_min = [sig for sig in signals if sig.metadata["timeframe"] == "10m"]
    assert ten_min
    expected = frame.index[0].to_pydatetime()
    assert all(sig.timestamp == expected for sig in ten_min)


def test_scan_ignores_in_progress_10m_bar() -> None:
    s = OpenEqualsLowStrategy()
    now = pd.Timestamp("2026-01-15 11:00", tz="Asia/Kolkata")
    idx = pd.date_range("2026-01-15 09:15", periods=12, freq="10min", tz="Asia/Kolkata")
    idx = idx.append(pd.DatetimeIndex([pd.Timestamp("2026-01-15 11:00", tz="Asia/Kolkata")]))
    n = len(idx)
    frame = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [105.0] * n,
            "low": [100.0] * n,
            "close": [103.0] * n,
            "volume": [100.0] * n,
        },
        index=idx,
    )
    signals = s.scan(
        {"SBIN": {"10m": frame, "daily": _daily_frame()}}, _settings(), now=now
    )
    stamps = {sig.timestamp for sig in signals if sig.metadata["timeframe"] == "10m"}
    assert stamps == {pd.Timestamp("2026-01-15 09:15", tz="Asia/Kolkata").to_pydatetime()}


def test_passes_filters_records_metadata() -> None:
    s = OpenEqualsLowStrategy()
    daily = _daily_frame()
    data = {"SBIN": {"10m": _intraday_frame(), "daily": daily}}
    signals = s.scan(data, _settings(), now=NOW_INTRADAY)
    assert all("passes_filters" in sig.metadata for sig in signals)


def test_scan_is_idempotent_for_10m() -> None:
    """A still-valid candle is re-reported; storage dedupes the alert."""
    s = OpenEqualsLowStrategy()
    daily = _daily_frame()
    data = {"SBIN": {"10m": _intraday_frame(), "daily": daily}}
    signals1 = s.scan(data, _settings(), now=NOW_INTRADAY)
    signals2 = s.scan(data, _settings(), now=NOW_INTRADAY)
    ten_min_1 = [sig for sig in signals1 if sig.metadata["timeframe"] == "10m"]
    ten_min_2 = [sig for sig in signals2 if sig.metadata["timeframe"] == "10m"]
    assert len(ten_min_1) == 1
    assert len(ten_min_2) == 1
    assert ten_min_1[0].timestamp == ten_min_2[0].timestamp


def test_scan_is_idempotent_for_daily_eod() -> None:
    """The completed daily candle is re-reported on every scan."""
    s = OpenEqualsLowStrategy()
    daily = _daily_frame()
    data = {"SBIN": {"10m": _intraday_frame(), "daily": daily}}
    signals1 = s.scan(data, _settings(), now=NOW_DAILY)
    signals2 = s.scan(data, _settings(), now=NOW_DAILY)
    daily_1 = [sig for sig in signals1 if sig.metadata["timeframe"] == "daily"]
    daily_2 = [sig for sig in signals2 if sig.metadata["timeframe"] == "daily"]
    assert len(daily_1) == 1
    assert len(daily_2) == 1
    assert daily_1[0].timestamp == daily_2[0].timestamp


# ------------------------------------------------------------------ new tests
def test_intraday_rejects_signal_when_price_later_dips_below_open() -> None:
    """The criterion is the DAY's open == the DAY's low, not just the first bar.

    AARTIIND 30/10: the first candle opened at its low, then the stock traded
    below that open for the rest of the day - it must NOT be flagged.
    """
    s = OpenEqualsLowStrategy()
    idx = pd.date_range("2026-01-15 09:15", periods=8, freq="10min", tz="Asia/Kolkata")
    rows = [
        {"open": 100.0, "high": 105.0, "low": 100.0, "close": 103.0, "volume": 100.0},
        {"open": 103.0, "high": 104.0, "low": 99.0, "close": 100.0, "volume": 100.0},
        {"open": 100.0, "high": 102.0, "low": 99.5, "close": 101.0, "volume": 100.0},
        {"open": 101.0, "high": 103.0, "low": 100.5, "close": 102.0, "volume": 100.0},
        {"open": 102.0, "high": 104.0, "low": 101.0, "close": 103.0, "volume": 100.0},
        {"open": 103.0, "high": 105.0, "low": 102.0, "close": 104.0, "volume": 100.0},
        {"open": 104.0, "high": 106.0, "low": 103.0, "close": 105.0, "volume": 100.0},
        {"open": 105.0, "high": 107.0, "low": 104.0, "close": 106.0, "volume": 100.0},
    ]
    frame = pd.DataFrame(rows, index=idx)
    signals = s.scan(
        {"SBIN": {"10m": frame, "daily": _daily_frame()}}, _settings(), now=NOW_INTRADAY
    )
    intraday = [sig for sig in signals if sig.metadata["timeframe"] == "10m"]
    assert intraday == []


def test_intraday_uses_full_day_low_and_latest_close() -> None:
    """A genuine OEL day: every later low stays at/above the open."""
    s = OpenEqualsLowStrategy()
    idx = pd.date_range("2026-01-15 09:15", periods=8, freq="10min", tz="Asia/Kolkata")
    rows = [
        {"open": 100.0, "high": 105.0, "low": 100.0, "close": 103.0, "volume": 100.0},
        {"open": 103.0, "high": 106.0, "low": 101.0, "close": 104.0, "volume": 100.0},
        {"open": 104.0, "high": 107.0, "low": 102.0, "close": 105.0, "volume": 100.0},
        {"open": 105.0, "high": 108.0, "low": 103.0, "close": 106.0, "volume": 100.0},
        {"open": 106.0, "high": 109.0, "low": 104.0, "close": 107.0, "volume": 100.0},
        {"open": 107.0, "high": 110.0, "low": 105.0, "close": 108.0, "volume": 100.0},
        {"open": 108.0, "high": 111.0, "low": 106.0, "close": 109.0, "volume": 100.0},
        {"open": 109.0, "high": 112.0, "low": 107.0, "close": 110.0, "volume": 100.0},
    ]
    frame = pd.DataFrame(rows, index=idx)
    signals = s.scan(
        {"SBIN": {"10m": frame, "daily": _daily_frame()}}, _settings(), now=NOW_INTRADAY
    )
    intraday = [sig for sig in signals if sig.metadata["timeframe"] == "10m"]
    assert len(intraday) == 1
    sig = intraday[0]
    assert sig.metadata["day_open"] == 100.0
    assert sig.metadata["day_low"] == 100.0
    assert sig.price == 110.0  # latest completed close, not first candle's 103
    assert sig.metadata["move_pct"] == 10.0
    assert sig.metadata["bars_elapsed"] == 8


def test_intraday_waits_for_one_hour_of_bars() -> None:
    """At 09:55 (4 bars) the low is still trivially the open - wait until 10:15."""
    s = OpenEqualsLowStrategy()
    data = {"SBIN": {"10m": _intraday_frame(30), "daily": _daily_frame()}}
    early = s.scan(
        data, _settings(), now=pd.Timestamp("2026-01-15 09:55", tz="Asia/Kolkata")
    )
    assert [x for x in early if x.metadata["timeframe"] == "10m"] == []
    ready = s.scan(
        data, _settings(), now=pd.Timestamp("2026-01-15 10:15", tz="Asia/Kolkata")
    )
    assert [x for x in ready if x.metadata["timeframe"] == "10m"]


def test_after_continuous_close_only_daily_variant_fires() -> None:
    """No duplicate 10m signal after 15:15 - the EOD daily candle takes over."""
    s = OpenEqualsLowStrategy()
    now = pd.Timestamp("2026-01-15 15:20", tz="Asia/Kolkata")
    signals = s.scan(
        {"SBIN": {"10m": _intraday_frame(30), "daily": _daily_frame()}},
        _settings(),
        now=now,
    )
    tfs = {sig.metadata["timeframe"] for sig in signals}
    assert "10m" not in tfs
    assert "daily" in tfs


def test_intraday_skips_when_true_first_candle_missing() -> None:
    """If the 09:15 bar is absent (provider gap), do not substitute another bar."""
    s = OpenEqualsLowStrategy()
    # Frame starts at 09:25, no 09:15 bar.
    idx = pd.date_range("2026-01-15 09:25", periods=6, freq="10min", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 6,
            "high": [105.0] * 6,
            "low": [100.0] * 6,
            "close": [103.0] * 6,
            "volume": [100.0] * 6,
        },
        index=idx,
    )
    signals = s.scan(
        {"SBIN": {"10m": frame, "daily": _daily_frame()}}, _settings(), now=NOW_INTRADAY
    )
    intraday = [sig for sig in signals if sig.metadata["timeframe"] == "10m"]
    assert intraday == []


def test_intraday_no_signal_before_first_candle_closes() -> None:
    s = OpenEqualsLowStrategy()
    now = pd.Timestamp("2026-01-15 09:20", tz="Asia/Kolkata")
    signals = s.scan(
        {"SBIN": {"10m": _intraday_frame(), "daily": _daily_frame()}},
        _settings(),
        now=now,
    )
    intraday = [sig for sig in signals if sig.metadata["timeframe"] == "10m"]
    assert intraday == []


def test_intraday_ignores_yesterday_data() -> None:
    s = OpenEqualsLowStrategy()
    now = pd.Timestamp("2026-01-16 11:00", tz="Asia/Kolkata")
    signals = s.scan(
        {"SBIN": {"10m": _intraday_frame(), "daily": _daily_frame()}},
        _settings(),
        now=now,
    )
    intraday = [sig for sig in signals if sig.metadata["timeframe"] == "10m"]
    assert intraday == []


def test_no_weekly_or_monthly_variants() -> None:
    """Only day-level OEL is emitted (live 10m + EOD daily)."""
    s = OpenEqualsLowStrategy()
    daily = _daily_frame(60)
    data = {"SBIN": {"10m": _intraday_frame(), "daily": daily}}
    signals = s.scan(data, _settings(), now=NOW_DAILY)
    tfs = {sig.metadata["timeframe"] for sig in signals}
    assert tfs <= {"10m", "daily"}
    assert "daily" in tfs


def test_daily_signals_each_completed_candle_once() -> None:
    s = OpenEqualsLowStrategy()
    # Monday..Friday then the following Monday.
    idx = pd.date_range("2026-01-05", periods=5, freq="D", tz="Asia/Kolkata").append(
        pd.DatetimeIndex([pd.Timestamp("2026-01-12", tz="Asia/Kolkata")])
    )
    rows = [
        {"open": 100.0, "high": 105.0, "low": 100.0, "close": 103.0, "volume": 100.0}
        for _ in range(6)
    ]
    daily = pd.DataFrame(rows, index=idx)
    data = {"SBIN": {"10m": _intraday_frame(), "daily": daily}}
    now1 = pd.Timestamp("2026-01-09 14:00", tz="Asia/Kolkata")  # last completed = Jan 9 (Fri)
    now2 = pd.Timestamp("2026-01-12 16:00", tz="Asia/Kolkata")  # last completed = Jan 12 (Mon)
    sigs1 = s.scan(data, _settings(), now=now1)
    sigs2 = s.scan(data, _settings(), now=now2)
    daily1 = [x for x in sigs1 if x.metadata["timeframe"] == "daily"]
    daily2 = [x for x in sigs2 if x.metadata["timeframe"] == "daily"]
    assert len(daily1) == 1
    assert len(daily2) == 1
    assert daily1[0].timestamp != daily2[0].timestamp


# --------------------------------------------------------------- quality rank
def test_intraday_ranks_high_opening_volume_first() -> None:
    """Heavy 09:15 volume ranks top; thin opening volume ranks bottom."""
    s = OpenEqualsLowStrategy()
    now = pd.Timestamp("2026-01-11 10:15", tz="Asia/Kolkata")
    daily = _daily_frame(end="2026-01-11")
    data = {
        "HEAVY": {
            "10m": _multi_day_frame([100.0] * 10 + [300.0]),
            "daily": daily,
        },
        "THIN": {
            "10m": _multi_day_frame([100.0] * 10 + [50.0]),
            "daily": daily,
        },
    }
    signals = s.scan(data, _settings(), now=now)
    intraday = [sig for sig in signals if sig.metadata["timeframe"] == "10m"]
    assert [sig.symbol for sig in intraday] == ["HEAVY", "THIN"]

    heavy, thin = intraday
    assert heavy.metadata["open_vol_ratio"] == 3.0
    assert heavy.metadata["open_vol_pctile"] == 100.0
    assert heavy.metadata["open_vol_history_days"] == 10
    assert heavy.metadata["quality"] == "HIGH"
    assert thin.metadata["open_vol_ratio"] == 0.5
    assert thin.metadata["quality"] == "LOW"
    assert heavy.metadata["score"] > thin.metadata["score"]


def test_daily_quality_uses_first_10m_volume() -> None:
    """The EOD variant also ranks on the day's 09:15 10-minute volume."""
    s = OpenEqualsLowStrategy()
    now = pd.Timestamp("2026-01-11 16:00", tz="Asia/Kolkata")
    data = {
        "SBIN": {
            "10m": _multi_day_frame([100.0] * 10 + [300.0]),
            "daily": _daily_frame(end="2026-01-11"),
        }
    }
    signals = s.scan(data, _settings(), now=now)
    daily = [x for x in signals if x.metadata["timeframe"] == "daily"]
    assert len(daily) == 1
    meta = daily[0].metadata
    assert meta["volume_basis"] == "open_10m"
    assert meta["open_vol_ratio"] == 3.0
    assert meta["open_vol_pctile"] == 100.0
    assert meta["quality"] == "HIGH"
    assert meta["daily_vol_ratio"] == 1.0


def test_daily_quality_falls_back_to_daily_volume_without_10m() -> None:
    """Daily-only data still gets a score, using the daily volume ratio."""
    s = OpenEqualsLowStrategy()
    now = pd.Timestamp("2026-01-30 16:00", tz="Asia/Kolkata")
    signals = s.scan({"SBIN": {"daily": _daily_frame()}}, _settings(), now=now)
    daily = [x for x in signals if x.metadata["timeframe"] == "daily"]
    assert len(daily) == 1
    meta = daily[0].metadata
    assert meta["volume_basis"] == "daily"
    assert meta["open_vol_ratio"] is None
    assert meta["daily_vol_ratio"] == 1.0
    assert meta["score"] > 0


# -- quote prefilter ---------------------------------------------------------
def test_prefilter_keeps_only_open_equals_low(monkeypatch) -> None:
    """Only OEL candidates get candle history; missing quotes are kept."""
    s = OpenEqualsLowStrategy()
    monkeypatch.setattr(
        s, "get_universe", lambda settings: ["AAA", "BBB", "CCC", "DDD"]
    )
    quotes = {
        "NSE:AAA-EQ": Quote(
            "NSE:AAA-EQ", ltp=101.0, open=100.0, high=105.0, low=100.0
        ),
        "NSE:BBB-EQ": Quote(
            "NSE:BBB-EQ", ltp=101.0, open=100.0, high=105.0, low=99.5
        ),
        # CCC has no quote -> kept so a data hiccup cannot hide a signal.
        "NSE:DDD-EQ": Quote(
            "NSE:DDD-EQ", ltp=101.0, open=100.0, high=105.0, low=None
        ),
    }
    assert s.prefilter(quotes, _settings()) == ["AAA", "CCC", "DDD"]


def test_prefilter_returns_empty_when_no_open_equals_low(monkeypatch) -> None:
    s = OpenEqualsLowStrategy()
    monkeypatch.setattr(s, "get_universe", lambda settings: ["AAA", "BBB"])
    quotes = {
        "NSE:AAA-EQ": Quote(
            "NSE:AAA-EQ", ltp=101.0, open=100.0, high=105.0, low=99.0
        ),
        "NSE:BBB-EQ": Quote(
            "NSE:BBB-EQ", ltp=101.0, open=100.0, high=105.0, low=98.0
        ),
    }
    assert s.prefilter(quotes, _settings()) == []
