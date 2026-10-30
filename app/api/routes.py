"""HTTP API endpoints (Phase 6).

The UI talks only to this API, keeping the strategy engine decoupled from
presentation (NFR-2). Every handler reads its components from ``app.state``
which the application lifespan builds once.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api import auth as dashboard_auth
from app.core.logging import get_logger
from app.core.market import last_trading_days
from app.data.global_index import INDEX_TICKERS
from app.data.kite import KiteAuthenticator

logger = get_logger(__name__)

router = APIRouter(prefix="/api")

#: FYERS symbol for the Nifty 50 index (verify against current FYERS docs).
NIFTY_FYERS_SYMBOL = "NSE:NIFTY 50-EQ"

#: Market Overview cache (yfinance + FYERS) shared across polls.
_OVERVIEW_TTL = 600.0
_overview_cache: dict = {"at": 0.0, "payload": None}


def clear_overview_cache() -> None:
    _overview_cache["at"] = 0.0
    _overview_cache["payload"] = None


# -- registry ---------------------------------------------------------------
@router.get("/strategies")
def list_strategies(request: Request) -> list[dict]:
    """Every registered strategy with its metadata (FR-10/FR-19 context)."""
    registry = request.app.state.registry
    return [
        {
            "name": s.metadata.name,
            "description": s.metadata.description,
            "category": s.metadata.category.value,
            "universe": s.metadata.universe,
            "schedule_type": s.metadata.schedule_type,
            "frequency_minutes": s.metadata.frequency_minutes,
            "run_at": s.metadata.run_at,
            "timeframes": s.metadata.timeframes,
        }
        for s in registry.values()
    ]


# -- results ----------------------------------------------------------------
@router.get("/results")
def latest_results(
    request: Request,
    category: str | None = Query(None, description="Filter by category slug"),
) -> list[dict]:
    """Latest run per strategy, optionally filtered by category (FR-10)."""
    registry = request.app.state.registry
    store = request.app.state.store
    latest = store.get_all_latest()

    result = []
    for name, rows in latest.items():
        strategy = registry.get(name)
        if strategy is None:
            continue  # results for strategies that were removed
        cat = strategy.metadata.category.value
        if category and cat != category:
            continue
        result.append(
            {
                "strategy": name,
                "category": cat,
                "signals": rows,
            }
        )
    return result


@router.get("/results/{strategy_name}")
def strategy_results(strategy_name: str, request: Request) -> dict:
    """Latest run for a single strategy."""
    registry = request.app.state.registry
    if strategy_name not in registry:
        raise HTTPException(status_code=404, detail=f"Unknown strategy {strategy_name!r}")
    rows = request.app.state.store.get_latest(strategy_name)
    return {"strategy": strategy_name, "signals": rows}


@router.get("/history/{strategy_name}")
def strategy_history(
    strategy_name: str,
    request: Request,
    days: int = Query(default=14, ge=1, le=60),
) -> dict:
    """Historical signals within the rolling retention window (FR-11/FR-18)."""
    registry = request.app.state.registry
    if strategy_name not in registry:
        raise HTTPException(status_code=404, detail=f"Unknown strategy {strategy_name!r}")
    rows = request.app.state.store.get_history(strategy_name, days=days)
    return {"strategy": strategy_name, "days": days, "signals": rows}


# -- manual trigger ---------------------------------------------------------
@router.post("/run/{strategy_name}")
def run_strategy(strategy_name: str, request: Request) -> dict:
    """Manually trigger a strategy (FR-19)."""
    scheduler = request.app.state.scheduler
    try:
        count = scheduler.run_manual(strategy_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    signals = request.app.state.store.get_latest(strategy_name)
    return {"strategy": strategy_name, "signals": count, "results": signals}


@router.get("/run/{strategy_name}/status")
def run_status(strategy_name: str, request: Request) -> dict:
    """Live progress of the strategy's latest run (the dashboard polls this)."""
    registry = request.app.state.registry
    if strategy_name not in registry:
        raise HTTPException(status_code=404, detail=f"Unknown strategy {strategy_name!r}")
    return request.app.state.scheduler.run_status(strategy_name)


