"""Tests for universe management."""

from __future__ import annotations

import pytest

from app.strategies.universe import (
    NIFTY_50,
    NIFTY_500,
    get_universe,
    save_universe,
)


def test_get_universe_fallback_nifty50() -> None:
    symbols = get_universe(NIFTY_50)
    assert "RELIANCE" in symbols
    assert "HDFCBANK" in symbols


def test_get_universe_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_universe("NONEXISTENT_UNIVERSE")


def test_save_and_load_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.strategies.universe._UNIVERSE_DIR", tmp_path / "u")
    save_universe(NIFTY_500, ["SBIN", "RELIANCE", "INFY"])
    symbols = get_universe(NIFTY_500)
    assert symbols == ["INFY", "RELIANCE", "SBIN"]  # sorted


def test_nifty500_populated_after_download() -> None:
    symbols = get_universe(NIFTY_500)
    assert len(symbols) > 0
    assert "RELIANCE" in symbols
