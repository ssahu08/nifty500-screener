"""Smoke tests for the FastAPI application shell."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_health() -> None:
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["retention_days"] == 14


def test_dashboard_served() -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "N500" in res.text
    assert "Market Overview" in res.text


def test_dashboard_has_containers() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for element_id in ("overview", "results", "filters", "toasts"):
        assert f'id="{element_id}"' in html
    assert "/app.js" in html and "/styles.css" in html


def test_dashboard_js_targets_api() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for endpoint in ("/health", "/api/strategies", "/api/results", "/api/overview",
                     "/api/run/", "/api/history/"):
        assert endpoint in js
    assert "?refresh=true" in js