# -- telegram test ------------------------------------------------------------
@router.post("/telegram/test")
def telegram_test(request: Request) -> dict:
    """Send a Telegram test message to verify bot configuration (FR-5)."""
    settings = request.app.state.settings
    try:
        from app.alerts import send_test_alert

        ok = send_test_alert(settings)
    except Exception as exc:  # noqa: BLE001
        logger.error("Telegram test failed: %s", exc)
        return {"ok": False, "error": str(exc), "configured": settings.telegram_configured}
    return {"ok": ok, "configured": settings.telegram_configured}


# -- admin console (read-only, observability only) --------------------------
@router.get("/admin/usage")
def admin_usage(request: Request) -> dict:
    """Real-time API rate-limit usage (read-only). No impact on core logic."""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        raise HTTPException(status_code=503, detail="Rate limiter not exposed")
    return limiter.snapshot()


@router.get("/admin/storage")
def admin_storage(request: Request) -> dict:
    """Application SQLite storage summary (read-only)."""
    store = request.app.state.store
    return store.storage_summary()


# -- market overview --------------------------------------------------------
#: Days shown in the overview plus one extra for the first change %.
_OVERVIEW_DISPLAY_DAYS = 5
@router.get("/overview")
def market_overview(
    request: Request,
    refresh: bool = Query(default=False, description="Bypass the cache"),
) -> dict:
    """Market Overview: last 5 NSE trading days with per-day change %, newest first.

    ``columns`` are ISO dates newest-first; each row carries ``values`` (closes)
    and ``changes`` (% vs the previous trading day) aligned to those columns.
    """
    now = time.monotonic()
    if not refresh and _overview_cache["payload"] is not None:
        if now - _overview_cache["at"] < _OVERVIEW_TTL:
            return _overview_cache["payload"]

    settings = request.app.state.settings
    days = last_trading_days(_OVERVIEW_DISPLAY_DAYS + 1)
    errors: list[str] = []

    # Global rows (includes a yfinance Nifty row as a fallback source).
    global_values: dict[str, dict[date, float | None]] = {}
    try:
        global_values = request.app.state.global_provider.get_overview(days)
    except Exception as exc:  # noqa: BLE001 - a data outage must not 500 (NFR-6)
        logger.error("Global index fetch failed: %s", exc)
        errors.append(f"global indices: {exc}")

    rows = []
    for label in INDEX_TICKERS:
        values = [None] * len(days)
        day_map = global_values.get(label, {})
        for i, day in enumerate(days):
            values[i] = day_map.get(day)

        # Nifty: prefer FYERS when configured, else the yfinance value above.
        if label == "Nifty" and settings.fyers_configured:
            try:
                fyers_values = _fetch_nifty_from_fyers(request, days)
                for i, v in enumerate(fyers_values):
                    if v is not None:
                        values[i] = v
            except Exception as exc:  # noqa: BLE001
                logger.error("FYERS Nifty fetch failed: %s", exc)
                errors.append(f"nifty (FYERS): {exc}")

        rows.append({"label": label, "values": values})

    # Drop the extra oldest day; derive % change vs the prior trading day, and
    # present newest-first (the dashboard order requested for the overview).
    columns = [d.isoformat() for d in reversed(days[1:])]
    payload_rows: list[dict] = []
    for row in rows:
        closes = row["values"][1:]
        changes: list[float | None] = []
        for i in range(1, len(row["values"])):
            prev, curr = row["values"][i - 1], row["values"][i]
            if prev is None or curr is None or prev == 0:
                changes.append(None)
            else:
                changes.append((curr - prev) / prev * 100.0)
        payload_rows.append(
            {
                "label": row["label"],
                "values": list(reversed(closes)),
                "changes": list(reversed(changes)),
            }
        )

    payload = {"columns": columns, "rows": payload_rows, "errors": errors}
    _overview_cache.update({"at": now, "payload": payload})
    return payload


