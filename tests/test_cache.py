"""Tests for the file-backed candle cache."""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.data.cache import CandleCache


def _frame(days: list[date]) -> pd.DataFrame:
    idx = pd.to_datetime([pd.Timestamp(d) for d in days], utc=True).tz_convert("Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": [100.0] * len(days),
            "high": [101.0] * len(days),
            "low": [99.0] * len(days),
            "close": [100.5] * len(days),
            "volume": [1000.0] * len(days),
        },
        index=idx,
    )


def test_cache_miss_when_empty(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    assert cache.get("NSE:SBIN-EQ|1D", date(2026, 1, 1), date(2026, 1, 5)) is None


def test_first_put_deduplicates_timestamps(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    frame = _frame([date(2026, 1, 2), date(2026, 1, 2), date(2026, 1, 3)])
    cache.put("NSE:SBIN-EQ|1D", frame)
    result = cache.get("NSE:SBIN-EQ|1D", date(2026, 1, 2), date(2026, 1, 3))
    assert result is not None
    assert len(result) == 2
    assert not result.index.duplicated().any()


def test_cache_hit_returns_range_subset(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    df = _frame([date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)])
    cache.put("NSE:SBIN-EQ|1D", df)

    cached = cache.get("NSE:SBIN-EQ|1D", date(2026, 1, 1), date(2026, 1, 2))
    assert cached is not None
    assert list(cached.columns) == ["open", "high", "low", "close", "volume"]
    assert len(cached) == 2


def test_cache_miss_when_range_not_covered(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    cache.put("NSE:SBIN-EQ|1D", _frame([date(2026, 1, 1)]))
    # Requested range extends past the cached data.
    assert cache.get("NSE:SBIN-EQ|1D", date(2026, 1, 1), date(2026, 1, 5)) is None


def test_put_merges_and_deduplicates(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    cache.put("NSE:SBIN-EQ|1D", _frame([date(2026, 1, 1), date(2026, 1, 2)]))
    cache.put("NSE:SBIN-EQ|1D", _frame([date(2026, 1, 2), date(2026, 1, 3)]))

    cached = cache.get("NSE:SBIN-EQ|1D", date(2026, 1, 1), date(2026, 1, 3))
    assert cached is not None
    assert len(cached) == 3


def test_invalidate_removes_entry(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    cache.put("NSE:SBIN-EQ|1D", _frame([date(2026, 1, 1)]))
    cache.invalidate("NSE:SBIN-EQ|1D")
    assert cache.get("NSE:SBIN-EQ|1D", date(2026, 1, 1), date(2026, 1, 1)) is None


def test_prune_removes_stale_files(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    cache.put("NSE:SBIN-EQ|1D", _frame([date(2026, 1, 1)]))
    removed = cache.prune(max_age_seconds=-1)
    assert removed == 1
