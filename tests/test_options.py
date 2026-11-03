"""Tests for Nifty expiry and FYERS option-symbol helpers."""

from __future__ import annotations

from datetime import date

import pytest

from app.data.options import (
    expiry_kind,
    expiry_to_fyers_format,
    is_monthly_expiry,
    is_nifty_expiry_day,
    next_weekly_expiry,
    nifty_option_symbol,
    nifty_option_symbol_candidates,
    round_to_nifty_strike,
)


def test_weekly_symbol_uses_single_letter_month_and_day() -> None:
    # Verified against the FYERS NSE_FO symbol master.
    assert nifty_option_symbol(26000, "CE", date(2026, 11, 3)) == (
        "NSE:NIFTY26N0326000CE"
    )
    assert nifty_option_symbol(26000, "PE", date(2026, 11, 3)) == (
        "NSE:NIFTY26N0326000PE"
    )


def test_monthly_symbol_uses_three_letter_month() -> None:
    assert nifty_option_symbol(26000, "CE", date(2026, 11, 23)) == (
        "NSE:NIFTY26NOV26000CE"
    )
    assert nifty_option_symbol(26000, "PE", date(2026, 11, 23)) == (
        "NSE:NIFTY26NOV26000PE"
    )


def test_weekly_and_monthly_kind() -> None:
    assert expiry_kind(date(2026, 11, 3)) == "weekly"
    assert expiry_kind(date(2026, 11, 23)) == "monthly"
    assert expiry_kind(date(2026, 12, 1)) == "weekly"
    assert expiry_kind(date(2026, 12, 29)) == "monthly"
    assert is_monthly_expiry(date(2026, 11, 23))
    assert not is_monthly_expiry(date(2026, 11, 17))


def test_expiry_format_fragment() -> None:
    assert expiry_to_fyers_format(date(2026, 11, 3)) == "26N03"
    assert expiry_to_fyers_format(date(2026, 11, 23)) == "26NOV"


def test_next_expiry_is_today_on_expiry_day() -> None:
    assert next_weekly_expiry(date(2026, 11, 3)) == date(2026, 11, 3)
    assert is_nifty_expiry_day(date(2026, 11, 3))


def test_holiday_shifted_expiry_moves_to_previous_trading_day() -> None:
    # 10 Nov 2026 is Diwali-Balipratipada (NSE holiday), so that week's
    # expiry is Monday 9 Nov 2026.
    assert next_weekly_expiry(date(2026, 11, 9)) == date(2026, 11, 9)
    assert is_nifty_expiry_day(date(2026, 11, 9))
    assert not is_nifty_expiry_day(date(2026, 11, 10))
    assert next_weekly_expiry(date(2026, 11, 10)) == date(2026, 11, 17)


def test_holiday_shifted_monthly_expiry() -> None:
    # 24 Nov 2026 is a holiday; the November monthly expiry is Monday 23 Nov.
    assert next_weekly_expiry(date(2026, 11, 23)) == date(2026, 11, 23)
    assert is_monthly_expiry(date(2026, 11, 23))
    assert next_weekly_expiry(date(2026, 11, 24)) == date(2026, 12, 1)


def test_monthly_candidate_has_same_date_weekly_fallback() -> None:
    candidates = nifty_option_symbol_candidates(26000, "CE", date(2026, 11, 23))
    assert candidates[0] == "NSE:NIFTY26NOV26000CE"
    assert "NSE:NIFTY26N2326000CE" in candidates


def test_weekly_tuesday_has_no_monthly_fallback() -> None:
    # A weekly expiry must never fall back to the monthly symbol: that symbol
    # would point at a later expiry in the same month.
    candidates = nifty_option_symbol_candidates(26000, "CE", date(2026, 11, 3))
    assert candidates == ["NSE:NIFTY26N0326000CE"]


def test_holiday_shifted_weekday_also_tries_nominal_tuesday() -> None:
    candidates = nifty_option_symbol_candidates(26000, "CE", date(2026, 11, 9))
    assert candidates[0] == "NSE:NIFTY26N0926000CE"
    assert "NSE:NIFTY26N1026000CE" in candidates


def test_invalid_option_type_raises() -> None:
    with pytest.raises(ValueError):
        nifty_option_symbol(26000, "XX", date(2026, 11, 3))


def test_round_to_nifty_strike() -> None:
    assert round_to_nifty_strike(26012.4) == 26000
    assert round_to_nifty_strike(26026.0) == 26050
