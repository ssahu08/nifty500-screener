"""Tests for the Telegram alerter (HTTP layer mocked)."""

from __future__ import annotations

from datetime import datetime

from app.alerts.telegram import (
    MAX_MESSAGE_CHARS,
    TelegramAlerter,
    _format_time,
    _split,
)
from app.config import Settings
from app.strategies.base import Signal


def _settings(**kwargs) -> Settings:
    defaults = {
        "_env_file": None,
        "telegram_bot_token": "tok",
        "telegram_chat_id": "chat",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def _signal(symbol="NSE:SBIN-EQ", price=469.25) -> Signal:
    return Signal(
        symbol=symbol,
        strategy_name="Open-Equals-Low",
        timestamp=datetime(2026, 1, 1, 9, 30, 45),
        price=price,
    )


def _banded(symbol: str, band: str, score: float) -> Signal:
    return Signal(
        symbol=symbol,
        strategy_name="EOD Compression Breakout",
        timestamp=datetime(2026, 1, 1, 15, 20),
        price=100.0,
        metadata={"band": band, "score": score, "entry": 100.1, "stop": 99.1},
    )


def test_banded_run_alerts_only_buy_band(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "app.alerts.telegram.httpx.post",
        lambda *a, **k: calls.append((a, k)) or _FakeResponse(),
    )
    alerter = TelegramAlerter(_settings())
    signals = [
        _banded("NSE:A-EQ", "HOLD", 60.0),
        _banded("NSE:B-EQ", "BUY", 82.0),
        _banded("NSE:C-EQ", "SKIP", 30.0),
    ]
    assert alerter.send_strategy_results("EOD Compression Breakout", signals) is True
    assert len(calls) == 1
    text = calls[0][1]["json"]["text"]
    assert "NSE:B-EQ" in text
    assert "NSE:A-EQ" not in text
    assert "NSE:C-EQ" not in text
    assert "3 matched" in text
    assert "1 BUY-band" in text
    assert "82.0" in text and "100.1" in text and "99.1" in text


def test_banded_run_without_buy_band_is_silent(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "app.alerts.telegram.httpx.post",
        lambda *a, **k: calls.append((a, k)) or _FakeResponse(),
    )
    alerter = TelegramAlerter(_settings())
    assert alerter.send_strategy_results("S", [_banded("NSE:A-EQ", "HOLD", 60.0)]) is False
    assert calls == []


class _FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


def test_disabled_when_unconfigured() -> None:
    assert TelegramAlerter(Settings(_env_file=None)).enabled is False


def test_enabled_with_credentials() -> None:
    assert TelegramAlerter(_settings()).enabled is True


def test_master_switch_disables_alerts() -> None:
    alerter = TelegramAlerter(_settings(telegram_alerts_enabled=False))
    assert alerter.enabled is False
    assert alerter.send_strategy_results("S", [_signal()]) is False


def test_empty_run_skipped_by_default(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "app.alerts.telegram.httpx.post",
        lambda *a, **k: calls.append((a, k)) or _FakeResponse(),
    )
    alerter = TelegramAlerter(_settings(telegram_notify_empty=False))
    assert alerter.send_strategy_results("S", []) is False
    assert calls == []


def test_empty_run_sent_when_opted_in(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "app.alerts.telegram.httpx.post",
        lambda *a, **k: calls.append((a, k)) or _FakeResponse(),
    )
    alerter = TelegramAlerter(_settings(telegram_notify_empty=True))
    assert alerter.send_strategy_results("S", []) is True
    assert len(calls) == 1
    body = calls[0][1]["json"]
    assert "No stocks matched" in body["text"]


def test_format_contains_symbol_time_price(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.alerts.telegram.httpx.post", lambda *a, **k: _FakeResponse()
    )
    alerter = TelegramAlerter(_settings())
    text = alerter._format("Open-Equals-Low", [_signal()])
    assert "*Open-Equals-Low*" in text
    assert "NSE:SBIN-EQ" in text
    assert "2026-01-01 09:30:45" in text
    assert "469.25" in text


def test_send_failure_returns_false_and_does_not_raise(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.alerts.telegram.httpx.post", boom)
    alerter = TelegramAlerter(_settings())
    assert alerter.send_strategy_results("S", [_signal()]) is False


def test_http_error_returns_false(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.alerts.telegram.httpx.post",
        lambda *a, **k: _FakeResponse(status_code=429, text="rate limited"),
    )
    alerter = TelegramAlerter(_settings())
    assert alerter.send_strategy_results("S", [_signal()]) is False


def test_format_time_accepts_string_and_datetime() -> None:
    assert _format_time("2026-01-01T09:30:45") == "2026-01-01 09:30:45"
    assert _format_time(datetime(2026, 1, 1, 9, 30)) == "2026-01-01 09:30:00"
    assert _format_time("not-a-date") == "not-a-date"


def test_split_chunks_respect_limit() -> None:
    long_text = "\n".join("line of text " * 50 for _ in range(80))
    parts = _split(long_text, MAX_MESSAGE_CHARS)
    assert len(parts) >= 2
    assert all(len(p) <= MAX_MESSAGE_CHARS for p in parts)
    assert "".join(parts) == long_text


def test_split_empty_string_returns_one_chunk() -> None:
    assert _split("", MAX_MESSAGE_CHARS) == [""]


def test_banded_short_signal_shows_direction_setup_and_target(monkeypatch) -> None:
    """Bidirectional strategies must not send direction-less SHORT alerts."""
    calls = []
    monkeypatch.setattr(
        "app.alerts.telegram.httpx.post",
        lambda *a, **k: calls.append((a, k)) or _FakeResponse(),
    )
    alerter = TelegramAlerter(_settings())
    signal = Signal(
        symbol="NSE:INFY-EQ",
        strategy_name="Guppy Ribbon Compression",
        timestamp=datetime(2026, 1, 1, 15, 20),
        price=1500.0,
        metadata={
            "band": "BUY",
            "score": 80.0,
            "direction": "SHORT",
            "setup_type": "SHORT_BREAKDOWN",
            "entry": 1500.0,
            "stop": 1530.0,
            "target_1": 1425.0,
            "risk_pct": 2.0,
        },
    )
    assert alerter.send_strategy_results("Guppy Ribbon Compression", [signal]) is True
    text = calls[0][1]["json"]["text"]
    assert "SHORT SHORT_BREAKDOWN" in text
    assert "T1 1425.0" in text


def test_oel_line_shows_day_open_and_low(monkeypatch) -> None:
    """The alert must make the day-open == day-low criterion verifiable."""
    alerter = TelegramAlerter(_settings())
    signal = Signal(
        symbol="NSE:COFORGE-EQ",
        strategy_name="Open-Equals-Low",
        timestamp=datetime(2026, 1, 1, 9, 15),
        price=2045.0,
        metadata={
            "timeframe": "10m",
            "day_open": 1981.1,
            "day_low": 1981.1,
            "volume_ratio": 4.31,
            "move_pct": 3.23,
        },
    )
    text = alerter._format("Open-Equals-Low", [signal])
    assert "open=1981.1 low=1981.1" in text
    assert "tf=10m" in text
    assert "move=3.23%" in text


def test_oel_alert_ranks_by_quality_and_shows_volume() -> None:
    """Best opening volume is #1; thin volume is last; open/low stay visible."""
    alerter = TelegramAlerter(_settings())

    def _oel(symbol: str, score: float, quality: str, vol: float) -> Signal:
        return Signal(
            symbol=symbol,
            strategy_name="Open-Equals-Low",
            timestamp=datetime(2026, 1, 1, 9, 15),
            price=103.0,
            metadata={
                "timeframe": "10m",
                "day_open": 100.0,
                "day_low": 100.0,
                "score": score,
                "quality": quality,
                "open_vol_ratio": vol,
                "open_vol_pctile": 100.0 if vol >= 3 else 0.0,
                "open_vol_history_days": 10,
                "volume_ratio": vol,
                "move_pct": 3.0,
            },
        )

    thin = _oel("NSE:THIN-EQ", 25.0, "LOW", 0.5)
    heavy = _oel("NSE:HEAVY-EQ", 88.0, "HIGH", 3.0)
    text = alerter._format("Open-Equals-Low", [thin, heavy])

    assert text.index("#1") < text.index("#2")
    assert text.index("HEAVY") < text.index("THIN")
    assert "score=88.0 HIGH" in text
    assert "vol10=3.0x (100% of 10d)" in text
    assert "open=100.0 low=100.0" in text
