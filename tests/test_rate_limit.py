"""Tests for the rate limiter."""

from __future__ import annotations

import time

import pytest

from app.data.rate_limit import RateLimiter


def test_allows_burst_up_to_limit() -> None:
    limiter = RateLimiter(per_second=1000, per_minute=1000, per_day=100000)
    for _ in range(100):
        limiter.acquire()


def test_throttles_when_per_second_exceeded() -> None:
    limiter = RateLimiter(per_second=2, per_minute=10000, per_day=100000)
    start = time.monotonic()
    for _ in range(4):
        limiter.acquire()
    elapsed = time.monotonic() - start
    # The 3rd and 4th calls must wait for the per-second window to advance.
    assert elapsed >= 0.5


def test_raises_when_daily_limit_reached() -> None:
    limiter = RateLimiter(per_second=1000, per_minute=1000, per_day=3)
    for _ in range(3):
        limiter.acquire()
    with pytest.raises(RuntimeError):
        limiter.acquire()
