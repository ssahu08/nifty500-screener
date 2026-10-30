"""Universe management: which symbols each strategy scans.

Universes are stored as JSON lists in ``data/universes/`` so they can be
refreshed without code changes. A downloader for the Nifty 500 constituent
list from NSE archives is provided for convenience (best-effort; NSE may
require browser-like headers or block automated access).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Well-known universe names.
NIFTY_500 = "NIFTY_500"
NIFTY_50 = "NIFTY_50"
ALL_NSE = "ALL_NSE"

_UNIVERSE_DIR = Path("data/universes")

#: Minimal fallback lists so the framework is usable before a download.
_FALLBACK: dict[str, list[str]] = {
    NIFTY_50: [
        "RELIANCE", "HDFCBANK", "INFY", "ICICIBANK", "TCS", "SBIN", "BAJFINANCE",
        "BHARTIARTL", "ITC", "KOTAKBANK", "LT", "HINDUNILVR", "AXISBANK",
        "MARUTI", "SUNPHARMA", "TITAN", "NESTLEIND", "ULTRACEMCO", "TATASTEEL",
        "WIPRO", "M&M", "BAJAJ-AUTO", "HCLTECH", "ADANIENT", "ONGC", "NTPC",
        "POWERGRID", "COALINDIA", "IOC", "BPCL", "HINDALCO", "GRASIM", "JSWSTEEL",
        "TECHM", "DRREDDY", "CIPLA", "DIVISLAB", "HEROMOTOCO", "APOLLOHOSP",
        "TATAMOTORS", "EICHERMOT", "SHREECEM", "BRITANNIA", "DABUR", "GODREJCP",
        "MARICO", "COLPAL", "PGHH", "BERGEPAINT", "ASIANPAINT",
    ],
    NIFTY_500: [],  # populated by download; empty until then
    ALL_NSE: [],
}


def get_universe(name: str, settings: Settings | None = None) -> list[str]:
    """Return the symbol list for a universe, loading from disk when present."""
    path = _UNIVERSE_DIR / f"{name}.json"
    if path.exists():
        try:
            symbols = json.loads(path.read_text(encoding="utf-8"))
            if symbols:
                return symbols
        except (OSError, json.JSONDecodeError):
            logger.warning("Universe file %s unreadable; using fallback", path)

    fallback = _FALLBACK.get(name)
    if fallback is not None:
        return fallback
    raise KeyError(f"Unknown universe {name!r} and no fallback available")


def save_universe(name: str, symbols: list[str]) -> Path:
    """Persist a universe list to disk."""
    _UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
    path = _UNIVERSE_DIR / f"{name}.json"
    path.write_text(json.dumps(sorted(set(symbols)), indent=2), encoding="utf-8")
    return path


def download_nifty_500() -> list[str]:
    """Download the Nifty 500 constituent list from NSE archives.

    NSE endpoints frequently require browser-like headers and may block
    automated access. This is best-effort: if the download fails, log a warning
    and return an empty list so the caller can fall back to a stored copy.

    Returns a list of bare tickers (e.g. ``["RELIANCE", ...]``).
    """
    import csv
    from io import StringIO

    import requests

    urls = [
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://www.nseindia.com/content/indices/ind_nifty500list.csv",
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/csv,application/xhtml+xml,*/*",
    }

    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - network failures are expected
            logger.warning("NSE universe download failed for %s: %s", url, exc)
            continue

        reader = csv.DictReader(StringIO(resp.text))
        symbols = [
            row["Symbol"].strip()
            for row in reader
            if row.get("Symbol", "").strip()
        ]
        if symbols:
            save_universe(NIFTY_500, symbols)
            logger.info("Downloaded %d Nifty 500 symbols", len(symbols))
            return symbols

    logger.error("All NSE universe download URLs failed")
    return []
