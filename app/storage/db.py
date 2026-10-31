"""SQLite storage for strategy signals with rolling retention."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.core.logging import get_logger
from app.strategies.base import Signal

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    run_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_strategy ON runs(strategy_name, run_at);
CREATE INDEX IF NOT EXISTS idx_runs_time ON runs(run_at);
"""


class SignalStore:
    """Persist and query strategy signals in SQLite."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = Path(settings.database_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Migration: older databases have no direction column. Add it and
            # backfill from the signal metadata so LONG and SHORT signals on
            # the same candle remain distinct occurrences.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            if "direction" not in columns:
                conn.execute(
                    "ALTER TABLE runs ADD COLUMN direction TEXT NOT NULL DEFAULT ''"
                )
                for row in conn.execute("SELECT id, metadata FROM runs").fetchall():
                    try:
                        direction = str(
                            json.loads(row["metadata"] or "{}").get("direction", "")
                        )
                    except (json.JSONDecodeError, TypeError):
                        direction = ""
                    if direction:
                        conn.execute(
                            "UPDATE runs SET direction = ? WHERE id = ?",
                            (direction, row["id"]),
                        )

            # One row per occurrence: dedupe any pre-existing repeats, then
            # enforce it (a candle may be re-evaluated by a later run).
            conn.execute("DROP INDEX IF EXISTS idx_runs_unique")
            conn.execute(
                "DELETE FROM runs WHERE id NOT IN "
                "(SELECT MIN(id) FROM runs GROUP BY "
                "strategy_name, symbol, signal_time, direction)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_unique "
                "ON runs(strategy_name, symbol, signal_time, direction)"
            )

    def save_run(self, strategy_name: str, signals: list[Signal]) -> list[Signal]:
        """Save a strategy run, skipping occurrences already stored.

        Returns the newly stored signals. Callers use this to alert only on
        genuinely new occurrences - re-evaluating the same candle (e.g. after
        hours, on a manual re-run, or by the first run of the next session)
        must not re-alert it.
        """
        if not signals:
            return []
        now = datetime.now(IST).isoformat()
        with self._connect() as conn:
            seen = {
                (row["symbol"], row["signal_time"], row["direction"]): row["id"]
                for row in conn.execute(
                    "SELECT id, symbol, signal_time, direction FROM runs "
                    "WHERE strategy_name = ?",
                    (strategy_name,),
                )
            }
            fresh = []
            for s in signals:
                direction = str((s.metadata or {}).get("direction", ""))
                key = (s.symbol, s.timestamp.isoformat(), direction)
                if key in seen:
                    # Re-evaluating the same candle refreshes its live price and
                    # metadata (e.g. volume rank, move) but must not re-alert.
                    conn.execute(
                        "UPDATE runs SET run_at = ?, price = ?, metadata = ? "
                        "WHERE id = ?",
                        (
                            now,
                            s.price,
                            json.dumps(s.metadata, default=str),
                            seen[key],
                        ),
                    )
                    continue
                fresh.append(s)

            if fresh:
                conn.executemany(
                    "INSERT OR IGNORE INTO runs "
                    "(strategy_name, run_at, symbol, signal_time, direction, "
                    "price, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            strategy_name,
                            now,
                            s.symbol,
                            s.timestamp.isoformat(),
                            str((s.metadata or {}).get("direction", "")),
                            s.price,
                            json.dumps(s.metadata, default=str),
                        )
                        for s in fresh
                    ],
                )
        logger.info(
            "Saved %d/%d signals for strategy %s", len(fresh), len(signals), strategy_name
        )
        return fresh

    def get_latest(self, strategy_name: str) -> list[dict]:
        """Return the most recent run's signals for a strategy."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_at FROM runs WHERE strategy_name = ? ORDER BY run_at DESC LIMIT 1",
                (strategy_name,),
            ).fetchone()
            if not row:
                return []
            return self._rows_to_dicts(
                conn.execute(
                    "SELECT * FROM runs WHERE strategy_name = ? AND run_at = ? "
                    "ORDER BY signal_time",
                    (strategy_name, row["run_at"]),
                ).fetchall()
            )

    def get_history(
        self,
        strategy_name: str,
        days: int | None = None,
    ) -> list[dict]:
        """Return signals from the last ``days`` days for a strategy."""
        days = days or self.settings.retention_days
        cutoff = (datetime.now(IST) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE strategy_name = ? AND run_at >= ? "
                "ORDER BY run_at DESC, signal_time",
                (strategy_name, cutoff),
            ).fetchall()
        return self._rows_to_dicts(rows)

    def get_all_latest(self) -> dict[str, list[dict]]:
        """Return latest run signals for every strategy that has results."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT strategy_name, MAX(run_at) AS latest FROM runs GROUP BY strategy_name"
            ).fetchall()
        result: dict[str, list[dict]] = {}
        for row in rows:
            result[row["strategy_name"]] = self.get_latest(row["strategy_name"])
        return result

    def prune_old(self, days: int | None = None) -> int:
        """Delete rows older than the retention window. Returns count deleted."""
        days = days or self.settings.retention_days
        cutoff = (datetime.now(IST) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM runs WHERE run_at < ?", (cutoff,))
        deleted = cursor.rowcount
        if deleted:
            logger.info("Pruned %d rows older than %d days", deleted, days)
        return deleted

    # -- admin / observability -----------------------------------------------
    def storage_summary(self) -> dict:
        """Read-only SQLite storage summary (for admin console).

        Does not mutate any state.
        """
        from pathlib import Path

        db_path = Path(self.settings.database_path)
        size_bytes = db_path.stat().st_size if db_path.exists() else 0
        with self._connect() as conn:
            total_signals = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            strategies = (
                conn.execute("SELECT COUNT(DISTINCT strategy_name) FROM runs").fetchone()[0]
            )
            oldest = conn.execute("SELECT MIN(run_at) FROM runs").fetchone()[0]
            latest = conn.execute("SELECT MAX(run_at) FROM runs").fetchone()[0]
        return {
            "database_path": str(db_path),
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 2),
            "total_signals": total_signals,
            "strategies": strategies,
            "retention_days": self.settings.retention_days,
            "oldest_run": oldest,
            "latest_run": latest,
        }

    @staticmethod
    def _rows_to_dicts(rows) -> list[dict]:
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["metadata"] = {}
            result.append(d)
        return result
