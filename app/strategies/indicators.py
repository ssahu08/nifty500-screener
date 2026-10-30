"""Technical indicator helpers: ATR (Wilder), RSI, Supertrend, Bollinger
Bands, and resampling.

Pure pandas implementations - no external TA library required, fully testable.
All functions operate on OHLCV frames with columns ``open, high, low, close,
volume`` and return Series/DataFrame aligned to the input index.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.core.market import CONTINUOUS_CLOSE, MARKET_CLOSE

#: Bar duration per intraday timeframe name (minutes).
_TIMEFRAME_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "60m": 60, "1h": 60}


def closed_candles(
    df: pd.DataFrame | None,
    timeframe: str,
    now: datetime,
) -> pd.DataFrame | None:
    """Trim a candle frame to the bars fully closed at ``now`` (IST).

    Drops the in-progress bar and every bar that does not fit inside the
    continuous session (09:15-15:15). That removes FYERS' closing-auction
    artifacts - the flat ~zero-volume 15:15 auction-window bar and the
    5-minute 15:25 auction match - which otherwise match "open equals low"
    and doji screens trivially. Daily bars only count once their session has
    ended, so today's partial daily candle is ignored. Weekly/monthly frames
    are returned unchanged. ``now`` must be timezone-aware.
    """
    if df is None or df.empty:
        return df

    tz = getattr(df.index, "tz", None)
    if tz is not None and now.tzinfo is not None:
        now = now.astimezone(tz)

    if timeframe in {"daily", "1d"}:
        for i in range(len(df) - 1, -1, -1):
            label = df.index[i]
            if label.date() < now.date() or (
                label.date() == now.date() and now.time() >= MARKET_CLOSE
            ):
                return df.iloc[: i + 1]
        return df.iloc[:0]

    minutes = _TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        return df

    duration = timedelta(minutes=minutes)
    for i in range(len(df) - 1, -1, -1):
        label = df.index[i]
        bar_end = label + duration
        continuous_close = label.replace(
            hour=CONTINUOUS_CLOSE.hour,
            minute=CONTINUOUS_CLOSE.minute,
            second=0,
            microsecond=0,
        )
        if bar_end <= continuous_close and bar_end <= now:
            return df.iloc[: i + 1]
    return df.iloc[:0]


def true_range(df: pd.DataFrame) -> pd.Series:
    """Wilder's True Range series.

    TR = max(high - low, |high - prev_close|, |low - prev_close|)
    """
    prev_close = df["close"].shift(1)
    high = df["high"]
    low = df["low"]
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    tr.iloc[0] = (high - low).iloc[0]  # first bar has no previous close
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing.

    The first value is the SMA of the first ``period`` TR values; subsequent
    values use ``prev * (n-1) + tr) / n``.
    """
    tr = true_range(df)
    if len(tr) < period:
        return tr.rolling(period, min_periods=1).mean()
    result = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return result


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing).

    RSI = 100 - 100 / (1 + RS), where RS = avg_gain / avg_loss.
    """
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi_values = 100.0 - (100.0 / (1.0 + rs))
    rsi_values[avg_loss == 0.0] = 100.0  # no losses -> RSI 100
    return rsi_values


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(period, min_periods=1).mean()


def bollinger(
    df: pd.DataFrame,
    period: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """Bollinger Bands on the close.

    Returns a frame with ``middle, upper, lower, bandwidth`` where
    ``bandwidth = (upper - lower) / middle``. A low bandwidth is the classic
    "squeeze" signature of a compressed range.
    """
    middle = df["close"].rolling(period, min_periods=period).mean()
    std = df["close"].rolling(period, min_periods=period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    bandwidth = (upper - lower) / middle
    return pd.DataFrame(
        {"middle": middle, "upper": upper, "lower": lower, "bandwidth": bandwidth}
    )


def supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """Supertrend indicator (ATR trailing-stop bands).

    Returns a frame with ``supertrend`` (the active band) and ``direction``
    (``1`` bullish, ``-1`` bearish). Until enough bars exist for the ATR the
    values are ``NaN``; callers should require a warm-up period.
    """
    hl2 = (df["high"] + df["low"]) / 2.0
    atr_series = atr(df, period)
    upper = hl2 + multiplier * atr_series
    lower = hl2 - multiplier * atr_series
    close = df["close"]

    length = len(df)
    final_upper = upper.copy()
    final_lower = lower.copy()
    direction = pd.Series(1.0, index=df.index, dtype=float)
    st = pd.Series(float("nan"), index=df.index, dtype=float)

    for i in range(1, length):
        if pd.isna(final_upper.iloc[i - 1]) or pd.isna(upper.iloc[i]):
            continue

        # Bands ratchet: the upper band can only fall while price stays below
        # it, and the lower band can only rise while price stays above it.
        if close.iloc[i - 1] <= final_upper.iloc[i - 1]:
            final_upper.iloc[i] = min(upper.iloc[i], final_upper.iloc[i - 1])
        if close.iloc[i - 1] >= final_lower.iloc[i - 1]:
            final_lower.iloc[i] = max(lower.iloc[i], final_lower.iloc[i - 1])

        # Direction flips when the close crosses the opposite band.
        if close.iloc[i] > final_upper.iloc[i - 1]:
            direction.iloc[i] = 1.0
        elif close.iloc[i] < final_lower.iloc[i - 1]:
            direction.iloc[i] = -1.0
        else:
            direction.iloc[i] = direction.iloc[i - 1]

        st.iloc[i] = (
            final_lower.iloc[i] if direction.iloc[i] == 1.0 else final_upper.iloc[i]
        )

    return pd.DataFrame({"supertrend": st, "direction": direction})


def resample(df: pd.DataFrame, rule: str, offset: str | None = None) -> pd.DataFrame:
    """Resample OHLCV candles to a larger timeframe.

    ``rule`` is a pandas offset alias: ``"W"`` (weekly), ``"ME"`` (month-end),
    ``"1h"`` etc. ``offset`` shifts the bin boundaries (e.g. ``"15min"`` makes
    1-hour bins start at 09:15 IST like the NSE session).
    """
    if df.empty:
        return df
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    return df.resample(rule, offset=offset).agg(agg).dropna(subset=["open"])


def volume_ratio(df: pd.DataFrame, period: int = 20) -> float:
    """Ratio of the last candle's volume to its ``period``-bar average.

    Returns ``1.0`` when there is not enough history.
    """
    if len(df) < period:
        return 1.0
    avg = df["volume"].rolling(period, min_periods=period).mean().iloc[-1]
    if not avg or avg == 0:
        return 1.0
    return float(df["volume"].iloc[-1] / avg)

