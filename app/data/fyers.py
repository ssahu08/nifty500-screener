"""FYERS API v3 provider: authentication, quotes, history, rate limiting.

Authentication is unattended: :meth:`FyersAuthenticator.ensure_token` returns a
valid access token, obtaining one through the PIN + TOTP web login when needed
(``FYERS_FY_ID`` + ``FYERS_PIN`` + ``FYERS_TOTP_SECRET``):

1. Send the login OTP for the FY id, verify the TOTP, verify the PIN.
2. Exchange the resulting auth code for API tokens and store them locally
   (``data/fyers_tokens.json``); tokens are reused for the rest of the day.
3. Manual fallback: open the URL from :meth:`FyersAuthenticator.get_auth_url`
   once, then call :meth:`FyersAuthenticator.exchange_code` with the returned
   code. The refresh-token API is disabled by FYERS (SEBI), error ``-16``.

If you already hold a token, set ``FYERS_ACCESS_TOKEN`` in the environment.
Tokens and secrets are read only from environment / token files - never hard-coded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

from app.config import Settings
from app.core.logging import get_logger
from app.core.market import MARKET_CLOSE, MARKET_OPEN
from app.data.cache import CandleCache
from app.data.provider import MarketDataProvider, Quote, chunked, normalize_symbol
from app.data.rate_limit import RateLimiter

logger = get_logger(__name__)

#: FYERS history resolution strings for the intervals this product uses.
INTERVAL_TO_RESOLUTION = {
    "10m": "10",
    "10min": "10",
    "10-minute": "10",
    "1h": "60",
    "60m": "60",
    "1hour": "60",
    "daily": "1D",
    "day": "1D",
    "1d": "1D",
}

#: Map FYERS history resolution -> pandas offset for building higher timeframes.
RESOLUTION_TO_PANDAS = {
    "1": "1min",
    "2": "2min",
    "3": "3min",
    "5": "5min",
    "10": "10min",
    "15": "15min",
    "20": "20min",
    "30": "30min",
    "60": "60min",
    "120": "120min",
    "240": "240min",
}

IST = timezone(timedelta(hours=5, minutes=30))

#: Intraday intervals whose cached tail must be refreshed during the session.
#: A cache is only date-granular, so without this a 10m frame fetched at 09:25
#: would keep being served for the rest of the day.
INTRADAY_MINUTES = {
    "1m": 1,
    "2m": 2,
    "3m": 3,
    "5m": 5,
    "10m": 10,
    "10min": 10,
    "10-minute": 10,
    "15m": 15,
    "20m": 20,
    "30m": 30,
    "1h": 60,
    "60m": 60,
    "1hour": 60,
}


def _now_ist() -> datetime:
    """Current IST time (indirection so tests can freeze the clock)."""
    return datetime.now(IST)

#: FYERS API v3 refresh-token grant endpoint (separate from the auth-code one).
REFRESH_TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-refresh-token"
#: Web-login (vagator) endpoints used by the automated PIN + TOTP flow.
VAGATOR_URL = "https://api-t2.fyers.in/vagator/v2"
#: v3 API endpoints used by the automated PIN + TOTP flow.
AUTH_API_URL = "https://api-t1.fyers.in/api/v3"


class FyersAuthenticator:
    """Manage FYERS OAuth tokens and the daily refresh."""

    def __init__(
        self,
        settings: Settings,
        token_file: str | Path = "data/fyers_tokens.json",
        sdk=None,
    ) -> None:
        self.settings = settings
        self.token_file = Path(token_file)
        # Imported lazily so the provider works in tests without the SDK.
        self.sdk = sdk
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._stored_at: str | None = None

    # -- SDK helpers --------------------------------------------------------
    def _session(self, grant_type: str = "authorization_code", code: str = ""):
        sdk = self.sdk
        if sdk is None:
            from fyers_apiv3 import fyersModel as sdk

        session_cls = sdk.SessionModel
        session = session_cls(
            client_id=self.settings.fyers_client_id,
            secret_key=self.settings.fyers_secret_key,
            redirect_uri=self.settings.fyers_redirect_uri,
            response_type="code",
            scope=None,
            state="n500screener",
            nonce=None,
            grant_type=grant_type,
        )
        session.auth_token = code
        return session

    # -- public flow --------------------------------------------------------
    def get_auth_url(self) -> str:
        """URL to open in a browser for the one-time authorisation step."""
        return self._session().generate_authcode()

    def exchange_code(self, code: str) -> dict:
        """Swap a one-time auth ``code`` for access + refresh tokens."""
        response = self._session(code=code).generate_token()
        return self._store_tokens(response)

    def refresh(self) -> dict:
        """Refresh the access token using the stored refresh token.

        The v3 API has a dedicated ``validate-refresh-token`` endpoint that
        takes ``appIdHash``, the refresh token and the account PIN.
        """
        if not self._refresh_token and self.token_file.exists():
            self._load_tokens()
        if not self._refresh_token and self.settings.fyers_refresh_token:
            # Bootstrap value for ephemeral disks (fresh deploy lost data/).
            self._refresh_token = self.settings.fyers_refresh_token
            logger.info("Using FYERS_REFRESH_TOKEN bootstrap value from the environment")
        if not self._refresh_token:
            raise RuntimeError("No refresh token stored; run the authorisation flow first")
        payload = {
            "grant_type": "refresh_token",
            "appIdHash": hash_app_id(
                self.settings.fyers_client_id, self.settings.fyers_secret_key
            ),
            "refresh_token": self._refresh_token,
            "pin": self.settings.fyers_pin,
        }
        response = requests.post(REFRESH_TOKEN_URL, json=payload, timeout=30)
        return self._store_tokens(response.json())

    def ensure_token(self) -> str:
        """Return a valid access token, logging in unattended when needed.

        Resolution order: ``FYERS_ACCESS_TOKEN`` from the environment, then a
        token stored earlier today, then the automated PIN + TOTP login, then
        (last resort) a stale stored token.
        """
        env_token = self.settings.fyers_access_token
        if env_token:
            self._access_token = env_token
            return env_token

        if self.token_file.exists():
            try:
                self._load_tokens()
            except Exception:  # noqa: BLE001 - corrupted file should not crash
                logger.warning("Could not read stored FYERS tokens")

        if self._access_token and self._tokens_fresh():
            return self._access_token

        # Cheap daily path first: validate the 15-day refresh token for a new
        # access token (runs once per day; stored_at only counts today's IST
        # date, so the first call after midnight/06:30 refreshes).
        if not self._refresh_token and self.settings.fyers_refresh_token:
            self._refresh_token = self.settings.fyers_refresh_token
        if self._refresh_token:
            try:
                self.refresh()
                if self._access_token:
                    return self._access_token
            except Exception as exc:  # noqa: BLE001 - fall back to TOTP login
                logger.warning(
                    "FYERS refresh-token validation failed (%s); trying PIN+TOTP login", exc
                )

        if self.settings.fyers_automated_login_configured:
            try:
                self.login_with_pin_totp()
                return self._access_token  # type: ignore[return-value]
            except Exception as exc:  # noqa: BLE001 - fall back to manual path
                logger.warning("Automated FYERS login failed: %s", exc)

        if self._access_token:
            logger.warning("Using stale FYERS token stored at %s", self._stored_at)
            return self._access_token

        raise RuntimeError(
            "FYERS access token missing or expired. Set FYERS_FY_ID, FYERS_PIN "
            "and FYERS_TOTP_SECRET for unattended login, or open this URL to "
            "authorise manually: " + self.get_auth_url()
        )

    def login_with_pin_totp(self) -> dict:
        """Log in unattended using FY ID, PIN and TOTP (no browser needed).

        Mirrors the web login: send OTP, verify TOTP, verify PIN, then
        exchange the resulting auth code for API tokens.
        """
        settings = self.settings
        if not settings.fyers_automated_login_configured:
            raise RuntimeError(
                "Automated login requires FYERS_FY_ID, FYERS_PIN and "
                "FYERS_TOTP_SECRET in addition to the client credentials."
            )

        request_key = self._vagator_post(
            "/send_login_otp_v2",
            {"fy_id": _encode_b64(settings.fyers_fy_id), "app_id": "2"},
        )["request_key"]

        # TOTP codes rotate every 30 s; avoid one that expires mid-flight.
        if datetime.now(IST).second % 30 > 27:
            time.sleep(5)
        otp = self._generate_totp(settings.fyers_totp_secret)
        request_key = self._vagator_post(
            "/verify_otp", {"request_key": request_key, "otp": otp}
        )["request_key"]

        session = requests.Session()
        pin_response = session.post(
            f"{VAGATOR_URL}/verify_pin_v2",
            json={
                "request_key": request_key,
                "identity_type": "pin",
                "identifier": _encode_b64(settings.fyers_pin),
            },
            timeout=30,
        ).json()
        session_token = _nested_token(pin_response, "PIN verification")

        base_id, _, app_type = settings.fyers_client_id.partition("-")
        auth_response = session.post(
            f"{AUTH_API_URL}/token",
            headers={"Authorization": f"Bearer {session_token}"},
            json={
                "fyers_id": settings.fyers_fy_id,
                "app_id": base_id,
                "redirect_uri": settings.fyers_redirect_uri,
                "appType": app_type or "100",
                "code_challenge": "",
                "state": "None",
                "scope": "",
                "nonce": "",
                "response_type": "code",
                "create_cookie": True,
            },
            timeout=30,
        ).json()
        auth_code = _extract_auth_code(auth_response)
        logger.info("Automated FYERS login obtained an auth code")
        return self.exchange_code(auth_code)

    def _vagator_post(self, path: str, payload: dict) -> dict:
        response = requests.post(
            f"{VAGATOR_URL}{path}", json=payload, timeout=30
        ).json()
        if not isinstance(response, dict) or "request_key" not in response:
            raise RuntimeError(f"Unexpected FYERS response from {path}: {response!r}")
        return response

    @staticmethod
    def _generate_totp(secret: str, at: datetime | None = None) -> str:
        """RFC 6238 TOTP (SHA-1, 30 s step, 6 digits) for a base32 secret."""
        key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
        counter = int((at or datetime.now(UTC)).timestamp()) // 30
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        return f"{code % 1_000_000:06d}"

    def _tokens_fresh(self) -> bool:
        """True when the stored token was captured earlier today (IST)."""
        if not self._stored_at:
            return False
        try:
            stored = datetime.fromisoformat(self._stored_at)
        except ValueError:
            return False
        return stored.astimezone(IST).date() == datetime.now(IST).date()

    # -- storage ------------------------------------------------------------
    def _store_tokens(self, response: dict) -> dict:
        if not isinstance(response, dict) or "access_token" not in response:
            raise RuntimeError(f"Unexpected FYERS token response: {response!r}")
        self._access_token = response["access_token"]
        self._refresh_token = response.get("refresh_token", self._refresh_token)
        expires_in = response.get("expires_in")
        record = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "stored_at": datetime.now(IST).isoformat(),
            "expires_in": expires_in,
        }
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(record), encoding="utf-8")
        logger.info("FYERS tokens stored (expires_in=%s)", expires_in)
        return record

    def _load_tokens(self) -> None:
        record = json.loads(self.token_file.read_text(encoding="utf-8"))
        self._access_token = record.get("access_token")
        self._refresh_token = record.get("refresh_token")
        self._stored_at = record.get("stored_at")


class FyersProvider(MarketDataProvider):
    """Market-data provider backed by the FYERS API v3."""

    QUOTE_BATCH_SIZE = 50

    def __init__(
        self,
        settings: Settings,
        authenticator: FyersAuthenticator | None = None,
        rate_limiter: RateLimiter | None = None,
        cache: CandleCache | None = None,
        fyers_model=None,
    ) -> None:
        self.settings = settings
        self.authenticator = authenticator or FyersAuthenticator(settings)
        self.rate_limiter = rate_limiter or RateLimiter()
        self.cache = cache or CandleCache("data/cache")
        self._fyers = fyers_model

    # -- connection ---------------------------------------------------------
    def _model(self):
        if self._fyers is None:
            from fyers_apiv3 import fyersModel as sdk

            token = self.authenticator.ensure_token()
            self._fyers = sdk.FyersModel(
                is_async=False,
                log_path="",
                client_id=self.settings.fyers_client_id,
                token=token,
                log_level="ERROR",
            )
        return self._fyers

    def _call(self, func, data):
        """Run an FYERS call through the rate limiter with retry/backoff.

        FYERS reports rate-limit breaches both as raised errors and as
        ``{"s": "error", "code": -360}`` bodies inside HTTP 200 responses;
        both are retried with backoff.
        """
        last_error = None
        for attempt in range(4):
            self.rate_limiter.acquire()
            try:
                result = func(data)
            except Exception as exc:  # noqa: BLE001 - SDK raises varied errors
                last_error = exc
                message = str(exc)
                retry_after = getattr(exc, "retry_after", None)
                if retry_after:
                    time.sleep(float(retry_after))
                elif "429" in message or "Too Many" in message:
                    time.sleep(min(2**attempt, 30))
                else:
                    time.sleep(min(2**attempt, 5))
                logger.warning("FYERS call failed (attempt %s): %s", attempt + 1, message)
                continue
            if self._is_rate_limited(result):
                message = str(result.get("message") or "rate limit")
                last_error = RuntimeError(f"FYERS rate limit response: {message}")
                time.sleep(min(2**attempt, 30))
                logger.warning("FYERS rate limit (attempt %s): %s", attempt + 1, message)
                continue
            return result
        raise last_error

    @staticmethod
    def _is_rate_limited(response) -> bool:
        if not isinstance(response, dict):
            return False
        code = str(response.get("code", ""))
        message = str(response.get("message", "")).lower()
        return (
            code in {"-429", "429", "-360", "360"}
            or "rate limit" in message
            or "requests/second" in message
        )

    # -- interface ----------------------------------------------------------
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch realtime quotes in batches of up to 50 symbols."""
        normalized = [normalize_symbol(s) for s in symbols]
        result: dict[str, Quote] = {}
        for batch in chunked(normalized, self.QUOTE_BATCH_SIZE):
            response = self._call(
                lambda data: self._model().quotes(data), {"symbols": ",".join(batch)}
            )
            entries = self._parse_quotes(response)
            result.update(entries)
        return result

    def get_candles(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles, using the cache when it covers the range.

        The on-disk cache is date-granular. For intraday intervals during the
        session the current day's bars can change (and an early fetch can
        capture a partial bar), so the tail is refetched on every in-session
        call and merged over the cache. Without this a 10m cache written at
        09:25 would freeze for the rest of the day.
        """
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()
        norm = normalize_symbol(symbol)
        resolution = INTERVAL_TO_RESOLUTION.get(interval, interval)
        key = f"{norm}|{resolution}"

        cached = self.cache.get(key, start, end)
        now = _now_ist()
        refresh_tail = (
            interval in INTRADAY_MINUTES
            and end >= now.date()
            and MARKET_OPEN <= now.time() <= MARKET_CLOSE
        )

        if cached is not None and not refresh_tail:
            return cached

        fetch_start = start
        if refresh_tail and cached is not None and not cached.empty:
            # Only the current day's tail can change; history is already on disk.
            fetch_start = cached.index[-1].date()

        request = {
            "symbol": norm,
            "resolution": resolution,
            "date_format": 1,
            "range_from": fetch_start.isoformat(),
            "range_to": end.isoformat(),
            "cont_flag": 1,
        }
        response = self._call(lambda data: self._model().history(data), request)
        df = self._parse_history(response, resolution)
        if df is not None and not df.empty:
            self.cache.put(key, df)

        if refresh_tail:
            merged = self.cache.get(key, start, end)
            if merged is not None:
                return merged
            if cached is not None:
                return cached

        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df

    def health_check(self) -> bool:
        """Return True when FYERS is reachable and the token is accepted."""
        try:
            self._model().get_profile()
            return True
        except Exception as exc:  # noqa: BLE001 - network/auth failures are expected
            logger.warning("FYERS health check failed: %s", exc)
            return False

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _parse_quotes(response) -> dict[str, Quote]:
        parsed: dict[str, Quote] = {}
        if not isinstance(response, dict):
            return parsed
        items = response.get("d") or []
        for item in items:
            values = item.get("v", item)
            symbol = values.get("symbol")
            # FYERS v3 names the last price ``lp``; older payloads use ``ltp``.
            ltp = values.get("ltp", values.get("lp"))
            if symbol is None or ltp is None:
                continue
            parsed[symbol] = Quote(
                symbol=symbol,
                ltp=float(ltp),
                open=FyersProvider._to_float(values.get("open_price")),
                high=FyersProvider._to_float(values.get("high_price")),
                low=FyersProvider._to_float(values.get("low_price")),
                prev_close=FyersProvider._to_float(values.get("prev_close_price")),
                volume=FyersProvider._to_float(values.get("volume")),
            )
        return parsed

    @staticmethod
    def _parse_history(response, resolution: str) -> pd.DataFrame | None:
        if not isinstance(response, dict) or response.get("s") not in {"ok", "success"}:
            if FyersProvider._is_rate_limited(response):
                raise RuntimeError("FYERS rate limit exceeded (429)")
            logger.warning("FYERS history response not ok: %r", response)
            return None
        candles = response.get("candles") or []
        if not candles:
            return None

        records = []
        for candle in candles:
            ts, o, h, low, c, v = candle[:6]
            records.append(
                {
                    "open": float(o),
                    "high": float(h),
                    "low": float(low),
                    "close": float(c),
                    "volume": float(v),
                    "time": datetime.fromtimestamp(int(ts), tz=IST),
                }
            )
        if not records:
            return None
        df = pd.DataFrame(records).set_index("time").sort_index()
        df = df[["open", "high", "low", "close", "volume"]]
        # FYERS can repeat the current day's candles in a single response.
        duplicates = int(df.index.duplicated().sum())
        if duplicates:
            logger.info("FYERS history contained %d duplicated candles; keeping first", duplicates)
            df = df[~df.index.duplicated(keep="first")]
        return df

    @staticmethod
    def _to_float(value) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None


def resolution_to_pandas_freq(resolution: str) -> str:
    """Map a FYERS history resolution to a pandas offset alias."""
    return RESOLUTION_TO_PANDAS.get(resolution, "1D")


def hash_app_id(client_id: str, secret_key: str) -> str:
    """FYERS appIdHash: SHA-256 of ``client_id:secret_key``."""
    return hashlib.sha256(f"{client_id}:{secret_key}".encode()).hexdigest()


def _encode_b64(value: str) -> str:
    """Base64-encode a login identifier (the vagator API expects this)."""
    return base64.b64encode(str(value).encode("ascii")).decode("ascii")


def _nested_token(response: dict, step: str) -> str:
    """Pull an access token out of a nested FYERS login response."""
    data = response.get("data") if isinstance(response, dict) else None
    token = (data or {}).get("access_token") or (
        response.get("access_token") if isinstance(response, dict) else None
    )
    if not token:
        raise RuntimeError(f"FYERS {step} failed: {response!r}")
    return token


def _extract_auth_code(response: dict) -> str:
    """Find the auth code in the ``/api/v3/token`` response."""
    if isinstance(response, dict) and response.get("auth_code"):
        return str(response["auth_code"])
    url = ""
    if isinstance(response, dict):
        url = str(response.get("Url") or response.get("url") or "")
    if url:
        codes = parse_qs(urlparse(url).query).get("auth_code")
        if codes:
            return codes[0]
    raise RuntimeError(f"Could not find an auth code in FYERS response: {response!r}")
