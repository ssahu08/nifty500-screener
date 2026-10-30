"""Numeric-PIN gate for the dashboard API.

The screener is deployed on a public URL (Render) where anybody with the link
could otherwise trigger scans and consume the FYERS API quota. When
``DASHBOARD_PIN`` is set, every ``/api/*`` endpoint (except ``/api/auth/*``)
and any other data-bearing path requires a session cookie issued by
``POST /api/auth/login``.

Design notes:

* The session cookie is an HMAC of a fixed purpose string keyed by the PIN -
  stateless, stdlib only, and it invalidates automatically when the PIN
  changes. No server-side session store to lose on free-tier restarts.
* Login attempts are rate-limited per client: after ``MAX_ATTEMPTS`` failures
  within ``WINDOW_SECONDS`` the client is locked out for ``LOCKOUT_SECONDS``.
* An empty PIN disables the gate entirely (local development).
"""

from __future__ import annotations

import hashlib
import hmac
import time

from app.config import get_settings

#: HttpOnly session cookie name.
SESSION_COOKIE = "n500_session"
#: Cookie lifetime (the PWA stays logged in on the phone).
SESSION_MAX_AGE = 30 * 24 * 3600

_COOKIE_PURPOSE = b"n500-dashboard-session/v1"

#: Brute-force protection for a short numeric PIN.
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 600.0
LOCKOUT_SECONDS = 60.0

#: client key -> list of recent failure timestamps (epoch seconds).
_failures: dict[str, list[float]] = {}


def auth_enabled() -> bool:
    """True when a dashboard PIN is configured."""
    return bool(get_settings().dashboard_pin)


def session_token() -> str:
    """HMAC session value derived from the configured PIN."""
    pin = get_settings().dashboard_pin
    return hmac.new(pin.encode("utf-8"), _COOKIE_PURPOSE, hashlib.sha256).hexdigest()


def is_valid_session(cookie_value: str | None) -> bool:
    """Constant-time check of a presented session cookie (or auth disabled)."""
    if not auth_enabled():
        return True
    if not cookie_value:
        return False
    return hmac.compare_digest(cookie_value, session_token())


def _client_key(client_ip: str) -> str:
    return client_ip or "unknown"


def lockout_remaining(client_ip: str) -> float:
    """Seconds this client must still wait (0 when not locked out)."""
    failures = _failures.get(_client_key(client_ip), [])
    now = time.monotonic()
    recent = [t for t in failures if now - t <= WINDOW_SECONDS]
    if len(recent) < MAX_ATTEMPTS:
        return 0.0
    remaining = (recent[-1] + LOCKOUT_SECONDS) - now
    return max(0.0, remaining)


def verify_pin(pin: str, client_ip: str) -> tuple[bool, str]:
    """Check a submitted PIN with lockout handling.

    Returns ``(ok, message)``; the message is safe to show the client.
    """
    if not auth_enabled():
        return True, "auth disabled"
    wait = lockout_remaining(client_ip)
    if wait > 0:
        return False, f"Too many attempts - try again in {int(wait) + 1}s"

    expected = get_settings().dashboard_pin
    if hmac.compare_digest(str(pin), expected):
        _failures.pop(_client_key(client_ip), None)
        return True, "ok"

    now = time.monotonic()
    recent = [t for t in _failures.get(_client_key(client_ip), []) if now - t <= WINDOW_SECONDS]
    recent.append(now)
    _failures[_client_key(client_ip)] = recent
    if len(recent) >= MAX_ATTEMPTS:
        return False, f"Too many attempts - locked out for {int(LOCKOUT_SECONDS)}s"
    return False, "Incorrect PIN"


def clear_failures() -> None:
    """Reset the attempt tracker (used by tests)."""
    _failures.clear()
