"""Telegram alert sender (direct HTTP calls to the Telegram Bot API).

Sends one message per run that has results (FR-12). Message content matches
what the dashboard shows (FR-13): strategy name, and for each signal the
symbol, time met and current price (FR-9), plus strategy-specific details.
Empty runs are not sent unless ``TELEGRAM_NOTIFY_EMPTY`` is enabled (FR-14).
A send failure is logged and never raises (NFR-6).

Each strategy gets its own formatter so the message includes the fields that
matter most for that strategy (confluence score, band/entry/stop, hedge
action/SL/P&L, etc.).
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Telegram rejects messages over 4096 chars; stay safely below.
MAX_MESSAGE_CHARS = 3800
API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramAlerter:
    """Send strategy alerts to a Telegram chat (best-effort, never raises)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.telegram_alerts_enabled and self.settings.telegram_configured
        )

    def send_strategy_results(self, strategy_name: str, signals: list) -> bool:
        """Send one alert for a run. Returns ``True`` on success.

        Strategies that emit a confidence ``band`` in the signal metadata only
        alert their BUY-band signals (the full ranked list stays on the
        dashboard); other strategies alert every signal as before.
        """
        if not self.enabled:
            logger.debug("Telegram disabled or unconfigured; skipping alert")
            return False

        meta0 = getattr(signals[0], "metadata", None) or {} if signals else {}
        action = meta0.get("action")

        banded = [s for s in signals if (getattr(s, "metadata", None) or {}).get("band")]
        if banded:
            buys = [s for s in banded if s.metadata.get("band") == "BUY"]
            if not buys:
                return False
            return self._send(self._format_banded(strategy_name, buys, len(signals)))

        if action in {"ENTRY", "EXIT"}:
            return self._send(self._format_hedge(strategy_name, signals))

        if not signals:
            if not self.settings.telegram_notify_empty:
                return False
            return self._send(self._format_empty(strategy_name))

        return self._send(self._format(strategy_name, signals))

    # -- formatting: generic ------------------------------------------------
    def _format(self, strategy_name: str, signals: list) -> str:
        # Quality-scored runs (OEL) are shown best-first; low-volume matches
        # fall to the bottom. Unscored strategies keep their original order.
        ranked = sorted(
            signals,
            key=lambda s: (
                -((s.metadata or {}).get("score") or 0.0),
                -((s.metadata or {}).get("open_vol_ratio") or 0.0),
                s.symbol,
            ),
        )
        lines = [f"*{strategy_name}*", ""]
        for rank, s in enumerate(ranked, 1):
            meta = s.metadata or {}
            show_rank = rank if meta.get("score") is not None else None
            lines.append(self._signal_line_generic(s, meta, rank=show_rank))
        lines.append("")
        lines.append(f"_{len(signals)} stock(s) matched._")
        return "\n".join(lines)

    def _signal_line_generic(
        self, s, meta: dict, rank: int | None = None
    ) -> str:
        prefix = f"#{rank} " if rank else ""
        parts = [f"{prefix}`{s.symbol}` | {_format_time(s.timestamp)} | Rs{s.price:.2f}"]
        tf = meta.get("timeframe")
        if tf:
            parts.append(f"tf={tf}")
        # OEL: show the actual open/low so "open == low" is verifiable.
        open_ = meta.get("day_open")
        low_ = meta.get("day_low")
        if open_ is None:
            open_ = meta.get("week_open", meta.get("month_open"))
            low_ = meta.get("week_low", meta.get("month_low"))
        if open_ is not None and low_ is not None:
            parts.append(f"open={open_} low={low_}")
        subtype = meta.get("subtype")
        if subtype:
            parts.append(f"{subtype}")
        confluence = meta.get("confluence_score")
        if confluence is not None:
            parts.append(f"score={confluence}")
        quality_score = meta.get("score")
        if quality_score is not None:
            quality = meta.get("quality")
            parts.append(
                f"score={quality_score}" + (f" {quality}" if quality else "")
            )
        open_vol = meta.get("open_vol_ratio")
        if open_vol is not None:
            detail = f"vol10={open_vol}x"
            pct = meta.get("open_vol_pctile")
            days = meta.get("open_vol_history_days")
            if pct is not None and days:
                detail += f" ({pct:.0f}% of {days}d)"
            parts.append(detail)
        else:
            vol_ratio = meta.get("volume_ratio")
            if vol_ratio is not None:
                parts.append(f"vol={vol_ratio}x")
        daily_vol = meta.get("daily_vol_ratio")
        if daily_vol is not None and open_vol is not None:
            parts.append(f"volD={daily_vol}x")
        move_pct = meta.get("move_pct")
        if move_pct is not None:
            parts.append(f"move={move_pct}%")
        return "- " + " | ".join(parts)

    # -- formatting: banded (compression) -----------------------------------
    def _format_banded(self, strategy_name: str, signals: list, total: int) -> str:
        lines = [f"*{strategy_name}*", ""]
        for s in signals:
            meta = s.metadata or {}
            tag = " ".join(
                part for part in (meta.get("direction", ""), meta.get("setup_type", "")) if part
            )
            tag = f" [{tag}]" if tag else ""
            lines.append(
                f"- `{s.symbol}`{tag} | score {meta.get('score', '?')} "
                f"{meta.get('band', '')} | Rs{s.price:.2f} | "
                f"entry {meta.get('entry', '?')} SL {meta.get('stop', '?')} "
                f"T1 {meta.get('target_1', '-')} "
                f"risk {meta.get('risk_pct', '?')}% | exp {meta.get('expected_move_date', '-')}"
            )
        lines.append("")
        lines.append(
            f"_{total} matched - showing {len(signals)} BUY-band. "
            "Full list on dashboard._"
        )
        return "\n".join(lines)

    # -- formatting: hedge (entry / exit) -----------------------------------
    def _format_hedge(self, strategy_name: str, signals: list) -> str:
        lines = [f"*{strategy_name}*", ""]
        for s in signals:
            meta = s.metadata or {}
            action = meta.get("action", "?")
            mode = meta.get("mode", "?")
            strike = meta.get("strike", "?")
            expiry = meta.get("expiry", "-")
            lots = meta.get("lots", "?")
            combined = meta.get("combined_premium", 0)
            sl = meta.get("sl_price", "-")
            avg = meta.get("entry_average", meta.get("average_entry", "-"))

            if action == "ENTRY":
                ce_sym = meta.get("ce_symbol", "-")
                pe_sym = meta.get("pe_symbol", "-")
                entry_lvl = meta.get("entry_level", "-")
                lines.append(
                    f"- *ENTRY* [{mode}] strike {strike} ({expiry}) "
                    f"| {ce_sym} / {pe_sym} | combined Rs{combined} | "
                    f"lots={lots} | level={entry_lvl} | SL=Rs{sl}"
                )
            else:  # EXIT
                reason = meta.get("reason", "-")
                exit_px = meta.get("combined_premium", 0)
                pnl = meta.get("pnl_estimate")
                booked = meta.get("booked_lots", 0)
                remaining = meta.get("remaining_lots", "-")
                pnl_txt = f" | P&L ≈ Rs{pnl}" if pnl is not None else ""
                lines.append(
                    f"- *EXIT* [{reason}] strike {strike} ({expiry}) "
                    f"| combined Rs{exit_px} | avg Rs{avg} | "
                    f"booked={booked} / remaining={remaining}{pnl_txt}"
                )
        lines.append("")
        lines.append(f"_{len(signals)} signal(s)._")
        return "\n".join(lines)

    def _format_empty(self, strategy_name: str) -> str:
        return f"*{strategy_name}*\n\nNo stocks matched this run."

    # -- sending ------------------------------------------------------------
    def _send(self, text: str) -> bool:
        for chunk in _split(text, MAX_MESSAGE_CHARS):
            if not self._send_one(chunk):
                return False
        return True

    def _send_one(self, text: str) -> bool:
        try:
            resp = httpx.post(
                API_URL.format(token=self.settings.telegram_bot_token),
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Telegram send failed (HTTP %s): %s", resp.status_code, resp.text[:200]
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001 - network failures must not crash
            logger.warning("Telegram send failed: %s", exc)
            return False


def test_alert(settings: Settings) -> bool:
    """Send a test message to verify Telegram is configured correctly."""
    alerter = TelegramAlerter(settings)
    if not alerter.enabled:
        logger.warning("Telegram not enabled or unconfigured; cannot send test")
        return False
    text = (
        "*N500 Stock Screener*\n\n"
        "Telegram test message — alerts are working. "
        "You will receive strategy signals here."
    )
    return alerter._send(text)


def _format_time(ts) -> str:
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return ts
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    return str(ts)


def _split(text: str, limit: int) -> list[str]:
    """Split ``text`` on line boundaries so no chunk exceeds ``limit``."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [""]
