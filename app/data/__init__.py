"""Data layer: market-data providers behind a common interface.

* :class:`~app.data.provider.MarketDataProvider` - abstract provider contract.
* :class:`~app.data.fyers.FyersProvider` - FYERS API v3 (NSE) provider.
* :class:`~app.data.fyers.FyersAuthenticator` - FYERS OAuth token management.
* :class:`~app.data.groww_provider.GrowwProvider` - Groww Trade API provider
  (off-hours source; needs ``GROWW_API_KEY``).
* :class:`~app.data.yfinance_provider.YFinanceProvider` - Yahoo Finance
  off-hours provider (no API key, no broker quota).
* :class:`~app.data.routing.RoutingProvider` - picks the live vs off-hours
  source by NSE session state so FYERS calls are only spent when necessary.
* :class:`~app.data.global_index.GlobalIndexProvider` - yfinance global indices.
* :class:`~app.data.rate_limit.RateLimiter` - quota-aware limiter.
* :class:`~app.data.cache.CandleCache` - persistent candle cache.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.core.logging import get_logger
from app.data.cache import CandleCache
from app.data.fyers import FyersAuthenticator, FyersProvider
from app.data.global_index import GlobalIndexProvider
from app.data.groww_provider import GrowwProvider
from app.data.provider import MarketDataProvider, Quote
from app.data.rate_limit import RateLimiter
from app.data.routing import RoutingProvider
from app.data.yfinance_provider import YFinanceProvider

logger = get_logger(__name__)


def _build_provider(
    name: str,
    settings: Settings,
    cache: CandleCache | None,
) -> MarketDataProvider | None:
    source = (name or "").strip().lower()
    if source == "fyers":
        return FyersProvider(settings, cache=cache)
    if source == "yfinance":
        return YFinanceProvider()
    if source == "groww":
        if not settings.groww_api_key:
            logger.warning(
                "GROWW_API_KEY not set; groww provider unavailable - "
                "off-hours scans fall back to yfinance"
            )
            return None
        return GrowwProvider(
            api_key=settings.groww_api_key,
            base_url=settings.groww_api_base_url,
        )
    logger.warning("Unknown market-data provider %r; ignoring", name)
    return None


def build_market_provider(settings: Settings) -> MarketDataProvider:
    """Build the session-aware market-data provider.

    Live session -> the primary source (``MARKET_PROVIDER``, FYERS by default);
    outside session hours -> the off-hours source (``OFFHOURS_MARKET_PROVIDER``,
    yfinance by default) with automatic fallback to it if a live call fails.
    """
    cache = CandleCache(Path(settings.database_path).parent / "cache")
    primary = _build_provider(settings.market_provider, settings, cache)
    offhours_name = settings.offhours_market_provider
    offhours = _build_provider(offhours_name, settings, None)
    if offhours is None and offhours_name:
        logger.warning(
            "Off-hours provider %r unavailable; falling back to yfinance",
            offhours_name,
        )
        offhours = YFinanceProvider()
    if primary is None:
        if offhours is None:
            raise RuntimeError("No market-data provider configured")
        return offhours
    if offhours is None:
        return primary
    return RoutingProvider(primary, offhours)


def build_global_index_provider(settings: Settings) -> GlobalIndexProvider:
    """Build the global-index provider (source configurable, default yfinance)."""
    return GlobalIndexProvider()


__all__ = [
    "CandleCache",
    "FyersAuthenticator",
    "FyersProvider",
    "GlobalIndexProvider",
    "MarketDataProvider",
    "Quote",
    "RateLimiter",
    "RoutingProvider",
    "GrowwProvider",
    "YFinanceProvider",
    "build_global_index_provider",
    "build_market_provider",
]
