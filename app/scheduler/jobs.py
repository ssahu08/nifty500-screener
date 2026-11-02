"""Scheduler for automatic and manual strategy runs.

Uses APScheduler with an interval tick. On each tick it works out which
auto-strategies are due and dispatches each to its own background thread, so
one slow full-universe fetch cannot delay the others. A strategy is never run
twice concurrently, and a strategy or API failure never crashes the service
(NFR-6).

Within a single run, the union of the due strategies' universes is fetched
**once**, then every strategy in that run evaluates the shared in-memory data
(the BRD fetch-once-evaluate-many rule).

Auto-strategies come in two flavours:

* **interval** - ``frequency_minutes`` set, runs while the market is open;
* **daily** - ``run_at`` ("HH:MM" IST) set, runs once per trading day near
  that time (e.g. the 15:35 EOD compression scan).

Strategies may override :meth:`~app.strategies.base.Strategy.prefilter` to
prune their universe with batched realtime quotes before any candle history
is fetched (the BRD two-stage intraday scan).
"""

from __future__ import annotations

import threading
from datetime import datetime, time, timedelta, timezone
from time import monotonic

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings
from app.core.logging import get_logger
from app.core.market import is_market_open, is_trading_day
from app.data.provider import MarketDataProvider
from app.storage.db import SignalStore
from app.strategies.base import Strategy

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

#: How long after ``run_at`` a daily strategy may still start (late boot).
DAILY_RUN_WINDOW = timedelta(minutes=30)


