"""Strategy base class and metadata definitions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

import pandas as pd

from app.config import Settings
from app.data.provider import MarketDataProvider, Quote


class Category(StrEnum):
    INTRADAY = "intraday"
    SHORT_TERM = "short-term"
    LONG_TERM = "long-term"
    EVENT_DRIVEN = "event-driven"


@dataclass
class StrategyMetadata:
    """Static description of a strategy.

    ``timeframes`` lists every candle interval the strategy needs (e.g.
    ``["10m", "daily"]``). The scheduler fetches each once per symbol and shares
    them, so adding a timeframe adds no per-stock API cost beyond that interval.

    ``run_at`` ("HH:MM", IST) marks a daily-scheduled strategy: the auto-tick
    runs it once per trading day at that time instead of every
    ``frequency_minutes``.
    """

    name: str
    description: str
    category: Category
    universe: str  # e.g. "NIFTY_500", "NIFTY_50", "ALL_NSE"
    schedule_type: str  # "auto" or "manual"
    timeframes: list[str]  # e.g. ["10m", "daily"]
    frequency_minutes: int | None = None  # for interval auto strategies
    candle_interval: str | None = None  # primary interval (display)
    run_at: str | None = None  # "HH:MM" IST, for daily auto strategies


@dataclass
class Signal:
    """A single strategy signal for one stock."""

    symbol: str
    strategy_name: str
    timestamp: datetime
    price: float
    metadata: dict = field(default_factory=dict)  # subtype, confluence_score, etc.


class Strategy(ABC):
    """Base class for all strategies.

    Subclasses implement :meth:`scan` and provide a :meth:`metadata` property.
    """

    #: Live market-data provider. Set by the application/scheduler after
    #: construction so strategies can fetch realtime quotes directly.
    provider: MarketDataProvider | None = None

    @property
    @abstractmethod
    def metadata(self) -> StrategyMetadata:
        raise NotImplementedError

    @abstractmethod
    def scan(
        self,
        data: dict[str, dict[str, pd.DataFrame]],
        settings: Settings,
    ) -> list[Signal]:
        """Evaluate the strategy.

        ``data`` maps symbol -> {timeframe -> OHLCV DataFrame}. All timeframes
        the strategy needs were fetched once and shared. Returns a list of
        signals (empty if no matches).
        """
        raise NotImplementedError

    def get_universe(self, settings: Settings) -> list[str]:
        """Return the list of symbols this strategy scans.

        Defaults to a static list from config; override for dynamic universes.
        """
        from app.strategies.universe import get_universe

        return get_universe(self.metadata.universe, settings)

    def prefilter(self, quotes: dict[str, Quote], settings: Settings) -> list[str] | None:
        """Cheap first pass before candle history is fetched.

        Receives one batched realtime quote per symbol in the universe and
        returns the symbols worth fetching candles for. Returning ``None``
        means "no prefilter": the scheduler fetches the full universe. The
        default implementation skips prefiltration so existing strategies are
        unaffected.
        """
        return None
