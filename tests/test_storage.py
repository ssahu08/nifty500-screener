"""Tests for the SQLite signal store with rolling retention."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.config import Settings
from app.storage.db import IST, SignalStore
from app.strategies.base import Signal


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, database_path=str(tmp_path / "test.db"))


def _signal(symbol: str = "SBIN", price: float = 100.0) -> Signal:
    return Signal(
        symbol=symbol,
        strategy_name="TestStrategy",
        timestamp=datetime.now(IST),
        price=price,
    )


def test_save_and_get_latest(tmp_path) -> None:
    store = SignalStore(_settings(tmp_path))
    store.save_run("TestStrategy", [_signal("SBIN"), _signal("RELIANCE", 200.0)])

    latest = store.get_latest("TestStrategy")
    assert len(latest) == 2
    symbols = {row["symbol"] for row in latest}
    assert symbols == {"SBIN", "RELIANCE"}


def test_get_latest_empty_when_no_runs(tmp_path) -> None:
    store = SignalStore(_settings(tmp_path))
    assert store.get_latest("NeverRan") == []


def test_get_latest_returns_most_recent_run(tmp_path) -> None:
    store = SignalStore(_settings(tmp_path))
    store.save_run("TestStrategy", [_signal("OLD", 1.0)])
    store.save_run("TestStrategy", [_signal("NEW", 2.0)])

    latest = store.get_latest("TestStrategy")
    assert len(latest) == 1
    assert latest[0]["symbol"] == "NEW"


def test_get_history_returns_within_window(tmp_path) -> None:
    store = SignalStore(_settings(tmp_path))
    store.save_run("TestStrategy", [_signal("A"), _signal("B")])
    history = store.get_history("TestStrategy", days=14)
    assert len(history) == 2


def test_get_all_latest(tmp_path) -> None:
    store = SignalStore(_settings(tmp_path))
    store.save_run("S1", [_signal("A")])
    store.save_run("S2", [_signal("B")])
    all_latest = store.get_all_latest()
    assert set(all_latest.keys()) == {"S1", "S2"}


def test_prune_old_deletes_expired_rows(tmp_path) -> None:
    store = SignalStore(_settings(tmp_path))
    store.save_run("TestStrategy", [_signal("A")])

    # Manually age the rows beyond the retention window.
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    old = (datetime.now(IST) - timedelta(days=30)).isoformat()
    conn.execute("UPDATE runs SET run_at = ?", (old,))
    conn.commit()
    conn.close()

    deleted = store.prune_old(days=14)
    assert deleted == 1
    assert store.get_latest("TestStrategy") == []


def test_save_run_empty_list(tmp_path) -> None:
    store = SignalStore(_settings(tmp_path))
    assert store.save_run("TestStrategy", []) == []


def test_save_run_returns_only_new_signals(tmp_path) -> None:
    store = SignalStore(_settings(tmp_path))
    candle_time = datetime.now(IST)
    first = _signal("SBIN")
    first.timestamp = candle_time

    saved = store.save_run("TestStrategy", [first])
    assert len(saved) == 1

    # Re-evaluating the same candle stores (and returns) nothing.
    again = _signal("SBIN")
    again.timestamp = candle_time
    assert store.save_run("TestStrategy", [again]) == []
    assert len(store.get_latest("TestStrategy")) == 1

    # A new candle for the same symbol is a new occurrence.
    later = _signal("SBIN")
    later.timestamp = candle_time + timedelta(minutes=10)
    assert len(store.save_run("TestStrategy", [later])) == 1
    assert len(store.get_history("TestStrategy", days=14)) == 2


def test_save_run_refreshes_price_and_metadata_on_repeat(tmp_path) -> None:
    """A re-evaluated candle must refresh live price/rank without re-alerting."""
    store = SignalStore(_settings(tmp_path))
    candle_time = datetime.now(IST)

    first = _signal("SBIN", 100.0)
    first.timestamp = candle_time
    first.metadata = {"score": 10.0, "quality": "LOW"}
    assert len(store.save_run("TestStrategy", [first])) == 1

    again = _signal("SBIN", 105.0)
    again.timestamp = candle_time
    again.metadata = {"score": 82.5, "quality": "HIGH"}
    assert store.save_run("TestStrategy", [again]) == []

    latest = store.get_latest("TestStrategy")
    assert len(latest) == 1
    assert latest[0]["price"] == 105.0
    assert latest[0]["metadata"]["score"] == 82.5
    assert latest[0]["metadata"]["quality"] == "HIGH"


def test_long_and_short_on_same_candle_are_distinct(tmp_path) -> None:
    """A bidirectional strategy may emit both directions for one candle."""
    store = SignalStore(_settings(tmp_path))
    candle_time = datetime.now(IST)

    long_signal = _signal("SBIN")
    long_signal.timestamp = candle_time
    long_signal.metadata = {"direction": "LONG", "band": "BUY"}
    short_signal = _signal("SBIN")
    short_signal.timestamp = candle_time
    short_signal.metadata = {"direction": "SHORT", "band": "BUY"}

    assert len(store.save_run("TestStrategy", [long_signal, short_signal])) == 2
    latest = store.get_latest("TestStrategy")
    assert {row["direction"] for row in latest} == {"LONG", "SHORT"}

    # Re-saving both is a no-op; the unique key includes direction.
    assert store.save_run("TestStrategy", [long_signal, short_signal]) == []


def test_variants_on_same_candle_are_distinct(tmp_path) -> None:
    """OEL weekly and monthly can share a period-end timestamp."""
    store = SignalStore(_settings(tmp_path))
    candle_time = datetime.now(IST)

    weekly = _signal("SBIN")
    weekly.timestamp = candle_time
    weekly.metadata = {"variant": "weekly"}
    monthly = _signal("SBIN")
    monthly.timestamp = candle_time
    monthly.metadata = {"variant": "monthly"}

    assert len(store.save_run("TestStrategy", [weekly, monthly])) == 2
    latest = store.get_latest("TestStrategy")
    assert {row["variant"] for row in latest} == {"weekly", "monthly"}

    # Re-saving both is a no-op; the unique key includes variant.
    assert store.save_run("TestStrategy", [weekly, monthly]) == []


def test_migration_backfills_direction_from_metadata(tmp_path) -> None:
    """An older database without a direction column is upgraded in place."""
    import json
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL,
            run_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            signal_time TEXT NOT NULL,
            price REAL NOT NULL,
            metadata TEXT DEFAULT '{}'
        );
        """
    )
    candle_time = datetime.now(IST).isoformat()
    conn.execute(
        "INSERT INTO runs "
        "(strategy_name, run_at, symbol, signal_time, price, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "TestStrategy",
            candle_time,
            "SBIN",
            candle_time,
            100.0,
            json.dumps({"direction": "LONG", "variant": "weekly"}),
        ),
    )
    conn.commit()
    conn.close()

    store = SignalStore(Settings(_env_file=None, database_path=str(db_path)))
    rows = store.get_latest("TestStrategy")
    assert len(rows) == 1
    assert rows[0]["direction"] == "LONG"
    assert rows[0]["variant"] == "weekly"

    # The migrated row participates in the new direction/variant-aware dedup key.
    repeat = _signal("SBIN")
    repeat.timestamp = datetime.fromisoformat(candle_time)
    repeat.metadata = {"direction": "LONG", "variant": "weekly"}
    assert store.save_run("TestStrategy", [repeat]) == []