def _fetch_nifty_from_fyers(request: Request, days: list[date]) -> list[float | None]:
    """Daily closes for Nifty via FYERS, aligned to ``days`` (None if closed)."""
    provider = request.app.state.market_provider
    start = days[0] - timedelta(days=5)
    end = days[-1]
    df = provider.get_candles(NIFTY_FYERS_SYMBOL, "daily", start, end)
    closes: list[float | None] = [None] * len(days)
    if df is None or df.empty:
        return closes
    for i, day in enumerate(days):
        rows = df["close"][df.index.date == day]
        if not rows.empty:
            closes[i] = float(rows.iloc[-1])
    return closes


# -- Kite Connect (instrument token for chart links) ----------------------


@router.get("/kite/login-url")
def kite_login_url(request: Request) -> dict:
    """URL to open in a browser to get a Kite request_token."""
    settings = request.app.state.settings
    if not settings.kite_configured:
        raise HTTPException(status_code=400, detail="KITE_API_KEY / KITE_API_SECRET not set")
    auth = KiteAuthenticator(settings)
    return {"login_url": auth.get_login_url()}


@router.post("/kite/login")
def kite_login(request: Request, request_token: str) -> dict:
    """Exchange a Kite ``request_token`` (from browser redirect) for a token."""
    settings = request.app.state.settings
    if not settings.kite_configured:
        raise HTTPException(status_code=400, detail="KITE_API_KEY / KITE_API_SECRET not set")
    auth = KiteAuthenticator(settings)
    try:
        auth.exchange_code(request_token)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    instruments = {}
    try:
        instruments = auth.fetch_instruments()
    except Exception as exc:
        logger.warning("Kite instrument fetch after login failed: %s", exc)
    return {
        "ok": True,
        "access_token_set": bool(auth.ensure_token()),
        "instruments_loaded": len(instruments),
    }


@router.get("/kite/instruments")
def kite_instruments(request: Request, symbol: str | None = None) -> dict:
    """Fetch (or cache) NSE instrument tokens from Kite."""
    settings = request.app.state.settings
    auth = KiteAuthenticator(settings)
    token = auth.ensure_token()
    if token is None:
        raise HTTPException(status_code=401, detail="Kite not authorised; open /api/kite/login-url")
    instruments = auth.fetch_instruments(token)
    if symbol:
        return {"symbol": symbol, "instrument_token": instruments.get(symbol)}
    return {"count": len(instruments), "sample": dict(list(instruments.items())[:10])}


# -- dashboard PIN auth (public deployments) --------------------------------
class PinLogin(BaseModel):
    """Numeric PIN submitted by the login overlay."""

    pin: str = ""


def _client_ip(request: Request) -> str:
    """Best-effort client address (Render proxies set X-Forwarded-For)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    """Whether the PIN gate is active and this session is unlocked."""
    cookie = request.cookies.get(dashboard_auth.SESSION_COOKIE)
    return {
        "enabled": dashboard_auth.auth_enabled(),
        "authenticated": dashboard_auth.is_valid_session(cookie),
    }


@router.post("/auth/login")
def auth_login(payload: PinLogin, request: Request) -> JSONResponse:
    """Exchange the numeric PIN for an HttpOnly session cookie (rate-limited)."""
    ok, message = dashboard_auth.verify_pin(payload.pin, _client_ip(request))
    if not ok:
        raise HTTPException(status_code=401, detail=message)
    secure = (
        request.headers.get("x-forwarded-proto", "") == "https"
        or request.url.scheme == "https"
    )
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        dashboard_auth.SESSION_COOKIE,
        dashboard_auth.session_token(),
        max_age=dashboard_auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
    )
    logger.info("Dashboard PIN login succeeded from %s", _client_ip(request))
    return response


@router.post("/auth/logout")
def auth_logout() -> JSONResponse:
    """Clear the session cookie."""
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(dashboard_auth.SESSION_COOKIE)
    return response