class StrategyScheduler:
    """Drive strategy runs on a schedule or on manual trigger."""

    def __init__(
        self,
        settings: Settings,
        registry: dict[str, Strategy],
        provider: MarketDataProvider,
        store: SignalStore,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.provider = provider
        self.store = store
        self.scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
        self._last_run: dict[str, datetime] = {}
        #: Live run progress per strategy (`run_status` reads this; the
        #: dashboard polls it to animate what a manual run is doing).
        self.progress: dict[str, dict] = {}
        #: A strategy may not run twice at once (a tick can fire while the
        #: previous run is still fetching, and a manual click can race the
        #: auto tick). Each due strategy runs in its own thread so a slow
        #: full-universe scan never delays the others.
        self._run_lock = threading.Lock()
        self._running: set[str] = set()
        self._threads: dict[str, threading.Thread] = {}

    # -- scheduling ---------------------------------------------------------
    def schedule_auto_strategies(self) -> None:
        """Register an interval tick that runs due auto-strategies."""
        self.scheduler.add_job(
            self.tick,
            IntervalTrigger(minutes=1),
            id="strategy-tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Scheduled 1-minute tick for %d strategies", len(self.registry))

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler started")

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    # -- tick / execution ---------------------------------------------------
    def tick(self) -> None:
        """Start every auto-strategy that is due (interval or daily).

        Runs are dispatched to per-strategy daemon threads, so a slow
        full-universe fetch (e.g. the 10-minute Doji scan) can no longer hold
        the tick and starve the other strategies. ``_last_run`` is stamped
        when a run *finishes*, so an interval strategy re-arms its frequency
        from completion rather than restarting immediately.
        """
        now = datetime.now(IST)
        for name, strategy in self.registry.items():
            meta = strategy.metadata
            if meta.schedule_type != "auto":
                continue
            if meta.run_at:
                if self._is_daily_due(name, meta.run_at, now):
                    self._start_async(name)
                continue
            if not is_market_open(now):
                continue
            if self._is_due(name, meta.frequency_minutes, now):
                self._start_async(name)

    def run_manual(self, strategy_name: str) -> int:
        """Manually trigger a strategy and return the number of signals.

        If the same strategy is already running (auto or another manual
        trigger), this returns 0 instead of starting a duplicate run.
        """
        strategy = self.registry.get(strategy_name)
        if strategy is None:
            raise KeyError(f"Unknown strategy {strategy_name!r}")
        return self._run_auto_due([strategy_name], manual=True)

    def run_status(self, strategy_name: str) -> dict:
        """Snapshot of the strategy's latest/live run for the dashboard."""
        entry = self.progress.get(strategy_name)
        if entry is None:
            return {"state": "idle"}
        snapshot = {k: v for k, v in entry.items() if not k.startswith("_")}
        if entry["state"] == "running":
            snapshot["elapsed_s"] = round(monotonic() - entry["_started"], 1)
        return snapshot

    # -- progress tracking --------------------------------------------------
    def _progress_start(self, name: str, manual: bool) -> None:
        self.progress[name] = {
            "state": "running",
            "stage": "prefilter",
            "manual": manual,
            "started_at": datetime.now(IST).isoformat(),
            "done": 0,
            "total": 0,
            "signals": 0,
            "error": None,
            "_started": monotonic(),
        }

    def _progress_update(self, name: str, **fields) -> None:
        entry = self.progress.get(name)
        if entry is not None and entry["state"] == "running":
            entry.update(fields)

    def _progress_finish(self, name: str, *, signals: int, error: str | None) -> None:
        entry = self.progress.get(name)
        if entry is None:
            return
        entry.update(
            {
                "state": "error" if error else "done",
                "stage": "failed" if error else "finished",
                "signals": signals,
                "error": error,
                "elapsed_s": round(monotonic() - entry["_started"], 1),
                "finished_at": datetime.now(IST).isoformat(),
            }
        )

    # -- internals ----------------------------------------------------------
    def _is_due(self, name: str, frequency_minutes: int | None, now: datetime) -> bool:
        if not frequency_minutes:
            return False
        last = self._last_run.get(name)
        if last is None:
            return True
        return (now - last) >= timedelta(minutes=frequency_minutes)

    def _is_daily_due(self, name: str, run_at: str, now: datetime) -> bool:
        """True once per trading day when the clock reaches ``run_at`` (IST)."""
        if not is_trading_day(now.date()):
            return False
        try:
            hours, minutes = (int(part) for part in run_at.split(":"))
            run_time = time(hours, minutes)
        except ValueError:
            logger.warning("Invalid run_at %r for strategy %s", run_at, name)
            return False
        deadline = (datetime.combine(now.date(), run_time) + DAILY_RUN_WINDOW).time()
        if now.time() < run_time or now.time() > deadline:
            return False
        last = self._last_run.get(name)
        return last is None or last.date() != now.date()

    def _start_async(self, name: str) -> bool:
        """Claim ``name`` and run it in a background thread (non-blocking tick)."""
        if not self._claim([name]):
            logger.info("Skipping %s: previous run still in progress", name)
            return False
        thread = threading.Thread(
            target=self._execute,
            args=([name],),
            kwargs={"manual": False},
            name=f"strategy-run-{name}",
            daemon=True,
        )
        with self._run_lock:
            self._threads[name] = thread
        try:
            thread.start()
        except Exception:  # noqa: BLE001 - never leave a strategy stuck as running
            logger.exception("Could not start background run for %s", name)
            self._release([name])
            return False
        return True

    def _claim(self, names: list[str]) -> bool:
        """Reserve ``names`` atomically; False if any is already running."""
        with self._run_lock:
            if any(name in self._running for name in names):
                return False
            self._running.update(names)
            return True

    def _release(self, names: list[str]) -> None:
        with self._run_lock:
            for name in names:
                self._running.discard(name)
                self._threads.pop(name, None)

    def _run_auto_due(self, names: list[str], *, manual: bool = False) -> int:
        """Run the given strategies synchronously (used by manual triggers)."""
        if not self._claim(names):
            logger.info("Skipping %s: previous run still in progress", names)
            return 0
        return self._execute(names, manual=manual)

    def _execute(self, names: list[str], *, manual: bool = False) -> int:
        """Fetch the due strategies' universes once and run each on shared data."""
        try:
            return self._execute_inner(names, manual=manual)
        finally:
            self._release(names)

    def _execute_inner(self, names: list[str], *, manual: bool = False) -> int:
        total = 0
        for name in names:
            self._progress_start(name, manual)
        kept = self._prefiltered_universes(names)
        for name in names:
            self._progress_update(name, stage="fetching")
        try:
            data = self._fetch_shared(names, kept)
        except Exception as exc:  # noqa: BLE001
            logger.error("Data fetch failed for %s: %s", names, exc)
            for name in names:
                self._progress_finish(name, signals=0, error=f"data fetch failed: {exc}")
            return 0

        for name in names:
            strategy = self.registry[name]
            symbols = kept.get(name)
            strategy_data = (
                data if symbols is None else {s: data.get(s, {}) for s in symbols}
            )
            self._progress_update(name, stage="scanning")
            try:
                strategy._trigger_mode = "manual" if manual else "auto"
                signals = strategy.scan(strategy_data, self.settings)
                new_signals = self.store.save_run(name, signals)
                # Stamp completion, not start: a long scan must not make the
                # next tick think the strategy is due again immediately.
                self._last_run[name] = datetime.now(IST)
                total += len(signals)
                self._progress_finish(name, signals=len(signals), error=None)
                if signals:
                    logger.info(
                        "%s strategy %s produced %d signals (%d new)",
                        "Manual" if manual else "Auto",
                        name,
                        len(signals),
                        len(new_signals),
                    )
                if new_signals:
                    try:
                        from app.alerts import send_alert

                        send_alert(self.settings, name, new_signals)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Telegram alert failed: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("Strategy %s failed: %s", name, exc, exc_info=True)
                self._progress_finish(name, signals=0, error=str(exc))
        return total

    def _prefiltered_universes(
        self, names: list[str]
    ) -> dict[str, list[str] | None]:
        """Prune universes via batched quotes for strategies that support it.

        Returns ``name -> kept symbols`` (``None`` = full universe). Quote
        availability or failures fall back to the full universe so data issues
        never silently drop stocks (NFR-6).
        """
        result: dict[str, list[str] | None] = {}
        for name in names:
            strategy = self.registry[name]
            if type(strategy).prefilter is Strategy.prefilter:
                result[name] = None  # strategy does not override the hook
                continue
            universe = strategy.get_universe(self.settings)
            try:
                quotes = self.provider.get_quotes(universe)
                kept = strategy.prefilter(quotes, self.settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Prefilter for %s failed (%s); using the full universe", name, exc
                )
                kept = None
            result[name] = kept
            if kept is not None:
                logger.info(
                    "Prefilter kept %d/%d symbols for %s", len(kept), len(universe), name
                )
        return result

    def _fetch_shared(
        self, names: list[str], kept: dict[str, list[str] | None] | None = None
    ) -> dict[str, dict[str, object]]:
        """Fetch and cache data for the union of the given strategies' universes.

        Returns ``symbol -> {timeframe -> DataFrame}``. Each (symbol, timeframe)
        is fetched at most once regardless of how many strategies need it, so
        adding a strategy never multiplies API calls. ``kept`` optionally
        narrows each strategy's universe (quote prefilter).
        """
        kept = kept or {}
        universe: set[str] = set()
        for name in names:
            symbols = kept.get(name)
            if symbols is None:
                symbols = self.registry[name].get_universe(self.settings)
            universe.update(symbols)

        timeframes = self._required_timeframes(names)
        end = _today()

        ordered = sorted(universe)
        total_symbols = len(ordered)
        data: dict[str, dict[str, object]] = {}
        for done, symbol in enumerate(ordered, 1):
            data[symbol] = {}
            for tf in timeframes:
                try:
                    start = _start_date(tf)
                    data[symbol][tf] = self.provider.get_candles(symbol, tf, start, end)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to fetch %s (%s): %s", symbol, tf, exc)
            for name in names:
                self._progress_update(name, stage="fetching", done=done, total=total_symbols)
        return data

    def _required_timeframes(self, names: list[str]) -> list[str]:
        """Union of the candle intervals the given strategies need, ordered."""
        order = {"10m": 0, "1h": 1, "60m": 1, "daily": 2, "1d": 2}
        intervals = {
            tf
            for name in names
            for tf in (self.registry[name].metadata.timeframes or ["daily"])
        }
        if not intervals:
            return ["daily"]
        return sorted(intervals, key=lambda x: order.get(x, 99))


def _today():
    from datetime import date

    return date.today()


#: History lookback per timeframe; daily needs a year for 52-week context
#: (FYERS rejects 1D ranges beyond 366 days).
LOOKBACK_DAYS = {"daily": 360, "1d": 360}


def _start_date(timeframe: str = "10m"):
    from datetime import date, timedelta

    return date.today() - timedelta(days=LOOKBACK_DAYS.get(timeframe, 30))
