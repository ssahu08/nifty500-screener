"""File-backed candle cache with warm-cache / append semantics.

Stores candle DataFrames on disk keyed by ``symbol|interval`` together with the
date range they cover. A read that is fully covered by the cached range is a hit;
otherwise the caller re-fetches and :meth:`CandleCache.put` merges the new rows
with the cached ones (deduplicating by timestamp and re-sorting).

This realises the BRD rule "keep recent candles cached locally and append new
ones rather than refetching history".
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pandas as pd

from app.data.provider import OHLCV_COLUMNS


class CandleCache:
    """Persist and retrieve candle frames on disk."""

    def __init__(self, root: str | Path, namespace: str = "candles") -> None:
        self.root = Path(root)
        self.namespace = namespace
        self.root.mkdir(parents=True, exist_ok=True)
        #: Serialise writes: concurrent strategy runs may fetch the same
        #: symbol at the same time and must not interleave cache updates.
        self._write_lock = threading.Lock()

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace("|", "_").replace(":", "_")
        return self.root / self.namespace / f"{safe}.json"

    def get(self, key: str, start, end) -> pd.DataFrame | None:
        """Return cached candles covering ``[start, end]``, or ``None``.

        ``start``/``end`` are :class:`datetime.date`. The returned frame is
        trimmed to the requested range.
        """
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            df = pd.DataFrame(payload["data"])
            if df.empty:
                return None
            df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert("Asia/Kolkata")
            df = df.set_index("time").sort_index()
        except (ValueError, KeyError, TypeError, OSError):
            return None

        if df.empty:
            return None

        # Cover check: the cached range must span the requested window.
        first = df.index[0].date() if hasattr(df.index[0], "date") else df.index[0]
        last = df.index[-1].date() if hasattr(df.index[-1], "date") else df.index[-1]
        if first > start or last < end:
            return None

        # Trim to the requested window with a boolean mask (string slicing a
        # tz-aware DatetimeIndex is unreliable across pandas versions).
        subset = df[(df.index.date >= start) & (df.index.date <= end)]
        return subset[OHLCV_COLUMNS]

    def put(self, key: str, df: pd.DataFrame) -> None:
        """Merge ``df`` into the cache for ``key`` (append/overwrite duplicates).

        Writes are serialised and atomic (temp file + ``os.replace``) so a
        concurrent strategy run can never read a half-written cache file.
        """
        if df is None or df.empty:
            return
        with self._write_lock:
            self._put_locked(key, df)

    def _put_locked(self, key: str, df: pd.DataFrame) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        incoming = df.copy()
        if not isinstance(incoming.index, pd.DatetimeIndex):
            incoming.index = pd.to_datetime(incoming.index, utc=True)
        incoming = incoming.tz_convert("Asia/Kolkata") if incoming.index.tz else incoming
        has_ohlcv = all(c in incoming.columns for c in OHLCV_COLUMNS)
        incoming = incoming[OHLCV_COLUMNS] if has_ohlcv else incoming

        existing = None
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                existing = pd.DataFrame(payload["data"])
                existing["time"] = pd.to_datetime(existing["time"], utc=True).dt.tz_convert(
                    "Asia/Kolkata"
                )
                existing = existing.set_index("time").sort_index()
            except (ValueError, KeyError, TypeError, OSError):
                existing = None

        if existing is not None and not existing.empty:
            merged = pd.concat([existing, incoming[OHLCV_COLUMNS]])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        else:
            merged = incoming[OHLCV_COLUMNS].sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]

        records = merged.reset_index()
        records["time"] = merged.index.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload = json.dumps(
            {"key": key, "data": records.to_dict(orient="records")}, default=str
        )
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def invalidate(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def prune(self, max_age_seconds: int = 7 * 86400) -> int:
        """Delete cache files not modified within ``max_age_seconds``.

        Returns the number of files removed.
        """
        cutoff = pd.Timestamp.now().timestamp() - max_age_seconds
        removed = 0
        cache_dir = self.root / self.namespace
        if not cache_dir.exists():
            return 0
        for file in cache_dir.glob("*.json"):
            if file.stat().st_mtime < cutoff:
                file.unlink()
                removed += 1
        return removed
