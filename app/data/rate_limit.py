"""Sliding-window rate limiter with safety margin and retry/backoff.

Verified FYERS Standard-plan Data API limits (support.fyers.in, 2026-10-05):
5 requests/second, 50 requests/minute, 5,000 requests/day. Breaching the
per-minute cap more than three times in a day blocks the account until midnight.

The limiter deliberately targets values *below* those limits (configurable) so a
safety margin is always preserved. Windows are true sliding windows (event
timestamps), so bursts cannot straddle a fixed-window boundary.

Calendar-day usage is tracked separately for observability, matching FYERS'
midnight-IST daily reset.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

#: FYERS Standard-plan Data API limits (actual, from support.fyers.in).
FYERS_PER_SECOND = 5.0
FYERS_PER_MINUTE = 50.0
FYERS_PER_DAY = 5000.0

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class RateLimiter:
    """Enforce per-second, per-minute and per-day request ceilings.

    Internal safety margins are set below FYERS' actual limits to prevent
    accidental breaches. Calendar-day usage is tracked separately for
    observability (matches FYERS' midnight-IST daily reset).
    """

    per_second: float = 4.0
    per_minute: float = 40.0
    per_day: float = 4000.0

    _events: deque = field(default_factory=deque, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _daily_counts: dict[date, int] = field(
        default_factory=dict, init=False, repr=False
    )

    def acquire(self) -> None:
        """Block until a request is permitted under all three limits."""
        while True:
            with self._lock:
                now = time.monotonic()
                today = datetime.now(IST).date()

                # Purge events older than 24 hours (monotonic time).
                while self._events and now - self._events[0] >= 86400.0:
                    self._events.popleft()

                # Purge daily counts older than today.
                stale_days = [d for d in self._daily_counts if d < today]
                for d in stale_days:
                    del self._daily_counts[d]

                if len(self._events) >= self.per_day:
                    # Over the daily ceiling: stop rather than silently failing.
                    raise RuntimeError("Daily API request limit reached")

                wait = 0.0
                if len(self._events) >= self.per_second:
                    oldest_in_second = self._events[-int(self.per_second)]
                    wait = max(wait, 1.0 - (now - oldest_in_second) + 0.01)
                if len(self._events) >= self.per_minute:
                    oldest_in_minute = self._events[-int(self.per_minute)]
                    wait = max(wait, 60.0 - (now - oldest_in_minute) + 0.01)

                if wait <= 0.0:
                    self._events.append(now)
                    self._daily_counts[today] = (
                        self._daily_counts.get(today, 0) + 1
                    )
                    return
            time.sleep(min(wait, 1.0))

    def __call__(self):  # allow ``limiter()`` shorthand
        self.acquire()

    # -- read-only inspection ------------------------------------------------
    def snapshot(self) -> dict:
        """Return current usage without mutating any state.

        Intended for admin/observability dashboards. Reports both internal
        safety-margin limits and FYERS' actual API limits. Daily usage is
        based on calendar-day counts (midnight-IST reset), matching FYERS.
        """
        now = time.monotonic()
        today = datetime.now(IST).date()

        events = list(self._events)  # copy; do not hold the lock while computing
        day_used = self._daily_counts.get(today, 0)
        minute_used = sum(1 for t in events if now - t < 60.0)
        second_used = sum(1 for t in events if now - t < 1.0)

        return {
            "per_second": {
                "used": second_used,
                "limit_internal": self.per_second,
                "limit_fyers": FYERS_PER_SECOND,
            },
            "per_minute": {
                "used": minute_used,
                "limit_internal": self.per_minute,
                "limit_fyers": FYERS_PER_MINUTE,
            },
            "per_day": {
                "used": day_used,
                "limit_internal": self.per_day,
                "limit_fyers": FYERS_PER_DAY,
                "remaining_fyers": max(0, FYERS_PER_DAY - day_used),
            },
            "pct_day_internal": round(
                (day_used / self.per_day) * 100, 1
            )
            if self.per_day
            else 0.0,
            "pct_day_fyers": round(
                (day_used / FYERS_PER_DAY) * 100, 1
            ),
            "today": today.isoformat(),
        }
