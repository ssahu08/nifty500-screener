"""Tests for the numeric-PIN dashboard gate and the PWA assets."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import auth as dashboard_auth
from app.config import get_settings
from app.main import app

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
client = TestClient(app)

PIN = "1234"


@pytest.fixture(autouse=True)
def _clean_auth_state():
    """Reset cookies and the login attempt tracker around every test."""
    client.cookies.clear()
    dashboard_auth.clear_failures()
    yield
    client.cookies.clear()
    dashboard_auth.clear_failures()


def _enable_gate(monkeypatch, pin: str = PIN) -> None:
    monkeypatch.setattr(get_settings(), "dashboard_pin", pin)


# -- gate disabled (local development) --------------------------------------
def test_auth_disabled_allows_api_without_cookie(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "dashboard_pin", "")
    status = client.get("/api/auth/status").json()
    assert status == {"enabled": False, "authenticated": True}
    assert client.get("/api/strategies").status_code == 200


# -- gate enabled ------------------------------------------------------------
def test_api_401s_when_pin_enabled(monkeypatch) -> None:
    _enable_gate(monkeypatch)
    res = client.get("/api/strategies")
    assert res.status_code == 401
    assert res.json()["detail"] == "PIN required"
    # The overlay must be able to load, and the pinger must stay reachable.
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/app.js").status_code == 200
    status = client.get("/api/auth/status").json()
    assert status == {"enabled": True, "authenticated": False}


def test_login_wrong_pin_rejected(monkeypatch) -> None:
    _enable_gate(monkeypatch)
    res = client.post("/api/auth/login", json={"pin": "9999"})
    assert res.status_code == 401
    assert client.get("/api/strategies").status_code == 401


def test_login_and_logout_session_flow(monkeypatch) -> None:
    _enable_gate(monkeypatch)
    res = client.post("/api/auth/login", json={"pin": PIN})
    assert res.status_code == 200
    assert dashboard_auth.SESSION_COOKIE in res.cookies
    assert client.get("/api/auth/status").json()["authenticated"] is True
    assert client.get("/api/strategies").status_code == 200
    assert client.get("/api/history/Open-Equals-Low?days=7").status_code == 200

    out = client.post("/api/auth/logout")
    assert out.status_code == 200
    assert client.get("/api/strategies").status_code == 401


def test_session_token_changes_with_pin(monkeypatch) -> None:
    _enable_gate(monkeypatch, "1111")
    first = dashboard_auth.session_token()
    _enable_gate(monkeypatch, "2222")
    assert dashboard_auth.session_token() != first


def test_login_rate_limited_after_five_failures(monkeypatch) -> None:
    _enable_gate(monkeypatch)
    for _ in range(dashboard_auth.MAX_ATTEMPTS):
        res = client.post("/api/auth/login", json={"pin": "0000"})
        assert res.status_code == 401
    # Even the correct PIN is refused while locked out.
    res = client.post("/api/auth/login", json={"pin": PIN})
    assert res.status_code == 401
    assert "attempts" in res.json()["detail"].lower()
    assert client.get("/api/strategies").status_code == 401


def test_successful_login_resets_attempt_counter(monkeypatch) -> None:
    _enable_gate(monkeypatch)
    client.post("/api/auth/login", json={"pin": "0000"})
    assert dashboard_auth.lockout_remaining("testclient") == 0.0
    assert client.post("/api/auth/login", json={"pin": PIN}).status_code == 200


# -- PWA assets --------------------------------------------------------------
def test_manifest_served_with_expected_config() -> None:
    res = client.get("/manifest.json")
    assert res.status_code == 200
    manifest = res.json()
    assert manifest["display"] == "standalone"
    assert manifest["orientation"] == "portrait"
    assert manifest["theme_color"] == "#0f172a"
    assert manifest["background_color"] == "#0f172a"
    assert manifest["start_url"] == "/"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_dashboard_head_has_pwa_and_auth_markup() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest" href="/manifest.json"' in html
    assert 'name="theme-color" content="#0f172a"' in html
    assert 'name="viewport"' in html and "viewport-fit=cover" in html
    assert 'id="pin-gate"' in html
    assert 'id="pin-input"' in html
