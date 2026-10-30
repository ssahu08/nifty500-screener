"""Session-aware provider routing: broker API only when it is necessary.

:class:`RoutingProvider` sends every request to the primary provider (FYERS)
while the NSE session is open, and to the off-hours provider (default
:class:`~app.data.yfinance_provider.YFinanceProvider`) outside session hours.
A failing primary call falls back to the off-hours source, so broker quota is
only ever spent when live data is actually needed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pandas as pd

from app.core.logging import get_logger
from app.core.market import is_market_open
from app.data.provider import MarketDataProvider, Quote

logger = get_logger(__name__)


class RoutingProvider(MarketDataProvider):
    """Route market-data requests by NSE session state.

    ``market_open`` may be injected for testing; it defaults to
    :func:`app.core.market.is_market_open`.
    """

    def __init__(
        self,
        primary: MarketDataProvider,
        offhours: MarketDataProvider | None = None,
        market_open: Callable[[], bool] = is_market_open,
    ) -> None:
        self.primary = primary
        self.offhours = offhours
        self._market_open = market_open
        sizes = [p.QUOTE_BATCH_SIZE for p in (primary, offhours) if p is not None]
        self.QUOTE_BATCH_SIZE = min(sizes)

    def _active(self) -> MarketDataProvider:
        if self.offhours is None or self._market_open():
            return self.primary
        return self.offhours

    # -- interface ----------------------------------------------------------
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        provider = self._active()
        try:
            return provider.get_quotes(symbols)
        except Exception as exc:  # noqa: BLE001 - fall back for any provider failure
            if provider is not self.primary or self.offhours is None:
                raise
            logger.warning("Quotes via primary provider failed (%s); using off-hours source", exc)
            return self.offhours.get_quotes(symbols)

    def get_candles(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        provider = self._active()
        try:
            return provider.get_candles(symbol, interval, start, end)
        except Exception as exc:  # noqa: BLE001 - fall back for any provider failure
            if provider is not self.primary or self.offhours is None:
                raise
            logger.warning(
                "Candles for %s via primary provider failed (%s); using off-hours source",
                symbol,
                exc,
            )
            return self.offhours.get_candles(symbol, interval, start, end)

    def health_check(self) -> bool:
        active = self._active()
        if active.health_check():
            return True
        other = self.offhours if active is self.primary else self.primary
        return bool(other and other.health_check())
