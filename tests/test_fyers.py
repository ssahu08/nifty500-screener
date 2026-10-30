"""Tests for the FYERS provider (SDK mocked)."""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime, timedelta, timezone

import pandas as pd
import pytest

from app.config import Settings
from app.data.cache import CandleCache
from app.data.fyers import (
    INTERVAL_TO_RESOLUTION,
    FyersAuthenticator,
    FyersProvider,
    hash_app_id,
)
from app.data.provider import Quote, normalize_symbol

IST = timezone(timedelta(hours=5, minutes=30))


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        fyers_client_id="CID",
        fyers_secret_key="SECRET",
        fyers_redirect_uri="http://127.0.0.1:8000/callback",
        fyers_access_token="TEST-TOKEN",
        database_path="data/screener.db",
    )


def _settings_no_token() -> Settings:
    settings = _settings()
    settings.fyers_access_token = ""
    return settings


class _FakeModel:
    """A stand-in for an authenticated ``fyersModel.FyersModel`` instance."""

    def __init__(self) -> None:
        self.history_calls: list[dict] = []
        self.quotes_calls: list[dict] = []
        self.profile_calls = 0
        self.candles_payload: dict = {"s": "ok", "candles": []}
        self.quotes_payload: dict = {"s": "ok", "d": []}

    def history(self, data):
        self.history_calls.append(data)
        return self.candles_payload

    def quotes(self, data):
        self.quotes_calls.append(data)
        return self.quotes_payload

    def get_profile(self):
        self.profile_calls += 1
        return {"s": "ok"}


class _FakeSDK:
    """Stand-in for the ``fyers_apiv3.fyersModel`` module (SessionModel only)."""

    def SessionModel(self, **kwargs):

        class _Session:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def generate_authcode(self):
                return "https://example/auth?client_id=CID"

            def generate_token(self):
                return {
                    "access_token": "AT-123",
                    "refresh_token": "RT-456",
                    "expires_in": 86400,
                }

        return _Session(**kwargs)


def _candles_payload() -> dict:
    ts1 = int(datetime(2026, 1, 2, 9, 30, tzinfo=IST).timestamp())
    ts2 = int(datetime(2026, 1, 2, 9, 40, tzinfo=IST).timestamp())
    return {
        "s": "ok",
        "candles": [
            [ts1, 100.0, 105.0, 99.0, 103.0, 500.0],
            [ts2, 103.0, 106.0, 102.0, 104.0, 300.0],
        ],
    }


def test_normalize_symbol() -> None:
    assert normalize_symbol("sbin") == "NSE:SBIN-EQ"
    assert normalize_symbol("NSE:RELIANCE-EQ") == "NSE:RELIANCE-EQ"


def test_hash_app_id() -> None:
    import hashlib

    assert hash_app_id("CID", "SECRET") == hashlib.sha256(b"CID:SECRET").hexdigest()


def test_interval_resolution_map() -> None:
    assert INTERVAL_TO_RESOLUTION["10m"] == "10"
    assert INTERVAL_TO_RESOLUTION["1h"] == "60"
    assert INTERVAL_TO_RESOLUTION["daily"] == "1D"


def test_parse_history_builds_dataframe() -> None:
    df = FyersProvider._parse_history(_candles_payload(), "10")
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.index[0].date() == date(2026, 1, 2)


def test_parse_history_rate_limit_raises() -> None:
    payload = {"s": "error", "code": "-429", "message": "rate limit exceeded"}
    with pytest.raises(RuntimeError):
        FyersProvider._parse_history(payload, "10")


def test_parse_history_error_returns_none() -> None:
    assert FyersProvider._parse_history({"s": "error", "code": "1"}, "10") is None


class _NoopLimiter:
    def acquire(self) -> None:
        pass


