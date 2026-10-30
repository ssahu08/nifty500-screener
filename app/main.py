"""FastAPI application entry point.

Builds the application state (store, registry, scheduler, data providers) in
the lifespan, registers API routes, then serves the static dashboard shell.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _build_state(app: FastAPI) -> None:
    """Assemble the application components (called from the lifespan)."""
    from app.data import build_global_index_provider, build_market_provider
    from app.scheduler.jobs import StrategyScheduler
    from app.storage.db import SignalStore
    from app.strategies.registry import get_registry

    settings = get_settings()
    store = SignalStore(settings)
    registry = get_registry()
    provider = build_market_provider(settings)
    # Give every strategy direct access to the market-data provider so they
    # can fetch realtime quotes (e.g. option premiums) inside scan().
    for strategy in registry.values():
        strategy.provider = provider
    scheduler = StrategyScheduler(settings, registry, provider, store)
    scheduler.schedule_auto_strategies()

    app.state.settings = settings
    app.state.store = store
    app.state.registry = registry
    app.state.scheduler = scheduler
    app.state.market_provider = provider
    app.state.global_provider = build_global_index_provider(settings)

    # Expose the rate limiter for the admin console (read-only, no impact on
    # core logic). Walk the provider chain to find the underlying limiter.
    app.state.rate_limiter = _extract_rate_limiter(provider)

    if settings.env != "test":
        scheduler.start()
    logger.info("Application state built (%d strategies)", len(registry))


def _extract_rate_limiter(provider):
    """Walk the provider chain to locate a RateLimiter (read-only access)."""
    limiter = getattr(provider, "rate_limiter", None)
    if limiter is not None:
        return limiter
    for attr in ("primary", "offhours"):
        sub = getattr(provider, attr, None)
        if sub is not None:
            limiter = getattr(sub, "rate_limiter", None)
            if limiter is not None:
                return limiter
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    _build_state(app)
    try:
        yield
    finally:
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown()


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Personal NSE stock screener. Advisory only - never places trades.",
    lifespan=lifespan,
)


@app.get("/health", tags=["system"])
def health() -> dict[str, object]:
    """Liveness probe and a quick view of what is configured."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.env,
        "fyers_configured": settings.fyers_configured,
        "telegram_configured": settings.telegram_configured,
        "retention_days": settings.retention_days,
    }


# API routes are registered before the static mount so they take precedence
# over the catch-all dashboard mount.
from fastapi.responses import JSONResponse  # noqa: E402

from app.api import auth as dashboard_auth  # noqa: E402
from app.api.routes import router  # noqa: E402

app.include_router(router)


@app.middleware("http")
async def dashboard_pin_gate(request, call_next):  # noqa: ANN001
    """401 every data endpoint until the session presents a valid PIN cookie.

    Static assets (HTML/CSS/JS/icons/manifest) and /health stay public so the
    login overlay can render and external pingers keep the instance awake.
    """
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        cookie = request.cookies.get(dashboard_auth.SESSION_COOKIE)
        if not dashboard_auth.is_valid_session(cookie):
            return JSONResponse({"detail": "PIN required"}, status_code=401)
    return await call_next(request)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:  # pragma: no cover - convenience entry point
    """Run the app with uvicorn (``python -m app.main``)."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.env == "development",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
