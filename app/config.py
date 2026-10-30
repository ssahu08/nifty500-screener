"""Central configuration for the screener.

All settings are read from environment variables (optionally via a local
``.env`` file). Credentials are never hard-coded and never committed.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings sourced from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------
    app_name: str = "N500 Stock Screener"
    env: str = "development"
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000
    timezone: str = "Asia/Kolkata"
    #: Numeric PIN protecting the dashboard API when deployed publicly
    #: (Render). Empty disables the gate (local development). Set via the
    #: DASHBOARD_PIN environment variable - never committed.
    dashboard_pin: str = ""

    # --- Storage -----------------------------------------------------------
    database_path: str = "data/screener.db"
    retention_days: int = Field(default=14, ge=1)

    # --- Market data sources -----------------------------------------------
    #: Live/primary market-data source, used while the NSE session is open.
    market_provider: str = "fyers"
    #: Source used outside session hours and as a fallback (empty disables).
    offhours_market_provider: str = "yfinance"

    # --- Groww (NSE market data, off-hours source) -------------------------
    #: Groww Trade API access token (Bearer header). Historical data is a paid
    #: Groww add-on (~₹499/month early bird); orders are free/₹10 per order.
    groww_api_key: str = ""
    #: Base URL of the Groww Trade API.
    groww_api_base_url: str = "https://api.groww.in"

    # --- FYERS (NSE market data) ------------------------------------------
    fyers_client_id: str = ""
    fyers_secret_key: str = ""
    fyers_redirect_uri: str = "http://127.0.0.1:8000/callback"
    fyers_fy_id: str = ""
    fyers_pin: str = ""
    fyers_totp_secret: str = ""
    fyers_access_token: str = ""
    #: Bootstrap refresh token (15-day validity). Used only when the cached
    #: data/fyers_tokens.json is missing (fresh deploy on an ephemeral disk);
    #: rotated tokens are persisted to that file thereafter.
    fyers_refresh_token: str = ""

    # --- Telegram ----------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_alerts_enabled: bool = True
    telegram_notify_empty: bool = False

    # --- Nifty Hedge 23 (expiry-day straddle hedge) ------------------------
    hedge_23_enabled: bool = True
    hedge_23_mode: str = "simple"  # "simple" or "averaging" (50-30-20)
    hedge_23_capital: float = 100000.0  # total trading capital (₹)
    hedge_23_max_capital_pct: float = 0.05  # max capital per trade (5%)
    hedge_23_lot_size: int = 65  # Nifty lot size
    hedge_23_min_entry_gap_minutes: int = 5  # min gap between re-entries

    # --- Kite Connect (Zerodha) -----------------------------------------
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_fy_id: str = ""
    kite_pin: str = ""
    kite_totp_secret: str = ""
    kite_redirect_uri: str = "https://localhost:8000/kite/callback"
    kite_access_token: str = ""

    # --- Global indices ----------------------------------------------------
    global_index_provider: str = "yfinance"

    @property
    def fyers_configured(self) -> bool:
        """True when enough FYERS credentials are present to authenticate."""
        return bool(self.fyers_client_id and self.fyers_secret_key)

    @property
    def fyers_automated_login_configured(self) -> bool:
        """True when the PIN + TOTP login flow can run unattended."""
        return bool(
            self.fyers_client_id
            and self.fyers_secret_key
            and self.fyers_fy_id
            and self.fyers_pin
            and self.fyers_totp_secret
        )

    @property
    def kite_configured(self) -> bool:
        return bool(self.kite_api_key and self.kite_api_secret)

    @property
    def kite_automated_login_configured(self) -> bool:
        return bool(
            self.kite_api_key
            and self.kite_api_secret
            and self.kite_fy_id
            and self.kite_pin
            and self.kite_totp_secret
        )

    @property
    def telegram_configured(self) -> bool:
        """True when both Telegram credentials are present."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
