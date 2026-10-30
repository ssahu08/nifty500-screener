"""Kite Connect (Zerodha) authenticator + instrument token lookup.

Provides instrument_token for NSE symbols so dashboard chart links open
directly on Kite (``/ciq/NSE/infy/2953217``) instead of falling back to
the /markets search page.

Authentication flow (once per day):

1. Open the URL from :meth:`get_login_url` in a browser.
2. Log in with Zerodha credentials + TOTP.
3. You're redirected to ``KITE_REDIRECT_URI`` with ``?request_token=XXX``.
4. Call :meth:`exchange_code(request_token)` to get an access token, cached in
   ``data/kite_tokens.json`` for the rest of the day.
5. :meth:`fetch_instruments()` downloads the NSE instrument master and maps
   ``tradingsymbol -> instrument_token``.

Credentials are read from environment / token files only - never hard-coded.
Set these in ``.env``:

    KITE_API_KEY=...
    KITE_API_SECRET=...
    KITE_FY_ID=...
    KITE_PIN=...
    KITE_TOTP_SECRET=...
    KITE_REDIRECT_URI=...

All secrets live in .env (gitignored) and are never logged.
"""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path
from urllib.parse import quote

import requests

from app.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Kite Connect base URLs.
KITE_LOGIN_URL = "https://kite.trade/connect/login"
KITE_SESSION_URL = "https://api.kite.trade/session/token"
KITE_INSTRUMENTS_URL = "https://api.kite.trade/instruments/NSE"

#: Token cache file (reused for the rest of the day).
TOKEN_FILE = Path("data/kite_tokens.json")
#: Instrument cache file (refreshed daily).
INSTRUMENTS_FILE = Path("data/kite_instruments.json")
INSTRUMENTS_TTL_SECONDS = 24 * 60 * 60  # 24 hours


class KiteAuthenticator:
    """Manage Kite Connect OAuth tokens and instrument-token lookup."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._access_token: str | None = None
        self._stored_at: str | None = None
        self._instruments: dict[str, int] | None = None
        self._instruments_fetched_at: float | None = None

    # -- settings helpers --------------------------------------------------

    @property
    def _api_key(self) -> str | None:
        return self.settings.kite_api_key

    @property
    def _api_secret(self) -> str | None:
        return self.settings.kite_api_secret

    def _configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    # -- token persistence -------------------------------------------------

    def _load_tokens(self) -> None:
        if not TOKEN_FILE.exists():
            return
        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)
            self._access_token = data.get("access_token")
            self._stored_at = data.get("stored_at")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read stored Kite tokens: %s", exc)

    def _save_tokens(self, access_token: str) -> None:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": access_token,
            "stored_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f, indent=2)
        self._access_token = access_token
        self._stored_at = data["stored_at"]

    # -- public API --------------------------------------------------------

    def get_login_url(self) -> str:
        """URL to open in a browser for the one-time authorisation step."""
        if not self._configured():
            raise RuntimeError("KITE_API_KEY / KITE_API_SECRET not configured")
        return (
            f"{KITE_LOGIN_URL}?api_key={self._api_key}"
            f"&v=3&redirect_uri={quote(self.settings.kite_redirect_uri, safe='')}"
        )

    def exchange_code(self, request_token: str) -> dict:
        """Swap a one-time ``request_token`` for an access token."""
        if not self._configured():
            raise RuntimeError("KITE_API_KEY / KITE_API_SECRET not configured")
        payload = {
            "api_key": self._api_key,
            "api_secret": self._api_secret,
            "request_token": request_token,
        }
        try:
            response = requests.post(KITE_SESSION_URL, data=payload, timeout=30)
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"Kite rejected the request_token (HTTP {exc.response.status_code}). "
                "It may be expired or already used — get a fresh one from "
                "/api/kite/login-url."
            ) from exc
        data = response.json()
        access_token = data.get("data", {}).get("access_token")
        if not access_token:
            raise RuntimeError(f"No access token in Kite response: {data}")
        self._save_tokens(access_token)
        return data

    def ensure_token(self) -> str | None:
        """Return a valid access token, or None if not yet authorised.

        Resolution order: environment variable, stored token from today.
        If neither is available, returns None and logs the login URL.
        """
        env_token = self.settings.kite_access_token
        if env_token:
            self._access_token = env_token
            return env_token

        self._load_tokens()
        if self._access_token:
            # Kite access tokens expire at end of day; check stored_at date.
            today = time.strftime("%Y-%m-%d")
            if self._stored_at and self._stored_at.startswith(today):
                return self._access_token

        logger.info(
            "Kite access token missing. Open this URL in a browser, log in, "
            "then call /api/kite/login with the request_token:\n%s",
            self.get_login_url(),
        )
        return None

    # -- instruments -------------------------------------------------------

    def fetch_instruments(self, access_token: str | None = None) -> dict[str, int]:
        """Download the NSE instrument master and return {symbol: token}.

        Cached for 24 hours in ``data/kite_instruments.json``.
        """
        token = access_token or self._access_token
        if not token:
            raise RuntimeError("No Kite access token available")

        now = time.time()
        if (
            self._instruments is not None
            and self._instruments_fetched_at is not None
            and now - self._instruments_fetched_at < INSTRUMENTS_TTL_SECONDS
        ):
            return self._instruments

        # Check on-disk cache first.
        if INSTRUMENTS_FILE.exists():
            try:
                mtime = INSTRUMENTS_FILE.stat().st_mtime
                if now - mtime < INSTRUMENTS_TTL_SECONDS:
                    with open(INSTRUMENTS_FILE) as f:
                        self._instruments = json.load(f)
                    self._instruments_fetched_at = mtime
                    return self._instruments
            except (json.JSONDecodeError, OSError):
                pass

        headers = {"Authorization": f"token {self._api_key}:{token}"}
        response = requests.get(KITE_INSTRUMENTS_URL, headers=headers, timeout=60)
        response.raise_for_status()

        instruments: dict[str, int] = {}
        reader = csv.DictReader(io.StringIO(response.text))
        for row in reader:
            symbol = (row.get("tradingsymbol") or "").strip()
            token_str = (row.get("instrument_token") or "").strip()
            if symbol and token_str:
                instruments[symbol] = int(token_str)

        INSTRUMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(INSTRUMENTS_FILE, "w") as f:
            json.dump(instruments, f)
        self._instruments = instruments
        self._instruments_fetched_at = now
        logger.info("Fetched %d NSE instruments from Kite", len(instruments))
        return instruments

    def get_token(self, symbol: str, access_token: str | None = None) -> int | None:
        """Return the Kite instrument token for an NSE symbol, or None."""
        instruments = self.fetch_instruments(access_token)
        return instruments.get(symbol)
