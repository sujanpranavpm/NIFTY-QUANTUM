"""Indicator primitives.

The upstream library computes ``SMA_20_close`` / ``RSI_14_close`` on its
server-side data service, so the exact vendor implementation is not published.
The definitions below are the canonical ones (Wilder RSI, simple moving
average, Wilder-style ATR written exactly as in upstream example 19/20), and
the RSI variant is switchable because several of the ported strategies are
threshold strategies that are sensitive to it. Any deviation from the vendor is
therefore explicit and testable rather than silent.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

RSI_METHODS = ("wilder", "sma")


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average with a full-window requirement."""
    return series.rolling(period, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14, method: str = "wilder") -> pd.Series:
    """Relative Strength Index.

    Args:
        series: price series (typically close).
        period: lookback (14 in every ported strategy).
        method: ``"wilder"`` (default, classic RSI) or ``"sma"`` (simple average
            of gains/losses) for sensitivity testing.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    delta = series.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    if method == "wilder":
        alpha = 1.0 / period
        avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    elif method == "sma":
        avg_gain = gain.rolling(period, min_periods=period).mean()
        avg_loss = loss.rolling(period, min_periods=period).mean()
    else:
        raise ValueError(f"unknown RSI method {method!r}; expected one of {RSI_METHODS}")

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # A zero average loss means no downside over the window -> RSI 100.
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~(avg_gain.isna() | avg_loss.isna()), np.nan)
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Wilder true range, matching the upstream helper's three components."""
    previous_close = close.shift(1)
    components = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    )
    return components.max(axis=1)


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20
) -> pd.Series:
    """Simple (not Wilder-smoothed) ATR, identical to upstream ``average_true_range``."""
    return true_range(high, low, close).rolling(window, min_periods=window).mean()


def prior_extreme(series: pd.Series, lookback: int, kind: str = "max") -> pd.Series:
    """Rolling extreme of the ``lookback`` bars *before* the current bar.

    This reproduces the upstream slice ``series.iloc[idx - n : idx]``, which
    excludes the current bar and therefore cannot peek at the bar being traded.
    """
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    shifted = series.shift(1)
    if kind == "max":
        return shifted.rolling(lookback, min_periods=lookback).max()
    if kind == "min":
        return shifted.rolling(lookback, min_periods=lookback).min()
    raise ValueError("kind must be 'max' or 'min'")


def momentum(series: pd.Series, lookback: int) -> pd.Series:
    """Simple total return over ``lookback`` bars (upstream uses 126 for ~6 months)."""
    return series / series.shift(lookback) - 1.0


def precompute_daily_features(
    bars: pd.DataFrame,
    rsi_period: int = 14,
    rsi_method: str = "wilder",
    sma_periods: tuple[int, ...] = (20, 50),
) -> pd.DataFrame:
    """Build the feature columns the ported strategies read from ``features``.

    Column names mirror the upstream names (``SMA_20_close``, ``RSI_14_close``,
    ``adj_close``, ...) so the strategy bodies stay close to the originals.
    Every feature at row *t* uses only bars up to and including *t*.
    """
    out = pd.DataFrame(index=bars.index)
    for col in ("open", "high", "low", "close", "adj_close", "volume"):
        if col in bars.columns:
            out[col] = bars[col]

    close = bars["close"]
    adj = bars.get("adj_close", close)
    out["adj_close"] = adj

    for period in sma_periods:
        out[f"SMA_{period}_close"] = sma(close, period)
        out[f"SMA_{period}_adj_close"] = sma(adj, period)
    out[f"RSI_{rsi_period}_close"] = rsi(close, rsi_period, rsi_method)
    out["ATR_20"] = atr(bars["high"], bars["low"], close, 20)
    return out