def test_call_retries_rate_limit_responses(tmp_path, monkeypatch) -> None:
    calls = {"count": 0}

    def func(data):
        calls["count"] += 1
        if calls["count"] < 3:
            return {
                "s": "error",
                "code": -360,
                "message": "You've reached the Standard plan limit of 5 requests/second.",
            }
        return {"s": "ok", "d": []}

    monkeypatch.setattr("app.data.fyers.time.sleep", lambda seconds: None)
    provider = FyersProvider(
        _settings(), authenticator=None, cache=CandleCache(tmp_path), fyers_model=_FakeModel()
    )
    provider.rate_limiter = _NoopLimiter()

    result = provider._call(func, {})
    assert result["s"] == "ok"
    assert calls["count"] == 3


def test_is_rate_limited_recognises_v3_codes() -> None:
    assert FyersProvider._is_rate_limited({"s": "error", "code": -360})
    assert FyersProvider._is_rate_limited({"s": "error", "code": -429})
    assert FyersProvider._is_rate_limited(
        {"s": "error", "message": "You've reached the Standard plan limit of 5 requests/second."}
    )
    assert not FyersProvider._is_rate_limited({"s": "ok", "code": 200})


def test_parse_history_dedupes_repeated_candles() -> None:
    payload = _candles_payload()
    payload["candles"] = payload["candles"] + list(payload["candles"])
    df = FyersProvider._parse_history(payload, "10")
    assert len(df) == 2
    assert not df.index.duplicated().any()


def test_parse_quotes_maps_fields() -> None:
    response = {
        "s": "ok",
        "d": [
            {
                "v": {
                    "symbol": "NSE:SBIN-EQ",
                    "ltp": 123.4,
                    "open_price": 120.0,
                    "high_price": 125.0,
                    "low_price": 119.0,
                    "prev_close_price": 118.0,
                    "volume": 10000.0,
                }
            }
        ],
    }
    quotes = FyersProvider._parse_quotes(response)
    quote = quotes["NSE:SBIN-EQ"]
    assert isinstance(quote, Quote)
    assert quote.ltp == 123.4
    assert quote.prev_close == 118.0
    assert quote.change_pct is not None


def test_parse_quotes_accepts_v3_lp_field() -> None:
    response = {
        "s": "ok",
        "d": [
            {
                "n": "NSE:SBIN-EQ",
                "s": "ok",
                "v": {
                    "symbol": "NSE:SBIN-EQ",
                    "lp": 123.4,
                    "open_price": 120.0,
                    "high_price": 125.0,
                    "low_price": 119.0,
                    "prev_close_price": 118.0,
                    "volume": 10000.0,
                },
            }
        ],
    }
    quotes = FyersProvider._parse_quotes(response)
    quote = quotes["NSE:SBIN-EQ"]
    assert quote.ltp == 123.4
    assert quote.high == 125.0 and quote.low == 119.0


