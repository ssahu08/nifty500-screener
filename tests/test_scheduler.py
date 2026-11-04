"""Tests for the strategy scheduler."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.config import Settings
from app.data.provider import Quote
from app.scheduler.jobs import StrategyScheduler
from app.strategies.base import Category, Signal, Strategy, StrategyMetadata


def _settings() -> Settings:
    return Settings(_env_file=None, database_path="data/test.db")


class _FakeProvider:
    def __init__(self):
        self.calls = 0

    def get_candles(self, symbol, interval, start, end):
        self.calls += 1
        idx = pd.to_datetime([pd.Timestamp("2026-01-02")], utc=True).tz_convert("Asia/Kolkata")
        return pd.DataFrame(
            {
                "open": [100.0],
                "high": [101.0],
                "low": [99.0],
                "close": [100.5],
                "volume": [1.0],
            },
            index=idx,
        )


class _EchoStrategy(Strategy):
    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Echo",
            description="echoes a signal",
            category=Category.INTRADAY,
            universe="NIFTY_50",
            schedule_type="manual",
            timeframes=["10m", "daily"],
        )

    def scan(self, data, settings):
        return [
            Signal(
                symbol="NSE:SBIN-EQ",
                strategy_name="Echo",
                timestamp=datetime(2026, 1, 2, 9, 30),
                price=100.5,
            )
        ]


def test_run_manual_produces_signals(tmp_path) -> None:
    settings = _settings()
    provider = _FakeProvider()
    registry = {"Echo": _EchoStrategy()}
    from app.storage.db import SignalStore

    store = SignalStore(settings)
    scheduler = StrategyScheduler(settings, registry, provider, store)

    count = scheduler.run_manual("Echo")
    assert count == 1
    latest = store.get_latest("Echo")
    assert len(latest) == 1


def test_run_manual_unknown_strategy_raises(tmp_path) -> None:
    settings = _settings()
    scheduler = StrategyScheduler(settings, {}, _FakeProvider(), None)
    try:
        scheduler.run_manual("Nope")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


# -- live run progress -------------------------------------------------------
def test_run_status_idle_before_any_run() -> None:
    scheduler = StrategyScheduler(_settings(), {}, None, None)
    assert scheduler.run_status("Echo") == {"state": "idle"}


def test_run_status_tracks_manual_run() -> None:
    from app.storage.db import SignalStore

    settings = _settings()
    scheduler = StrategyScheduler(
        settings, {"Echo": _EchoStrategy()}, _FakeProvider(), SignalStore(settings)
    )

    scheduler.run_manual("Echo")
    status = scheduler.run_status("Echo")
    assert status["state"] == "done"
    assert status["stage"] == "finished"
    assert status["signals"] == 1
    assert status["manual"] is True
    assert status["error"] is None
    assert status["total"] > 0  # fetch counters were recorded
    assert status["elapsed_s"] >= 0


def test_run_status_records_failure() -> None:
    from app.storage.db import SignalStore

    class _Broken(Strategy):
        @property
        def metadata(self) -> StrategyMetadata:
            return StrategyMetadata(
                name="Broken", description="always fails", category=Category.INTRADAY,
                universe="NIFTY_50", schedule_type="manual", timeframes=["daily"],
            )

        def scan(self, data, settings):
            raise RuntimeError("kernel panic")

    settings = _settings()
    scheduler = StrategyScheduler(
        settings, {"Broken": _Broken()}, _FakeProvider(), SignalStore(settings)
    )

    assert scheduler.run_manual("Broken") == 0
    status = scheduler.run_status("Broken")
    assert status["state"] == "error"
    assert status["stage"] == "failed"
    assert "kernel panic" in status["error"]


def test_is_due_first_run_is_due() -> None:
    scheduler = StrategyScheduler(_settings(), {}, None, None)
    assert scheduler._is_due("X", 10, datetime(2026, 1, 1)) is True


def test_is_due_respects_frequency() -> None:
    scheduler = StrategyScheduler(_settings(), {}, None, None)
    now = datetime(2026, 1, 1, 12, 0)
    scheduler._last_run["X"] = now - timedelta(minutes=5)
    assert scheduler._is_due("X", 10, now) is False
    scheduler._last_run["X"] = now - timedelta(minutes=15)
    assert scheduler._is_due("X", 10, now) is True


def test_required_timeframes_union() -> None:
    class _S(Strategy):
        @property
        def metadata(self):
            return StrategyMetadata(
                name="X", description="", category=Category.INTRADAY,
                universe="NIFTY_50", schedule_type="auto",
                timeframes=["10m", "daily"],
            )

        def scan(self, data, settings):
            return []

    class _D(Strategy):
        @property
        def metadata(self):
            return StrategyMetadata(
                name="Y", description="", category=Category.INTRADAY,
                universe="NIFTY_50", schedule_type="auto",
                timeframes=["daily"],
            )

        def scan(self, data, settings):
            return []

    registry = {"X": _S(), "Y": _D()}
    scheduler = StrategyScheduler(_settings(), registry, None, None)
    assert scheduler._required_timeframes(["X", "Y"]) == ["10m", "daily"]


def test_fetch_shared_builds_nested_data() -> None:
    class _S(Strategy):
        @property
        def metadata(self):
            return StrategyMetadata(
                name="X", description="", category=Category.INTRADAY,
                universe="NIFTY_50", schedule_type="auto",
                timeframes=["10m", "daily"],
            )

        def scan(self, data, settings):
            return []

    provider = _FakeProvider()
    scheduler = StrategyScheduler(_settings(), {"X": _S()}, provider, None)
    data = scheduler._fetch_shared(["X"])
    # NIFTY_50 fallback has multiple symbols, each with both timeframes.
    assert len(data) > 0
    for frames in data.values():
        assert "10m" in frames and "daily" in frames


def test_start_date_lookback_per_timeframe() -> None:
    from datetime import date

    from app.scheduler.jobs import _start_date

    today = date.today()
    assert _start_date() == today - timedelta(days=30)
    assert _start_date("10m") == today - timedelta(days=30)
    assert _start_date("daily") == today - timedelta(days=360)
    assert _start_date("1d") == today - timedelta(days=360)


# -- daily run_at scheduling -------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))


class _DailyStrategy(Strategy):
    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Daily", description="daily scan", category=Category.INTRADAY,
            universe="NIFTY_50", schedule_type="auto",
            timeframes=["daily"], run_at="15:20",
        )

    def scan(self, data, settings):
        return []


def _daily_scheduler() -> StrategyScheduler:
    return StrategyScheduler(
        _settings(), {"Daily": _DailyStrategy()}, _FakeProvider(), None
    )


def test_daily_due_at_run_time() -> None:
    scheduler = _daily_scheduler()
    friday = datetime(2026, 10, 16, 0, 0, tzinfo=IST)  # a Friday
    assert scheduler._is_daily_due("Daily", "15:20", friday.replace(hour=15, minute=20))


def test_daily_not_due_before_run_time() -> None:
    scheduler = _daily_scheduler()
    friday = datetime(2026, 10, 16, 15, 19, tzinfo=IST)
    assert not scheduler._is_daily_due("Daily", "15:20", friday)


def test_daily_due_within_late_window() -> None:
    scheduler = _daily_scheduler()
    assert scheduler._is_daily_due(
        "Daily", "15:20", datetime(2026, 10, 16, 15, 49, tzinfo=IST)
    )


def test_daily_not_due_after_window() -> None:
    scheduler = _daily_scheduler()
    assert not scheduler._is_daily_due(
        "Daily", "15:20", datetime(2026, 10, 16, 15, 51, tzinfo=IST)
    )


def test_daily_runs_once_per_day() -> None:
    scheduler = _daily_scheduler()
    scheduler._last_daily_run[("Daily", "15:20")] = datetime(
        2026, 10, 16, 15, 25, tzinfo=IST
    )
    assert not scheduler._is_daily_due(
        "Daily", "15:20", datetime(2026, 10, 16, 15, 30, tzinfo=IST)
    )
    # Next trading day it is due again.
    assert scheduler._is_daily_due(
        "Daily", "15:20", datetime(2026, 10, 19, 15, 20, tzinfo=IST)
    )


class _TwoSlotStrategy(Strategy):
    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="TwoSlot", description="two daily slots", category=Category.INTRADAY,
            universe="NIFTY_50", schedule_type="auto",
            timeframes=["10m", "daily"], run_at="09:25", extra_run_at=["15:35"],
        )

    def scan(self, data, settings):
        return []


class _FrozenDatetime(datetime):
    """A ``datetime`` whose ``now`` is controlled by the test."""

    current = datetime(2026, 10, 16, 9, 25, tzinfo=IST)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


def test_daily_slots_are_independent() -> None:
    scheduler = StrategyScheduler(
        _settings(), {"TwoSlot": _TwoSlotStrategy()}, _FakeProvider(), None
    )
    assert scheduler._daily_slots(scheduler.registry["TwoSlot"].metadata) == [
        "09:25",
        "15:35",
    ]
    now = datetime(2026, 10, 16, 9, 25, tzinfo=IST)
    assert scheduler._is_daily_due("TwoSlot", "09:25", now)
    scheduler._last_daily_run[("TwoSlot", "09:25")] = now
    assert not scheduler._is_daily_due("TwoSlot", "09:25", now)
    # The later slot is still due on the same day.
    assert scheduler._is_daily_due(
        "TwoSlot", "15:35", datetime(2026, 10, 16, 15, 35, tzinfo=IST)
    )


def test_tick_starts_each_daily_slot(monkeypatch) -> None:
    import app.scheduler.jobs as jobs

    scheduler = StrategyScheduler(
        _settings(), {"TwoSlot": _TwoSlotStrategy()}, _FakeProvider(), None
    )
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        scheduler,
        "_start_async",
        lambda name, slot=None: calls.append((name, slot)) or True,
    )
    monkeypatch.setattr(jobs, "datetime", _FrozenDatetime)

    _FrozenDatetime.current = datetime(2026, 10, 16, 9, 25, tzinfo=IST)
    scheduler.tick()
    _FrozenDatetime.current = datetime(2026, 10, 16, 15, 35, tzinfo=IST)
    scheduler.tick()

    assert calls == [("TwoSlot", "09:25"), ("TwoSlot", "15:35")]


def test_tick_respects_auto_run_allowed(monkeypatch) -> None:
    import app.scheduler.jobs as jobs

    class _Closed(_TwoSlotStrategy):
        def auto_run_allowed(self, now=None):
            return False

    scheduler = StrategyScheduler(
        _settings(), {"TwoSlot": _Closed()}, _FakeProvider(), None
    )
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        scheduler,
        "_start_async",
        lambda name, slot=None: calls.append((name, slot)) or True,
    )
    monkeypatch.setattr(jobs, "datetime", _FrozenDatetime)
    _FrozenDatetime.current = datetime(2026, 10, 16, 9, 25, tzinfo=IST)
    scheduler.tick()
    assert calls == []


def test_daily_skips_weekends() -> None:
    scheduler = _daily_scheduler()
    saturday = datetime(2026, 10, 17, 15, 20, tzinfo=IST)
    assert not scheduler._is_daily_due("Daily", "15:20", saturday)


def test_daily_invalid_run_at_is_not_due() -> None:
    scheduler = _daily_scheduler()
    now = datetime(2026, 10, 16, 15, 20, tzinfo=IST)
    assert not scheduler._is_daily_due("Daily", "25:99", now)
    assert not scheduler._is_daily_due("Daily", "nonsense", now)


# -- quote prefilter ---------------------------------------------------------
class _QuoteProvider(_FakeProvider):
    def __init__(self):
        super().__init__()
        self.quote_calls = 0
        self.candle_symbols: list[str] = []

    def get_quotes(self, symbols):
        self.quote_calls += 1
        return {
            f"NSE:{s}-EQ": Quote(f"NSE:{s}-EQ", ltp=100.0, high=101.0, low=99.5)
            for s in symbols
        }

    def get_candles(self, symbol, interval, start, end):
        self.candle_symbols.append(symbol)
        return super().get_candles(symbol, interval, start, end)


class _PrefilterStrategy(Strategy):
    def __init__(self):
        self.seen_symbols: list[str] = []

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name="Pref", description="prefiltered", category=Category.INTRADAY,
            universe="NIFTY_50", schedule_type="manual", timeframes=["daily"],
        )

    def get_universe(self, settings):
        return ["AAA", "BBB", "CCC"]

    def prefilter(self, quotes, settings):
        return [s for s in ("AAA", "BBB", "CCC") if s != "BBB"]

    def scan(self, data, settings):
        self.seen_symbols = list(data.keys())
        return []


def test_prefilter_limits_candle_fetches_and_scan_symbols() -> None:
    from app.storage.db import SignalStore

    provider = _QuoteProvider()
    strategy = _PrefilterStrategy()
    store = SignalStore(_settings())
    scheduler = StrategyScheduler(_settings(), {"Pref": strategy}, provider, store)

    scheduler.run_manual("Pref")

    assert provider.quote_calls == 1
    assert provider.candle_symbols == ["AAA", "CCC"]
    assert strategy.seen_symbols == ["AAA", "CCC"]


def test_prefilter_failure_falls_back_to_full_universe() -> None:
    class _BrokenQuotes(_QuoteProvider):
        def get_quotes(self, symbols):
            raise RuntimeError("quotes down")

    provider = _BrokenQuotes()
    strategy = _PrefilterStrategy()
    from app.storage.db import SignalStore

    scheduler = StrategyScheduler(
        _settings(), {"Pref": strategy}, provider, SignalStore(_settings())
    )
    scheduler.run_manual("Pref")

    assert sorted(provider.candle_symbols) == ["AAA", "BBB", "CCC"]
    assert sorted(strategy.seen_symbols) == ["AAA", "BBB", "CCC"]


def test_strategies_without_prefilter_do_not_call_quotes() -> None:
    from app.storage.db import SignalStore

    provider = _QuoteProvider()
    scheduler = StrategyScheduler(
        _settings(), {"Echo": _EchoStrategy()}, provider, SignalStore(_settings())
    )
    scheduler.run_manual("Echo")
    assert provider.quote_calls == 0


# -- Telegram alert wiring (regression: alerts were never sent) --------------
def _fresh_scheduler(tmp_path, monkeypatch, send):
    """Scheduler over an empty DB with ``app.alerts.send_alert`` faked."""
    import app.alerts as alerts_mod
    from app.storage.db import SignalStore

    monkeypatch.setattr(alerts_mod, "send_alert", send)
    settings = Settings(
        _env_file=None, database_path=str(tmp_path / "alerts.db")
    )
    scheduler = StrategyScheduler(
        settings, {"Echo": _EchoStrategy()}, _FakeProvider(), SignalStore(settings)
    )
    return scheduler


def test_manual_run_sends_telegram_alert_for_new_signals(tmp_path, monkeypatch) -> None:
    """A completed run must notify Telegram with the newly stored signals."""
    sent: list[tuple[str, list]] = []
    scheduler = _fresh_scheduler(
        tmp_path,
        monkeypatch,
        lambda settings, name, signals: sent.append((name, signals)) or True,
    )

    count = scheduler.run_manual("Echo")

    assert count == 1
    assert len(sent) == 1
    name, signals = sent[0]
    assert name == "Echo"
    assert [s.symbol for s in signals] == ["NSE:SBIN-EQ"]


def test_second_run_does_not_realert_same_signal(tmp_path, monkeypatch) -> None:
    """save_run dedup means re-detecting a stored signal must not re-alert."""
    sent: list[str] = []
    scheduler = _fresh_scheduler(
        tmp_path,
        monkeypatch,
        lambda settings, name, signals: sent.append(name) or True,
    )

    scheduler.run_manual("Echo")
    scheduler.run_manual("Echo")

    assert sent == ["Echo"]  # only the first run had a genuinely new signal


def test_alert_failure_never_crashes_the_run(tmp_path, monkeypatch) -> None:
    """A Telegram outage must not fail the strategy run (NFR-6)."""

    def _boom(settings, name, signals):
        raise RuntimeError("telegram down")

    scheduler = _fresh_scheduler(tmp_path, monkeypatch, _boom)

    count = scheduler.run_manual("Echo")

    assert count == 1  # signals still produced and stored
    status = scheduler.run_status("Echo")
    assert status["state"] == "done"
    assert status["error"] is None


# -- independent concurrent runs --------------------------------------------
def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _AutoStrategy(Strategy):
    """Auto interval strategy that counts scans and can block on demand."""

    def __init__(self, name: str, release: threading.Event | None = None) -> None:
        self._name = name
        self.release = release
        self.started = threading.Event()
        self.scans = 0

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            name=self._name,
            description="auto test strategy",
            category=Category.INTRADAY,
            universe="NIFTY_50",
            schedule_type="auto",
            timeframes=["daily"],
            frequency_minutes=10,
        )

    def scan(self, data, settings):
        self.scans += 1
        self.started.set()
        if self.release is not None:
            self.release.wait(5)
        return []


def _auto_scheduler(tmp_path, monkeypatch, strategies):
    from app.storage.db import SignalStore

    monkeypatch.setattr("app.scheduler.jobs.is_market_open", lambda now: True)
    settings = Settings(_env_file=None, database_path=str(tmp_path / "concurrent.db"))
    return StrategyScheduler(
        settings, strategies, _FakeProvider(), SignalStore(settings)
    )


def test_tick_does_not_block_on_a_slow_strategy(tmp_path, monkeypatch) -> None:
    """A slow scan must not delay the other due strategies."""
    release = threading.Event()
    slow = _AutoStrategy("Slow", release=release)
    fast = _AutoStrategy("Fast")
    scheduler = _auto_scheduler(tmp_path, monkeypatch, {"Slow": slow, "Fast": fast})

    scheduler.tick()  # returns immediately instead of waiting for Slow

    assert slow.started.wait(2)
    assert _wait_until(lambda: scheduler.run_status("Fast")["state"] == "done")
    assert scheduler.run_status("Slow")["state"] == "running"

    release.set()
    assert _wait_until(lambda: scheduler.run_status("Slow")["state"] == "done")


def test_due_strategy_is_not_started_twice(tmp_path, monkeypatch) -> None:
    """The running guard prevents overlapping runs of the same strategy."""
    release = threading.Event()
    slow = _AutoStrategy("Slow", release=release)
    scheduler = _auto_scheduler(tmp_path, monkeypatch, {"Slow": slow})

    scheduler.tick()
    assert slow.started.wait(2)
    scheduler.tick()  # still due: _last_run is only stamped on completion
    time.sleep(0.1)

    assert slow.scans == 1

    release.set()
    assert _wait_until(lambda: scheduler.run_status("Slow")["state"] == "done")
    # Completion stamped _last_run, so the next tick does not restart it.
    scheduler.tick()
    time.sleep(0.1)
    assert slow.scans == 1


def test_manual_run_is_ignored_while_strategy_is_running(tmp_path, monkeypatch) -> None:
    release = threading.Event()
    slow = _AutoStrategy("Slow", release=release)
    scheduler = _auto_scheduler(tmp_path, monkeypatch, {"Slow": slow})

    scheduler.tick()
    assert slow.started.wait(2)

    assert scheduler.run_manual("Slow") == 0
    assert slow.scans == 1

    release.set()
    assert _wait_until(lambda: scheduler.run_status("Slow")["state"] == "done")