def test_get_candles_uses_cache_when_range_covered(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    idx = pd.to_datetime(
        [datetime(2026, 1, 2, 9, 30, tzinfo=IST), datetime(2026, 1, 3, 9, 30, tzinfo=IST)]
    )
    cache.put(
        "NSE:SBIN-EQ|10",
        pd.DataFrame(
            {
                "open": [100.0, 100.0],
                "high": [101.0, 101.0],
                "low": [99.0, 99.0],
                "close": [100.5, 100.5],
                "volume": [1.0, 1.0],
            },
            index=idx,
        ),
    )

    model = _FakeModel()
    provider = FyersProvider(
        _settings(), authenticator=None, cache=cache, fyers_model=model
    )
    df = provider.get_candles("SBIN", "10m", date(2026, 1, 2), date(2026, 1, 3))
    assert len(df) == 2
    # Cache hit -> no history call made.
    assert model.history_calls == []


def test_get_candles_fetches_when_cache_miss(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    model = _FakeModel()
    model.candles_payload = _candles_payload()
    provider = FyersProvider(
        _settings(), authenticator=None, cache=cache, fyers_model=model
    )
    df = provider.get_candles("SBIN", "10m", date(2026, 1, 2), date(2026, 1, 2))
    assert len(model.history_calls) == 1
    assert model.history_calls[0]["symbol"] == "NSE:SBIN-EQ"
    assert model.history_calls[0]["resolution"] == "10"
    assert len(df) == 2


def test_get_candles_normalises_datetime_range(tmp_path) -> None:
    model = _FakeModel()
    model.candles_payload = _candles_payload()
    provider = FyersProvider(
        _settings(), authenticator=None, cache=CandleCache(tmp_path), fyers_model=model
    )
    provider.get_candles(
        "SBIN",
        "10m",
        datetime(2026, 1, 2, 15, 30, tzinfo=IST),
        datetime(2026, 1, 3, 9, 15, tzinfo=IST),
    )
    request = model.history_calls[0]
    assert request["range_from"] == "2026-01-02"
    assert request["range_to"] == "2026-01-03"


def test_get_quotes_batches_by_50(tmp_path) -> None:
    model = _FakeModel()
    provider = FyersProvider(
        _settings(), authenticator=None, cache=CandleCache(tmp_path), fyers_model=model
    )
    symbols = [f"SYM{i}" for i in range(120)]
    provider.get_quotes(symbols)
    assert len(model.quotes_calls) == 3  # 50 + 50 + 20
    assert model.quotes_calls[0]["symbols"].count(",") == 49


def test_health_check_true_when_profile_ok(tmp_path) -> None:
    model = _FakeModel()
    provider = FyersProvider(
        _settings(), authenticator=None, cache=CandleCache(tmp_path), fyers_model=model
    )
    assert provider.health_check() is True


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def test_authenticator_token_roundtrip(tmp_path, monkeypatch) -> None:
    sdk = _FakeSDK()
    token_file = tmp_path / "tokens.json"
    auth = FyersAuthenticator(_settings_no_token(), token_file=token_file, sdk=sdk)

    record = auth.exchange_code("CODE-ABC")
    assert record["access_token"] == "AT-123"
    assert record["refresh_token"] == "RT-456"
    assert token_file.exists()

    # Reload from disk (no env token) and ensure refresh returns the token.
    monkeypatch.setattr(
        "app.data.fyers.requests.post",
        lambda *a, **k: _FakeResponse({"access_token": "AT-123", "refresh_token": "RT-456"}),
    )
    auth2 = FyersAuthenticator(_settings_no_token(), token_file=token_file, sdk=sdk)
    assert auth2.ensure_token() == "AT-123"


def test_refresh_posts_v3_payload(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResponse({"access_token": "AT-NEW", "refresh_token": "RT-NEW"})

    monkeypatch.setattr("app.data.fyers.requests.post", fake_post)
    auth = FyersAuthenticator(
        _settings_no_token(), token_file=tmp_path / "tokens.json", sdk=_FakeSDK()
    )
    auth.exchange_code("CODE-ABC")

    record = auth.refresh()
    assert record["access_token"] == "AT-NEW"
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/validate-refresh-token")
    body = calls[0]["json"]
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "RT-456"
    assert body["appIdHash"] == hash_app_id("CID", "SECRET")
    assert "pin" in body


def test_ensure_token_falls_back_to_stored_token(tmp_path, monkeypatch) -> None:
    sdk = _FakeSDK()
    token_file = tmp_path / "tokens.json"
    auth = FyersAuthenticator(_settings_no_token(), token_file=token_file, sdk=sdk)
    auth.exchange_code("CODE-ABC")

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.data.fyers.requests.post", boom)
    auth2 = FyersAuthenticator(_settings_no_token(), token_file=token_file, sdk=sdk)
    assert auth2.ensure_token() == "AT-123"


def test_refresh_requires_stored_refresh_token(tmp_path) -> None:
    auth = FyersAuthenticator(
        _settings_no_token(), token_file=tmp_path / "missing.json", sdk=_FakeSDK()
    )
    with pytest.raises(RuntimeError, match="No refresh token"):
        auth.refresh()


def test_refresh_uses_env_bootstrap_when_cache_missing(tmp_path, monkeypatch) -> None:
    """FYERS_REFRESH_TOKEN recovers instantly after an ephemeral-disk wipe."""
    calls: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResponse({"access_token": "AT-ENV", "refresh_token": "RT-ENV"})

    monkeypatch.setattr("app.data.fyers.requests.post", fake_post)
    settings = _settings_no_token()
    settings.fyers_refresh_token = "ENV-BOOT-RT"
    auth = FyersAuthenticator(settings, token_file=tmp_path / "missing.json", sdk=_FakeSDK())

    record = auth.refresh()
    assert record["access_token"] == "AT-ENV"
    assert calls[0]["json"]["refresh_token"] == "ENV-BOOT-RT"
    # Rotated token is persisted so the env value can go stale harmlessly.
    assert auth.token_file.exists()


def test_ensure_token_refreshes_stale_cache_before_totp(tmp_path, monkeypatch) -> None:
    """A stale cached token goes through validate-refresh-token, not TOTP login."""
    import json as _json

    sdk = _FakeSDK()
    token_file = tmp_path / "tokens.json"
    auth = FyersAuthenticator(_settings_no_token(), token_file=token_file, sdk=sdk)
    auth.exchange_code("CODE-ABC")

    record = _json.loads(token_file.read_text(encoding="utf-8"))
    record["stored_at"] = "2020-01-01T00:00:00+05:30"  # stale: not today
    token_file.write_text(_json.dumps(record), encoding="utf-8")

    monkeypatch.setattr(
        "app.data.fyers.requests.post",
        lambda *a, **k: _FakeResponse({"access_token": "AT-FRESH", "refresh_token": "RT-FRESH"}),
    )
    auth2 = FyersAuthenticator(_settings_no_token(), token_file=token_file, sdk=sdk)
    assert auth2.ensure_token() == "AT-FRESH"


def test_ensure_token_uses_env_token() -> None:
    sdk = _FakeSDK()
    auth = FyersAuthenticator(_settings(), token_file="/nonexistent/x.json", sdk=sdk)
    assert auth.ensure_token() == "TEST-TOKEN"


def test_ensure_token_raises_without_token(tmp_path) -> None:
    sdk = _FakeSDK()
    auth = FyersAuthenticator(
        _settings_no_token(), token_file=tmp_path / "missing.json", sdk=sdk
    )
    with pytest.raises(RuntimeError):
        auth.ensure_token()


# --- automated login (PIN + TOTP) -------------------------------------------

TOTP_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 seed


def _settings_auto() -> Settings:
    settings = _settings_no_token()
    settings.fyers_fy_id = "FAK123"
    settings.fyers_pin = "1234"
    settings.fyers_totp_secret = TOTP_SECRET_B32
    return settings


def test_generate_totp_matches_rfc6238_vectors() -> None:
    # RFC 6238 Table 1 (SHA-1, 8 digits) truncated to the 6 digits FYERS uses.
    vectors = {
        59: "287082",
        1111111109: "081804",
        1111111111: "050471",
        1234567890: "005924",
        2000000000: "279037",
    }
    for epoch, expected in vectors.items():
        at = datetime.fromtimestamp(epoch, tz=UTC)
        assert FyersAuthenticator._generate_totp(TOTP_SECRET_B32, at=at) == expected


class _FakePostResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeRequests:
    """Stand-in for the ``requests`` module used by the login flow."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        if url.endswith("/send_login_otp_v2"):
            return _FakePostResponse({"request_key": "RK1"})
        return _FakePostResponse({"request_key": "RK2"})

    def Session(self):  # noqa: N802 - mirrors the requests API
        outer = self

        class _Session:
            def post(self, url, headers=None, json=None, timeout=None):
                outer.calls.append({"url": url, "json": json, "headers": headers})
                if url.endswith("/verify_pin_v2"):
                    return _FakePostResponse(
                        {"s": "ok", "data": {"access_token": "SESSION-TOKEN"}}
                    )
                return _FakePostResponse(
                    {
                        "s": "ok",
                        "Url": (
                            "https://trade.fyers.in/api-login/redirect-uri/"
                            "index.html?auth_code=CODE-Z9&state=None"
                        ),
                    }
                )

        return _Session()


def test_login_with_pin_totp_full_flow(tmp_path, monkeypatch) -> None:
    fake_requests = _FakeRequests()
    monkeypatch.setattr("app.data.fyers.requests", fake_requests)
    settings = _settings_auto()
    settings.fyers_client_id = "CID-100"
    auth = FyersAuthenticator(
        settings, token_file=tmp_path / "tokens.json", sdk=_FakeSDK()
    )

    record = auth.login_with_pin_totp()

    assert record["access_token"] == "AT-123"
    assert record["refresh_token"] == "RT-456"
    assert auth.ensure_token() == "AT-123"

    urls = [call["url"] for call in fake_requests.calls]
    assert urls == [
        "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
        "https://api-t2.fyers.in/vagator/v2/verify_otp",
        "https://api-t2.fyers.in/vagator/v2/verify_pin_v2",
        "https://api-t1.fyers.in/api/v3/token",
    ]
    otp_call = fake_requests.calls[1]["json"]
    assert otp_call["request_key"] == "RK1"
    assert otp_call["otp"].isdigit() and len(otp_call["otp"]) == 6
    otp_request = fake_requests.calls[0]["json"]
    assert otp_request["fy_id"] == base64.b64encode(b"FAK123").decode()
    assert otp_request["app_id"] == "2"
    pin_call = fake_requests.calls[2]["json"]
    assert pin_call["request_key"] == "RK2"
    assert pin_call["identity_type"] == "pin"
    assert pin_call["identifier"] == base64.b64encode(b"1234").decode()
    token_call = fake_requests.calls[3]
    assert token_call["headers"]["Authorization"] == "Bearer SESSION-TOKEN"
    assert token_call["json"]["app_id"] == "CID"
    assert token_call["json"]["appType"] == "100"
    assert token_call["json"]["fyers_id"] == "FAK123"


def test_ensure_token_uses_automated_login(tmp_path, monkeypatch) -> None:
    auth = FyersAuthenticator(
        _settings_auto(), token_file=tmp_path / "missing.json", sdk=_FakeSDK()
    )
    calls: list[str] = []

    def fake_login(self) -> dict:
        calls.append("login")
        self._access_token = "AUTO-TOKEN"
        return {"access_token": "AUTO-TOKEN"}

    monkeypatch.setattr(FyersAuthenticator, "login_with_pin_totp", fake_login)
    assert auth.ensure_token() == "AUTO-TOKEN"
    assert calls == ["login"]


def test_ensure_token_reuses_fresh_stored_token(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "tokens.json"
    auth = FyersAuthenticator(_settings_auto(), token_file=token_file, sdk=_FakeSDK())
    auth.exchange_code("CODE-ABC")

    def fail_login(self) -> dict:
        raise AssertionError("login should not run for a fresh token")

    monkeypatch.setattr(FyersAuthenticator, "login_with_pin_totp", fail_login)
    auth2 = FyersAuthenticator(_settings_auto(), token_file=token_file, sdk=_FakeSDK())
    assert auth2.ensure_token() == "AT-123"


def test_login_with_pin_totp_requires_credentials(tmp_path) -> None:
    auth = FyersAuthenticator(
        _settings_no_token(), token_file=tmp_path / "missing.json", sdk=_FakeSDK()
    )
    with pytest.raises(RuntimeError, match="Automated login requires"):
        auth.login_with_pin_totp()
